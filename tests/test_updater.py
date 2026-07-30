"""平台便携包在线升级:清单解析、版本比较、协议预检、下载暂存与 console API。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from data2agent.protocol.ingest import SUPPORTED_INGEST_PROTOCOL_VERSIONS
from data2agent.updater.core import UpdateManager
from data2agent.updater.manifest import (
    UpdateError,
    UpdateManifest,
    is_newer_version,
    parse_version,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.console.app import create_app  # noqa: E402

SUPPORTED = list(SUPPORTED_INGEST_PROTOCOL_VERSIONS)


# ---- 版本比较 ----


@pytest.mark.parametrize("latest,current,expected", [
    ("v0.6.0", "v0.5.1", True),
    ("0.6.0", "0.5.9", True),
    ("v0.5.1", "v0.5.1", False),
    ("v0.5.0", "v0.5.1", False),
    ("v1.0", "v0.9.9", True),
    ("v0.6", "v0.6.0", False),     # 0.6 == 0.6.0
    ("manual-abc", "v0.5.1", True),   # 无法解析但不同 → 可更新
    ("v0.5.1", "v0.5.1", False),
])
def test_version_compare(latest, current, expected):
    assert parse_version("v0.6.0") == (0, 6, 0)
    assert is_newer_version(latest, current) is expected


def test_manifest_validation():
    good = {
        "version": "v0.6.0",
        "package": "d2a-portable-platform-v0.6.0.zip",
        "url": "https://example.com/x.zip",
        "sha256": "a" * 64,
        "supported_ingest_protocol_versions": SUPPORTED,
    }
    m = UpdateManifest.from_dict(good)
    assert m.version == "v0.6.0" and m.supported_ingest_protocol_versions == tuple(SUPPORTED)
    with pytest.raises(UpdateError, match="缺少字段"):
        UpdateManifest.from_dict({"version": "v1"})
    with pytest.raises(UpdateError, match="sha256"):
        UpdateManifest.from_dict({**good, "sha256": "xyz"})


# ---- 便携包假包构造 ----


def _make_portable_zip(tmp_path: Path, version: str, *,
                       role: str = "platform",
                       supported: list[str] | None = None,
                       missing: str | None = None) -> Path:
    """构造一个最小但合法的 platform 便携包 zip,返回 zip 路径。"""
    pkg = tmp_path / f"d2a-portable-platform-{version}"
    files = {
        "data2agent.exe": b"exe",
        "runtime/python.exe": b"py",
        "app/templates/objects/demo.yaml": b"t",
        "app/console-ui/dist/index.html": b"<html></html>",
        "README.txt": b"r",
    }
    if missing:
        files.pop(missing)
    for rel, content in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    build_info = {
        "application_version": version.lstrip("v"),
        "release_version": version,
        "role": role,
        "supported_ingest_protocol_versions": supported if supported is not None else SUPPORTED,
    }
    (pkg / "BUILD-INFO.json").write_text(json.dumps(build_info), encoding="utf-8")
    zip_path = tmp_path / f"{pkg.name}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in pkg.rglob("*"):
            zf.write(path, path.relative_to(tmp_path))
    return zip_path


def _make_home(tmp_path: Path, version: str = "v0.5.1") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "BUILD-INFO.json").write_text(json.dumps({
        "application_version": version.lstrip("v"),
        "release_version": version,
        "role": "platform",
        "supported_ingest_protocol_versions": SUPPORTED,
    }), encoding="utf-8")
    return home


def _manifest_for(zip_path: Path, version: str,
                  supported: list[str] | None = None) -> dict:
    return {
        "version": version,
        "package": zip_path.name,
        "url": zip_path.resolve().as_uri(),
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        "supported_ingest_protocol_versions": supported if supported is not None else SUPPORTED,
        "notes": "测试版本",
    }


def _write_manifest(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "latest.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ---- UpdateManager ----


def test_check_update_available(tmp_path):
    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(tmp_path, _manifest_for(zip_path, "v0.6.0"))
    manager = UpdateManager(home)

    result = manager.check(manifest_url.as_uri())

    assert result["ok"] and result["update_available"] and result["protocol_ok"]
    assert result["current_version"] == "v0.5.1"
    assert result["latest_version"] == "v0.6.0"
    status = manager.status()
    assert status["phase"] == "checked" and status["update_available"] is True


def test_check_already_latest(tmp_path):
    home = _make_home(tmp_path, version="v0.6.0")
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(tmp_path, _manifest_for(zip_path, "v0.6.0"))
    result = UpdateManager(home).check(manifest_url.as_uri())
    assert result["update_available"] is False
    with pytest.raises(UpdateError, match="最新"):
        UpdateManager(home).start_download()


def test_check_blocked_by_protocol(tmp_path):
    """新平台放弃现场协议 → 拦截升级。"""
    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(
        tmp_path, _manifest_for(zip_path, "v0.6.0", supported=["3"]))
    manager = UpdateManager(home)

    result = manager.check(manifest_url.as_uri())

    assert result["update_available"] and result["protocol_ok"] is False
    assert "中间机" in result["blocked_reason"]
    with pytest.raises(UpdateError, match="中间机"):
        manager.start_download()


def test_download_and_stage_end_to_end(tmp_path):
    """file:// 源走完整链路:下载 → 校验 → 解压 → 布局校验 → 脚本生成。"""
    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(tmp_path, _manifest_for(zip_path, "v0.6.0"))
    manager = UpdateManager(home)
    manager.check(manifest_url.as_uri())

    manager.start_download()
    manager._worker.join(timeout=30)

    status = manager.status()
    assert status["phase"] == "ready", status
    assert status["progress_done"] == status["progress_total"]
    pkg_root = home / "data" / "updates" / "staging" / "d2a-portable-platform-v0.6.0"
    assert (pkg_root / "runtime" / "python.exe").is_file()
    assert (home / "升级.bat").is_file()
    ps1 = home / "data" / "updates" / "apply-update.ps1"
    assert ps1.is_file()
    # Windows PowerShell 5.1 需要 BOM 才能正确按 UTF-8 解析
    assert ps1.read_bytes().startswith(b"\xef\xbb\xbf")


