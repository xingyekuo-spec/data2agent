"""抽取表管理 API 测试(M5)。"""

from datetime import date
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.increment import incremental_sync  # noqa: E402
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.middle_admin.app import create_app  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from tests.fixtures.e10.seed import build, write_db  # noqa: E402
from tests.helpers import watermarks_from_pack, whitelist_from_pack  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    sync_every: 30m\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        schema: main\n"
        "        key_columns: [Id]\n"
        "        watermark: LAST_MODIFIED_DATE\n"
        "      CURRENCY:\n"
        "        mode: full_refresh\n"
        "        schema: main\n",
        encoding="utf-8",
    )
    app = create_app(config_path=cfg, token="secret")
    return TestClient(app), cfg


def _h():
    return {"Authorization": "Bearer secret"}


def test_get_extraction_tables(env):
    client, _ = env
    r = client.get("/api/extraction-tables", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == SOURCE
    assert body["revision"]
    assert "CUSTOMER" in body["tables"]
    assert body["tables"]["CUSTOMER"]["watermark"] == "LAST_MODIFIED_DATE"
    assert body["table_count"] == 2


def test_validate_rejects_incremental_without_key(env):
    client, _ = env
    r = client.post("/api/extraction-tables/validate", headers=_h(), json={
        "tables": {
            "CUSTOMER": {"mode": "incremental", "schema": "main",
                         "watermark": "LAST_MODIFIED_DATE"},
        },
        "live": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["results"][0]["status"] == "key_missing"


def test_validate_ready_with_live_sqlite(env):
    client, _ = env
    r = client.post("/api/extraction-tables/validate", headers=_h(), json={
        "tables": {
            "CUSTOMER": {
                "mode": "incremental", "schema": "main",
                "key_columns": ["Id"], "watermark": "LAST_MODIFIED_DATE",
            },
            "CURRENCY": {"mode": "full_refresh", "schema": "main"},
        },
        "live": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert {x["table"]: x["status"] for x in body["results"]} == {
        "CURRENCY": "ready", "CUSTOMER": "ready",
    }


def test_put_requires_revision_and_is_atomic(env):
    client, cfg = env
    rev = client.get("/api/extraction-tables", headers=_h()).json()["revision"]
    missing = client.put("/api/extraction-tables", headers=_h(), json={
        "tables": {"CURRENCY": {"mode": "full_refresh", "schema": "main"}},
    })
    assert missing.status_code == 409

    stale = client.put("/api/extraction-tables", headers=_h(), json={
        "revision": "sha256:deadbeef",
        "tables": {"CURRENCY": {"mode": "full_refresh", "schema": "main"}},
        "live": True,
    })
    assert stale.status_code == 409

    ok = client.put("/api/extraction-tables", headers=_h(), json={
        "revision": rev,
        "tables": {
            "CURRENCY": {"mode": "full_refresh", "schema": "main"},
            "ITEM": {
                "mode": "incremental", "schema": "main",
                "key_columns": ["Id"], "watermark": "LAST_MODIFIED_DATE",
            },
        },
        "live": True,
    })
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert ok.json()["diff"]["removed"] == ["CUSTOMER"]
    assert "ITEM" in ok.json()["diff"]["added"]
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert set(data["sources"][SOURCE]["tables"]) == {"CURRENCY", "ITEM"}
    assert "CUSTOMER" not in data["sources"][SOURCE]["tables"]


def test_live_connection_failed_is_not_ready(env, monkeypatch):
    """ERP 不可达时不得返回 ready / 不得保存并写入 validated_at。"""
    from data2agent.connect import metadata as md

    def boom(_scfg):
        raise md.MetadataError("connection_failed", "数据库访问失败")

    monkeypatch.setattr(md, "build_discoverer", boom)
    # extraction_tables imports build_discoverer at module level — patch there too
    import data2agent.middle_admin.extraction_tables as et
    monkeypatch.setattr(et, "build_discoverer", boom)

    client, cfg = env
    h = {"Authorization": "Bearer secret"}
    tables = {
        "CUSTOMER": {
            "mode": "incremental", "schema": "main",
            "key_columns": ["Id"], "watermark": "LAST_MODIFIED_DATE",
        },
    }
    v = client.post("/api/extraction-tables/validate", headers=h, json={
        "tables": tables, "live": True,
    })
    assert v.status_code == 200
    assert v.json()["ok"] is False
    assert v.json()["results"][0]["status"] == "connection_failed"

    rev = client.get("/api/extraction-tables", headers=h).json()["revision"]
    put = client.put("/api/extraction-tables", headers=h, json={
        "revision": rev, "tables": tables, "live": True,
    })
    assert put.status_code == 200 and put.json()["ok"] is False
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    # PUT 失败后配置未替换为仅 CUSTOMER
    assert set(data["sources"][SOURCE]["tables"]) == {"CUSTOMER", "CURRENCY"}
    assert data["sources"][SOURCE]["tables"]["CUSTOMER"].get("validated_at") is None


def test_put_rejects_missing_table(env):
    client, _ = env
    rev = client.get("/api/extraction-tables", headers=_h()).json()["revision"]
    r = client.put("/api/extraction-tables", headers=_h(), json={
        "revision": rev,
        "tables": {"NO_SUCH": {"mode": "full_refresh", "schema": "main"}},
        "live": True,
    })
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["results"][0]["status"] == "table_missing"


def test_successful_live_put_stamps_validated_at(env):
    client, cfg = env
    rev = client.get("/api/extraction-tables", headers=_h()).json()["revision"]
    r = client.put("/api/extraction-tables", headers=_h(), json={
        "revision": rev,
        "tables": {"CURRENCY": {"mode": "full_refresh", "schema": "main"}},
        "live": True,
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["sources"][SOURCE]["tables"]["CURRENCY"]["validated_at"]


def test_put_ignores_client_live_false(env, monkeypatch):
    """PUT 不得因客户端 live:false 跳过现场校验而保存。"""
    from data2agent.connect import metadata as md
    import data2agent.middle_admin.extraction_tables as et

    def boom(_scfg):
        raise md.MetadataError("connection_failed", "数据库访问失败")

    monkeypatch.setattr(md, "build_discoverer", boom)
    monkeypatch.setattr(et, "build_discoverer", boom)

    client, cfg = env
    h = {"Authorization": "Bearer secret"}
    tables = {
        "CUSTOMER": {
            "mode": "incremental", "schema": "main",
            "key_columns": ["Id"], "watermark": "LAST_MODIFIED_DATE",
        },
    }
    # 离线预览仍可结构通过
    preview = client.post("/api/extraction-tables/validate", headers=h, json={
        "tables": tables, "live": False,
    })
    assert preview.status_code == 200 and preview.json()["ok"] is True

    rev = client.get("/api/extraction-tables", headers=h).json()["revision"]
    put = client.put("/api/extraction-tables", headers=h, json={
        "revision": rev, "tables": tables, "live": False,
    })
    assert put.status_code == 200 and put.json()["ok"] is False
    assert put.json()["results"][0]["status"] == "connection_failed"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert set(data["sources"][SOURCE]["tables"]) == {"CUSTOMER", "CURRENCY"}


def test_html_pages_share_source_scoped_draft_key(env):
    client, _ = env
    meta = client.get("/metadata", headers=_h()).text
    tables = client.get("/tables", headers=_h()).text
    assert "d2a_extraction_draft:" in meta
    assert "d2a_extraction_draft:" in tables
    # 不得再写入无 source 分区的固定草稿键（迁移读取除外）
    assert "sessionStorage.setItem('d2a_extraction_draft'" not in meta
    assert "sessionStorage.setItem('d2a_extraction_draft'" not in tables
    assert "sessionStorage.setItem(LEGACY_DRAFT_KEY" not in meta
    assert "rememberSource" in meta
    assert "selected.source" in meta or "body.source" in meta


def test_tables_page_keeps_draft_base_revision_for_optimistic_lock(env):
    """恢复草稿时不得用服务器最新 revision 替换 base_revision，否则会静默覆盖他会话改动。"""
    client, _ = env
    page = client.get("/tables", headers=_h()).text
    assert "draft.base_revision" in page
    assert "base !== body.revision" in page
    assert "revision=base" in page
    assert "保存将触发冲突" in page


def test_html_metadata_and_tables_pages(env):
    client, _ = env
    for path in ("/metadata", "/tables", "/status", "/config", "/logs"):
        r = client.get(path, headers=_h())
        assert r.status_code == 200, path
    nav = client.get("/status", headers=_h()).text
    assert 'href="/metadata"' in nav and 'href="/tables"' in nav
