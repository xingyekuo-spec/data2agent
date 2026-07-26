"""平台便携包更新管理器:检查 → 下载校验 → 解压暂存 → 生成换包脚本。

只做「程序运行中」的准备阶段,全部读写限制在 ``data/updates`` 与
home 根目录的「升级.bat」,不触碰运行中的 exe/runtime;
真正的换包由退出后运行的 apply-update.ps1 完成(见 apply_script.py)。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from ..ingest.protocol import SUPPORTED_INGEST_PROTOCOL_VERSIONS
from .apply_script import write_update_scripts
from .manifest import (
    UpdateError,
    UpdateManifest,
    fetch_manifest,
    http_get,
    is_newer_version,
    parse_version,
)

#: 更新功能仅对便携包安装可用(home 下存在 BUILD-INFO.json)。
STATE_NAME = "state.json"
PACKAGE_ZIP_NAME = "package.zip"
STAGING_NAME = "staging"

PHASES = ("idle", "checked", "downloading", "ready", "failed")


class UpdateManager:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.updates_dir = self.home / "data" / "updates"
        self.staging_dir = self.updates_dir / STAGING_NAME
        self.package_zip = self.updates_dir / PACKAGE_ZIP_NAME
        self.state_path = self.updates_dir / STATE_NAME
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # ---- 基础信息 ----

    def build_info(self) -> dict | None:
        path = self.home / "BUILD-INFO.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def available(self) -> bool:
        return self.build_info() is not None

    def current_version(self) -> str:
        info = self.build_info() or {}
        for key in ("release_version", "application_version"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    # ---- 状态文件 ----

    def _write_state(self, **fields) -> dict:
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        state.update(fields)
        state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
        return state

    def _read_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"phase": "idle"}
        return data if isinstance(data, dict) else {"phase": "idle"}

    def status(self) -> dict:
        state = self._read_state()
        bat = self.home / "升级.bat"
        return {
            "available": self.available(),
            "phase": state.get("phase", "idle"),
            "current_version": self.current_version() or None,
            "target_version": state.get("target_version"),
            "update_available": state.get("update_available"),
            "protocol_ok": state.get("protocol_ok"),
            "blocked_reason": state.get("blocked_reason"),
            "notes": state.get("notes"),
            "progress_done": state.get("progress_done"),
            "progress_total": state.get("progress_total"),
            "error": state.get("error"),
            "bat_path": str(bat) if bat.is_file() else None,
            "updated_at": state.get("updated_at"),
        }

    # ---- 检查更新 ----

    def check(self, source_url: str, *, token: str | None = None) -> dict:
        """拉取清单并做协议兼容预检;结果写入状态文件。

        与下载任务互斥:下载进行中拒绝检查(否则检查会覆盖 state.json 的
        manifest/phase,而旧下载线程仍会把旧包标记为就绪,导致界面显示的
        版本与实际暂存包不一致)。锁包住整个检查过程,保证与 start_download
        的状态转换原子互斥;worker 自身不取这把锁,不会死锁。
        """
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise UpdateError("下载任务正在进行中,请等待完成后再检查更新")
            manifest = fetch_manifest(source_url, token=token)
            current = self.current_version()
            update_available = is_newer_version(manifest.version, current)
            missing = [v for v in SUPPORTED_INGEST_PROTOCOL_VERSIONS
                       if v not in manifest.supported_ingest_protocol_versions]
            protocol_ok = not missing
            blocked_reason = None
            if update_available and not protocol_ok:
                blocked_reason = (
                    f"新版本不再支持现场中间机使用的推送协议(v{', v'.join(missing)}),"
                    "需先升级中间机,已阻止本次平台升级")
            state = self._write_state(
                phase="checked",
                target_version=manifest.version,
                update_available=update_available,
                protocol_ok=protocol_ok,
                blocked_reason=blocked_reason,
                notes=manifest.notes or None,
                manifest=manifest.as_dict(),
                progress_done=None,
                progress_total=None,
                error=None,
            )
            return {
                "ok": True,
                "current_version": current or None,
                "latest_version": manifest.version,
                "update_available": update_available,
                "protocol_ok": protocol_ok,
                "blocked_reason": blocked_reason,
                "notes": manifest.notes or None,
                "state": state,
            }

    # ---- 下载 + 暂存 ----

    def start_download(self, *, token: str | None = None) -> None:
        """后台线程下载并暂存;仅允许 checked + 可更新 状态启动。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise UpdateError("下载任务正在进行中,请稍候")
            state = self._read_state()
            if state.get("phase") == "checked" and not state.get("update_available"):
                raise UpdateError("当前已是最新版本,无需下载")
            if state.get("phase") != "checked":
                # ready / failed / downloading 等状态一律要求重新检查,
                # 避免重复点击或陈旧清单绕过协议预检。
                raise UpdateError("请先执行「检查更新」")
            if state.get("protocol_ok") is False:
                raise UpdateError(state.get("blocked_reason") or "协议不兼容,已阻止升级")
            raw = state.get("manifest")
            if not isinstance(raw, dict):
                raise UpdateError("请先执行「检查更新」")
            manifest = UpdateManifest.from_dict(raw)
            self._worker = threading.Thread(
                target=self._download_worker, args=(manifest, token),
                name="d2a-update-download", daemon=True)
            self._worker.start()

    def _download_worker(self, manifest: UpdateManifest, token: str | None) -> None:
        try:
            self.download_and_stage(manifest, token=token)
            bat = write_update_scripts(self.home, self.updates_dir)
            self._write_state(phase="ready", error=None, bat_path=str(bat))
        except UpdateError as exc:
            self._write_state(phase="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 —— 兜底:任何异常都要落到状态文件
            self._write_state(phase="failed", error=f"更新准备失败:{exc}")

    def download_and_stage(self, manifest: UpdateManifest, *,
                           token: str | None = None) -> Path:
        """同步执行:下载 → sha256 校验 → 解压到 staging → 校验包布局。"""
        self._write_state(phase="downloading", error=None,
                          progress_done=0, progress_total=None)
        self._download(manifest, token=token)
        return self.stage(self.package_zip, expected_sha256=manifest.sha256,
                          expected_version=manifest.version)

    def _download(self, manifest: UpdateManifest, *, token: str | None) -> None:
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        done = 0
        last_report = -1
        with http_get(manifest.url, token=token, timeout=60.0) as resp, \
                self.package_zip.open("wb") as out:
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            self._write_state(progress_total=total)
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                # 状态写盘节流:每 4MB 一次
                if done - last_report >= (4 << 20):
                    self._write_state(progress_done=done)
                    last_report = done
        self._write_state(progress_done=done)
        if digest.hexdigest() != manifest.sha256:
            self.package_zip.unlink(missing_ok=True)
            raise UpdateError("更新包校验失败(sha256 不一致),请重试")

    # ---- 暂存与校验 ----

    def stage(self, zip_path: Path, *, expected_sha256: str | None = None,
              expected_version: str | None = None) -> Path:
        """解压 zip 到 staging 并校验便携包布局;返回包根目录。

        expected_version:清单声明的目标版本,与包内 BUILD-INFO.json 核对,
        防止错误关联的清单/包导致安装版本与界面提示不一致。
        """
        zip_path = Path(zip_path)
        if expected_sha256 and hashlib.sha256(zip_path.read_bytes()).hexdigest() != expected_sha256:
            raise UpdateError("更新包校验失败(sha256 不一致),请重试")
        if self.staging_dir.exists():
            shutil.rmtree(self.staging_dir)
        self.staging_dir.mkdir(parents=True)
        try:
            _safe_extract(zip_path, self.staging_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            raise UpdateError(f"更新包解压失败:{exc}") from exc
        pkg_root = _find_package_root(self.staging_dir)
        _validate_package(pkg_root, expected_version=expected_version)
        return pkg_root


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解包并拒绝路径穿越条目。"""
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise UpdateError(f"更新包含非法路径:{info.filename}")
        zf.extractall(dest)


def _find_package_root(staging: Path) -> Path:
    """zip 内可能是单层顶层目录,也可能直接是包内容。"""
    if (staging / "BUILD-INFO.json").is_file():
        return staging
    dirs = [p for p in staging.iterdir() if p.is_dir()]
    if len(dirs) == 1 and (dirs[0] / "BUILD-INFO.json").is_file():
        return dirs[0]
    raise UpdateError("更新包结构无法识别(缺少 BUILD-INFO.json)")


def _validate_package(pkg_root: Path, *, expected_version: str | None = None) -> None:
    """与 scripts/check_portable_package.py 一致的最小现场校验。"""
    for rel, kind in (
        ("BUILD-INFO.json", "file"),
        ("data2agent.exe", "file"),
        ("runtime/python.exe", "file"),
        ("app/templates", "dir"),
        ("app/console-ui/dist/index.html", "file"),
        ("启动平台.bat", "file"),
    ):
        path = pkg_root / rel
        ok = path.is_file() if kind == "file" else path.is_dir()
        if not ok:
            raise UpdateError(f"更新包不完整,缺少 {rel}")
    try:
        info = json.loads((pkg_root / "BUILD-INFO.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"更新包 BUILD-INFO.json 无法解析:{exc}") from exc
    if info.get("role") != "platform":
        raise UpdateError("更新包角色不符:平台端只能使用 platform 便携包")
    if expected_version:
        packaged = str(info.get("release_version") or info.get("application_version") or "")
        if not _same_version(packaged, expected_version):
            raise UpdateError(
                f"更新包版本({packaged or '未知'})与清单声明({expected_version})不一致,"
                "请重新检查更新")
    supported = set(info.get("supported_ingest_protocol_versions") or [])
    missing = [v for v in SUPPORTED_INGEST_PROTOCOL_VERSIONS if v not in supported]
    if missing:
        raise UpdateError(
            f"新版本不再支持现场中间机使用的推送协议(v{', v'.join(missing)}),"
            "需先升级中间机,已阻止本次平台升级")


def _same_version(a: str, b: str) -> bool:
    """版本等同:v0.6.0 == 0.6.0;可解析时按数值段比较。"""
    va, vb = parse_version(a), parse_version(b)
    if va and vb:
        width = max(len(va), len(vb))
        return va + (0,) * (width - len(va)) == vb + (0,) * (width - len(vb))
    return a.strip().lstrip("v") == b.strip().lstrip("v")
