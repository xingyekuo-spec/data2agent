"""中间机管理 API 测试:配置读写、状态推算、Token 认证。"""

import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.increment import incremental_sync  # noqa: E402
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.connect.mapping_apply import apply_objects  # noqa: E402
from tests.helpers import whitelist_from_pack  # noqa: E402
import data2agent.middle_admin.app as middle_app  # noqa: E402
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
        "    sync_every: 30m\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: LAST_MODIFIED_DATE\n",
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
    rev = client.get("/api/config", headers=h).json()["revision"]
    r = client.post("/api/config", headers=h, json={
        "sources": {"digiwin_e10": {"sync_every": "15m", "dsn_env": "NOPE"}},
        "revision": rev,
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    text = cfg.read_text(encoding="utf-8")
    assert "15m" in text and "NOPE" not in text


def test_config_post_requires_current_revision(middle_env):
    client, _ = middle_env
    h = {"Authorization": "Bearer secret"}
    missing = client.post("/api/config", headers=h, json={"templates": "new"})
    assert missing.status_code == 409
    revision = client.get("/api/config", headers=h).json()["revision"]
    saved = client.post("/api/config", headers=h, json={
        "sources": {"digiwin_e10": {"sync_every": "15m"}},
        "revision": revision,
    })
    assert saved.status_code == 200 and saved.json()["revision"] != revision
    stale = client.post("/api/config", headers=h, json={
        "sources": {"digiwin_e10": {"sync_every": "20m"}},
        "revision": revision,
    })
    assert stale.status_code == 409


def test_config_same_revision_allows_only_one_concurrent_writer(middle_env, monkeypatch):
    _, cfg = middle_env
    app = create_app(config_path=cfg, token="secret")
    h = {"Authorization": "Bearer secret"}
    revision = TestClient(app).get("/api/config", headers=h).json()["revision"]
    original_merge = middle_app.merge_whitelist_and_save

    def slow_merge(*args, **kwargs):
        time.sleep(0.05)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(middle_app, "merge_whitelist_and_save", slow_merge)

    def submit(value: str) -> int:
        return TestClient(app).post("/api/config", headers=h, json={
            "sources": {"digiwin_e10": {"sync_every": value}},
            "revision": revision,
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, ("15m", "20m")))
    assert sorted(statuses) == [200, 409]


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


def test_status_and_trigger_explain_empty_tables(middle_env):
    client, cfg = middle_env
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["sources"][SOURCE]["tables"] = {}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    h = {"Authorization": "Bearer secret"}
    status = client.get("/api/status", headers=h)
    assert status.status_code == 200
    assert status.json()["sources"][0]["tables_configured"] is False
    trigger = client.post("/api/actions/trigger", headers=h, json={"action": "sync"})
    assert trigger.status_code == 200
    assert trigger.json()["reason"] == "tables_unconfigured"
    assert trigger.json()["executed"] is False


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
    for path in ("/status", "/config", "/logs", "/metadata", "/tables"):
        r = client.get(path, headers=h)
        assert r.status_code == 200
        body = r.content.lower()
        assert b"htmx" in body or b"hx-" in body or b"nav" in body


def test_config_page_keeps_and_submits_revision(middle_env):
    client, _ = middle_env
    page = client.get("/config", headers={"Authorization": "Bearer secret"}).text
    assert "currentRevision" in page
    assert "revision: currentRevision" in page
    assert "抽取表配置" not in page


def test_probe_connection_returns_generic_error_without_unbound_local(monkeypatch):
    class FakeOdbcError(Exception):
        pass

    class BrokenConnection:
        def cursor(self):
            raise RuntimeError("cursor boom")

        def close(self):
            pass

    fake_pyodbc = types.SimpleNamespace(
        Error=FakeOdbcError,
        connect=lambda *_args, **_kwargs: BrokenConnection(),
    )
    monkeypatch.setitem(sys.modules, "pyodbc", fake_pyodbc)
    result = middle_app._probe_connection_pure("DSN=test")
    assert result == {"status": "failed", "error": "RuntimeError", "detail": "cursor boom"}


def test_connection_probe_timeout_does_not_wait_for_worker(monkeypatch):
    def slow_probe(_dsn: str, timeout: int = 10) -> dict:
        time.sleep(0.15)
        return {"status": "connected"}

    monkeypatch.setattr(middle_app, "_probe_connection_pure", slow_probe)
    started = time.perf_counter()
    result = middle_app._probe_connection_with_timeout("DSN=test", timeout=0.02)
    elapsed = time.perf_counter() - started
    assert result["error"] == "timeout"
    assert elapsed < 0.12
