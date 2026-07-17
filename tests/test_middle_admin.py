"""中间机管理 API 测试:配置读写、状态推算、Token 认证。"""

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.increment import incremental_sync, watermarks_from_pack  # noqa: E402
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.connect.mapping_apply import apply_objects  # noqa: E402
from data2agent.connect.sync import whitelist_from_pack  # noqa: E402
from data2agent.middle_admin.app import create_app  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.showroom.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def middle_env(tmp_path):
    """seed 源库 + 完整管道后的落地库 + connect.yaml(与 test_console 同构)。"""
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE), audit_hook=hook)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    apply_objects(landing, pack, SOURCE)
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    sync_every: 30m\n",
        encoding="utf-8")
    app = create_app(config_path=cfg, token="secret", log_path=tmp_path / "c.log")
    return TestClient(app), cfg


def test_config_get_requires_token(middle_env):
    client, _ = middle_env
    assert client.get("/api/config").status_code == 401
    r = client.get("/api/config", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert "sync_every" in str(body)


def test_config_post_whitelist_and_validate(middle_env):
    client, cfg = middle_env
    h = {"Authorization": "Bearer secret"}
    r = client.post("/api/config", headers=h, json={
        "sources": {"digiwin_e10": {"sync_every": "15m", "dsn_env": "NOPE"}}
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    text = cfg.read_text(encoding="utf-8")
    assert "15m" in text and "NOPE" not in text


def test_config_validate_without_save(middle_env):
    client, cfg = middle_env
    h = {"Authorization": "Bearer secret"}
    before = cfg.read_text(encoding="utf-8")
    r = client.post("/api/config/validate", headers=h, json={
        "sources": {"digiwin_e10": {"sync_every": "15m"}}
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert cfg.read_text(encoding="utf-8") == before


def test_status_has_schedule_source(middle_env):
    client, _ = middle_env
    r = client.get("/api/status", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["schedule_source"] == "derived_from_yaml"
    assert body["sources"], "应有至少一个源"
    src = body["sources"][0]
    assert "in_window" in src
    assert "watermarks" in src
    assert "next_sync_at" in src


def test_logs_missing_file(middle_env):
    client, _ = middle_env
    r = client.get("/api/logs?lines=50", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_logs_unknown_service(middle_env):
    client, _ = middle_env
    r = client.get("/api/logs?service=bogus",
                   headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "未知服务" in body["text"]


def test_logs_admin_service_reads_own_log(middle_env, tmp_path):
    client, _ = middle_env
    # log_dir 由 log_path 推导(= c.log 所在目录 = tmp_path)
    (tmp_path / "d2a-middle-admin.log").write_text(
        "Traceback: boom\nERROR something\n", encoding="utf-8")
    r = client.get("/api/logs?service=admin",
                   headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "Traceback" in body["text"]


def test_trigger_sync_warns_or_runs(middle_env):
    client, _ = middle_env
    r = client.post("/api/actions/trigger", headers={"Authorization": "Bearer secret"},
                    json={"action": "sync"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("action") == "sync"
    assert body.get("overlap_warning") is True


def test_trigger_rejects_reconcile(middle_env):
    client, _ = middle_env
    r = client.post("/api/actions/trigger", headers={"Authorization": "Bearer secret"},
                    json={"action": "reconcile"})
    assert r.status_code == 400


def test_html_pages(middle_env):
    client, _ = middle_env
    h = {"Authorization": "Bearer secret"}
    for path in ("/status", "/config", "/logs"):
        r = client.get(path, headers=h)
        assert r.status_code == 200
        body = r.content.lower()
        assert b"htmx" in body or b"hx-" in body or b"nav" in body
