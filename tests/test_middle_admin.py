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
from tests.fixtures.e10.seed import build, write_db  # noqa: E402

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
    # 改异步后不再固定返回 overlap_warning
    assert body.get("executed") in (True, False)


def test_trigger_rejects_reconcile(middle_env):
    client, _ = middle_env
    r = client.post("/api/actions/trigger", headers={"Authorization": "Bearer secret"},
                    json={"action": "reconcile"})
    assert r.status_code == 400


def test_html_pages(middle_env):
    client, _ = middle_env
    h = {"Authorization": "Bearer secret"}
    for path in ("/status", "/config", "/logs", "/metadata", "/tables", "/push-logs"):
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


# ---- 异步触发 + 单飞锁 ----

def test_async_trigger_returns_run_id_immediately(middle_env):
    """异步触发立即返回 run_id 和 status=started,不阻塞。"""
    client, _ = middle_env
    r = client.post("/api/actions/trigger", headers={"Authorization": "Bearer secret"},
                    json={"action": "sync"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "sync"
    assert body["executed"] is True
    assert body["status"] == "started"
    assert isinstance(body["run_id"], int)
    assert body["run_id"] > 0


def test_trigger_returns_already_running_on_second_call(middle_env):
    """第二次触发在锁未释放时返回 already_running。"""
    from data2agent.connect.sync_lock import SourceSyncLock
    from data2agent.connect.config import load_config

    client, cfg_path = middle_env
    cfg = load_config(cfg_path)
    name = next(iter(cfg.sources))

    # 手动获取锁模拟后台正在运行
    lock = SourceSyncLock.try_acquire(cfg.landing, name)
    assert lock is not None, "应能获取锁"
    try:
        r = client.post("/api/actions/trigger", headers={"Authorization": "Bearer secret"},
                        json={"action": "sync"})
        assert r.status_code == 200
        body = r.json()
        assert body["reason"] == "already_running", (
            f"expected already_running, got {body}")
        assert body["executed"] is False
    finally:
        lock.release()


def test_trigger_rejects_unknown_action(middle_env):
    client, _ = middle_env
    r = client.post("/api/actions/trigger", headers={"Authorization": "Bearer secret"},
                    json={"action": "unknown_action"})
    assert r.status_code == 400
    body = r.json()
    detail = body.get("detail", "")
    if isinstance(detail, dict):
        detail = detail.get("detail", "")
    assert "不支持" in detail or "仅支持" in str(body)


# ---- 运行 API ----

def test_runs_list_returns_all_sources_when_source_not_specified(middle_env):
    client, _ = middle_env
    r = client.get("/api/runs", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert "runs" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body
    assert isinstance(body["runs"], list)
    if body["runs"]:
        run = body["runs"][0]
        for key in ("id", "source", "status", "started_at", "tables", "rows"):
            assert key in run, f"run missing key: {key}"


def test_runs_list_filters_by_source(middle_env):
    client, _ = middle_env
    r = client.get("/api/runs?source=digiwin_e10", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    for run in body["runs"]:
        assert run["source"] == "digiwin_e10"


def test_runs_list_unknown_source_returns_404(middle_env):
    client, _ = middle_env
    r = client.get("/api/runs?source=nonexistent", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 404


def test_runs_list_rejects_invalid_limit(middle_env):
    client, _ = middle_env
    r = client.get("/api/runs?limit=100", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 422


def test_run_detail_returns_steps(middle_env):
    client, _ = middle_env
    # 先查列表取一个 run_id
    r = client.get("/api/runs", headers={"Authorization": "Bearer secret"})
    runs = r.json()["runs"]
    if not runs:
        pytest.skip("没有 run 记录")
    run_id = runs[0]["id"]
    r2 = client.get(f"/api/runs/{run_id}", headers={"Authorization": "Bearer secret"})
    assert r2.status_code == 200
    body = r2.json()
    assert "run" in body
    assert "steps" in body
    assert body["run"]["id"] == run_id
    assert isinstance(body["steps"], list)


def test_run_detail_404_for_nonexistent_run(middle_env):
    client, _ = middle_env
    r = client.get("/api/runs/99999", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 404


def test_run_returned_in_status(middle_env):
    """验证 /api/status 返回 latest_run 和 running_run 字段。"""
    client, _ = middle_env
    r = client.get("/api/status", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["schedule_source"] == "derived_from_yaml"
    for src in body.get("sources", []):
        # running_run 可能为 null
        assert "running_run" in src, f"source missing running_run"
        # latest_run 至少在有 run 记录后不为 null
        assert "latest_run" in src, f"source missing latest_run"


def test_push_logs_api_returns_correct_batch_progress(middle_env):
    """推送记录列表与批次详情暴露真实 write 行数和完成状态。"""
    from data2agent.connect.config import load_config

    client, cfg_path = middle_env
    cfg = load_config(cfg_path)
    db = LandingStore(cfg.landing)
    try:
        for kind, rows, status in (
            ("write", 10, "ok"), ("write", 5, "ok"),
            ("write", 7, "failed"), ("complete_table", 15, "ok"),
        ):
            db.record_push_log(
                SOURCE, kind, "CUSTOMER", "incremental",
                batch_id="push-test-1", rows_count=rows, status=status,
            )
    finally:
        db.con.close()
    headers = {"Authorization": "Bearer secret"}
    listed = client.get("/api/push-logs", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["total"] >= 4
    detail = client.get("/api/push-logs/batch/push-test-1", headers=headers)
    assert detail.status_code == 200
    progress = detail.json()["progress"]
    assert progress["rows"] == 15
    assert progress["write_ok_batches"] == 2
    assert progress["failed"] == 1
    assert progress["completed"] is True


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