def test_stage_rejects_corrupt_and_invalid(tmp_path):
    home = _make_home(tmp_path)
    manager = UpdateManager(home)

    good = _make_portable_zip(tmp_path, "v0.6.0")
    with pytest.raises(UpdateError, match="sha256"):
        manager.stage(good, expected_sha256="0" * 64)

    bad = _make_portable_zip(tmp_path, "v0.6.1", missing="runtime/python.exe")
    with pytest.raises(UpdateError, match="runtime"):
        manager.stage(bad)

    middle = _make_portable_zip(tmp_path, "v0.6.2", role="middle")
    with pytest.raises(UpdateError, match="角色"):
        manager.stage(middle)

    proto = _make_portable_zip(tmp_path, "v0.6.3", supported=["3"])
    with pytest.raises(UpdateError, match="中间机"):
        manager.stage(proto)


def test_stage_rejects_path_traversal(tmp_path):
    home = _make_home(tmp_path)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(UpdateError, match="非法路径"):
        UpdateManager(home).stage(evil)


def test_download_sha256_mismatch(tmp_path):
    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    data = _manifest_for(zip_path, "v0.6.0")
    data["sha256"] = "0" * 64
    manifest_url = _write_manifest(tmp_path, data)
    manager = UpdateManager(home)
    manager.check(manifest_url.as_uri())

    manager.start_download()
    manager._worker.join(timeout=30)

    status = manager.status()
    assert status["phase"] == "failed" and "sha256" in status["error"]


def test_stage_rejects_version_mismatch(tmp_path):
    """清单声明的版本必须与包内 BUILD-INFO.json 一致(P2)。"""
    home = _make_home(tmp_path)
    manager = UpdateManager(home)
    zip_path = _make_portable_zip(tmp_path, "v0.6.1")

    with pytest.raises(UpdateError, match="不一致"):
        manager.stage(zip_path, expected_version="v0.6.0")
    # 一致(含 v 前缀差异)应通过
    assert manager.stage(zip_path, expected_version="0.6.1").is_dir()


def test_download_and_stage_binds_manifest_version(tmp_path):
    """端到端:清单版本与包版本不符时,即使 sha256 正确也拒绝。"""
    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.1")
    data = _manifest_for(zip_path, "v0.6.0")  # 清单错标版本
    manifest_url = _write_manifest(tmp_path, data)
    manager = UpdateManager(home)
    manager.check(manifest_url.as_uri())

    manager.start_download()
    manager._worker.join(timeout=30)

    status = manager.status()
    assert status["phase"] == "failed" and "不一致" in status["error"]


def test_download_requires_checked_state(tmp_path):
    """仅 checked + 可更新 可启动下载;ready / failed 后必须重新检查(P1)。"""
    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(tmp_path, _manifest_for(zip_path, "v0.6.0"))
    manager = UpdateManager(home)

    # idle:未检查
    with pytest.raises(UpdateError, match="检查更新"):
        manager.start_download()

    manager.check(manifest_url.as_uri())
    manager.start_download()
    manager._worker.join(timeout=30)
    assert manager.status()["phase"] == "ready"

    # ready:重复点击不再直接重启下载
    with pytest.raises(UpdateError, match="检查更新"):
        manager.start_download()


