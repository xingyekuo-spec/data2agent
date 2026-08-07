"""签发制授权矩阵:登记源按源 Token / 停用拒绝 / 未登记拒绝 / 管理员 Token 兼容。"""

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.platform.ingest.app import create_app  # noqa: E402
from data2agent.shared.store.landing import LandingStore  # noqa: E402

SOURCE = "factory_a_e10"
SOURCE_TOKEN = "src-token-aaa"


def _batch(source: str = SOURCE) -> dict:
    return {
        # 授权矩阵刻意走仍受支持的 v2，隔离 generation 契约对鉴权断言的影响。
        "ingest_protocol_version": "2",
        "source": source,
        "table": "CUSTOMER",
        "mode": "incremental",
        "columns": [["Id", "int"], ["CODE", "text"]],
        "pk": ["Id"],
        "batch_id": "b-001",
        "rows": [{"Id": 1, "CODE": "C1"}],
    }


def _register(landing: Path, source: str = SOURCE, token: str = SOURCE_TOKEN) -> None:
    db = LandingStore(landing)
    db.create_source_registration(
        source=source,
        token_sha256=hashlib.sha256(token.encode()).hexdigest(),
        created_at="2026-08-03T00:00:00+08:00")
    db.con.close()


def _post(client: TestClient, token: str | None, body: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/ingest/batch", json=body or _batch(), headers=headers)


def test_bootstrap_open_when_registry_empty(tmp_path):
    """空登记簿 + 无全局 Token = 开发引导期,开放推送。"""
    client = TestClient(create_app(tmp_path / "landing.sqlite"))
    assert _post(client, None).status_code == 200


def test_registered_source_token_matrix(tmp_path):
    """登记源:正确 Token 放行;错误/缺失 401;停用 403。"""
    landing = tmp_path / "landing.sqlite"
    _register(landing)
    client = TestClient(create_app(landing))

    assert _post(client, SOURCE_TOKEN).status_code == 200
    bad = _post(client, "wrong-token", _batch(source=SOURCE))
    assert bad.status_code == 401
    missing = _post(client, None)
    assert missing.status_code == 401

    db = LandingStore(landing)
    db.set_source_registration_status(SOURCE, "disabled", disabled_at="2026-08-03T01:00:00+08:00")
    db.con.close()
    disabled = _post(client, SOURCE_TOKEN)
    assert disabled.status_code == 403
    assert "已停用" in disabled.json()["detail"]


def test_unregistered_rejected_once_registry_nonempty(tmp_path):
    """签发制生效(登记簿非空)后,未登记源一律 403。"""
    landing = tmp_path / "landing.sqlite"
    _register(landing)
    client = TestClient(create_app(landing))
    r = _post(client, "whatever", _batch(source="ghost_source"))
    assert r.status_code == 403
    assert "未在平台登记" in r.json()["detail"]


def test_admin_token_overrides_for_any_source(tmp_path):
    """全局管理员 Token(迁移期):可推登记源与未登记源;错 Token 401。"""
    landing = tmp_path / "landing.sqlite"
    _register(landing)
    client = TestClient(create_app(landing, token="admin-token"))

    assert _post(client, "admin-token").status_code == 200
    assert _post(client, "admin-token", _batch(source="legacy_e10")).status_code == 200
    assert _post(client, SOURCE_TOKEN).status_code == 200  # 按源 Token 依然可用
    assert _post(client, "wrong", _batch(source="legacy_e10")).status_code == 401