def test_concurrent_download_conflict(tmp_path, monkeypatch):
    """下载进行中再次启动必须被拒绝(P1:单实例锁跨线程生效)。"""
    import threading

    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(tmp_path, _manifest_for(zip_path, "v0.6.0"))
    manager = UpdateManager(home)
    manager.check(manifest_url.as_uri())

    gate = threading.Event()
    real_download = UpdateManager._download

    def slow_download(self, manifest, *, token=None):
        gate.wait(timeout=30)
        return real_download(self, manifest, token=token)

    monkeypatch.setattr(UpdateManager, "_download", slow_download)
    manager.start_download()
    try:
        with pytest.raises(UpdateError, match="进行中"):
            manager.start_download()
        # 下载进行中也禁止重新检查:否则检查会覆盖清单/阶段,
        # 旧下载线程仍会把旧包标记为就绪,界面与实际暂存包不一致(P1)
        with pytest.raises(UpdateError, match="进行中"):
            manager.check(manifest_url.as_uri())
    finally:
        gate.set()
    manager._worker.join(timeout=30)
    status = manager.status()
    assert status["phase"] == "ready" and status["target_version"] == "v0.6.0"


def test_unavailable_without_build_info(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    manager = UpdateManager(home)
    assert manager.available() is False
    assert manager.status()["available"] is False


# ---- console API ----


def _console_client(home: Path) -> TestClient:
    return TestClient(create_app(home=home))


def test_update_api_full_flow(tmp_path):
    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(tmp_path, _manifest_for(zip_path, "v0.6.0"))
    client = _console_client(home)

    r = client.get("/api/update/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["current_version"] == "v0.5.1"

    r = client.post("/api/update/check", json={"source_url": manifest_url.as_uri()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["update_available"] and body["latest_version"] == "v0.6.0"

    r = client.post("/api/update/download")
    assert r.status_code == 200, r.text

    # 轮询直到就绪(本地 file:// 下载,很快)
    for _ in range(100):
        status = client.get("/api/update/status").json()
        if status["phase"] in ("ready", "failed"):
            break
    assert status["phase"] == "ready", status
    assert status["bat_path"] and status["bat_path"].endswith("升级.bat")


def test_update_api_requires_source(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    monkeypatch.delenv("D2A_UPDATE_URL", raising=False)
    client = _console_client(home)
    r = client.post("/api/update/check", json={})
    assert r.json()["ok"] is False
    assert "D2A_UPDATE_URL" in r.json()["error"]


def test_update_api_download_before_check(tmp_path):
    home = _make_home(tmp_path)
    r = _console_client(home).post("/api/update/download")
    assert r.status_code == 409


def test_update_api_concurrent_download_conflict(tmp_path, monkeypatch):
    """API 层:下载进行中重复 POST 返回 409(单例管理器跨请求复用,P1)。"""
    import threading

    home = _make_home(tmp_path)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    manifest_url = _write_manifest(tmp_path, _manifest_for(zip_path, "v0.6.0"))
    client = _console_client(home)
    assert client.post("/api/update/check",
                       json={"source_url": manifest_url.as_uri()}).json()["ok"]

    gate = threading.Event()
    real_download = UpdateManager._download

    def slow_download(self, manifest, *, token=None):
        gate.wait(timeout=30)
        return real_download(self, manifest, token=token)

    monkeypatch.setattr(UpdateManager, "_download", slow_download)
    assert client.post("/api/update/download").status_code == 200
    try:
        r = client.post("/api/update/download")
        assert r.status_code == 409 and "进行中" in r.json()["detail"]
        # 下载进行中重新检查同样被拒绝,且不得篡改进行中的状态(P1 回归)
        r = client.post("/api/update/check", json={"source_url": manifest_url.as_uri()})
        assert r.status_code == 200
        assert r.json()["ok"] is False and "进行中" in r.json()["error"]
        mid = client.get("/api/update/status").json()
        assert mid["phase"] == "downloading" and mid["target_version"] == "v0.6.0"
    finally:
        gate.set()
    for _ in range(100):
        status = client.get("/api/update/status").json()
        if status["phase"] in ("ready", "failed"):
            break
    assert status["phase"] == "ready", status


def test_update_api_unavailable_in_readonly_mode(tmp_path):
    """非 home 模式(只读/开发)不提供升级。"""
    client = TestClient(create_app(landing=str(tmp_path / "x.sqlite"),
                                   templates="templates"))
    assert client.get("/api/update/status").json()["available"] is False
    r = client.post("/api/update/check", json={"source_url": "http://x/latest.json"})
    assert r.status_code == 400
