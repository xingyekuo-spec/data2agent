"""真实 Overview API 集成测试(M3-T04):空库 / 正常库 / 告警 / 无副作用。"""

from __future__ import annotations

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
from data2agent.console.app import create_app  # noqa: E402
from data2agent.console.contracts import OverviewResponse  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.showroom.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE), audit_hook=hook)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    apply_objects(landing, pack, SOURCE)
    return landing


def _client(landing: LandingStore) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates"))


def test_overview_real_aggregation(env):
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    s = body.summary
    assert s.raw_rows is not None and s.raw_rows > 0
    assert s.object_rows is not None and s.object_rows > 0
    assert s.materialized_objects == 5
    assert s.template_objects == 5
    assert s.quarantine_pending == 0
    assert s.last_run_at is not None and s.last_run_at.tzinfo is not None
    assert s.data_updated_at is not None
    # 版本:app/template 真实,dataset/object 未启用为 null
    assert body.versions.app is not None
    assert body.versions.template
    assert body.versions.dataset is None and body.versions.object is None
    # binding:当前模板全部 draft → info 告警,且 mapping 不显示为 healthy
    assert body.binding_summary.draft == 10
    kinds = {a.id for a in body.alerts}
    assert "binding-draft" in kinds
    # 最近运行带类型;趋势桶 <= 24 且时间有序
    assert {r.run_type for r in body.recent_runs} >= {"sync", "apply"}
    buckets = [p.bucket for p in body.sync_trend]
    assert len(buckets) <= 24
    assert buckets == sorted(buckets)
    assert sum(p.runs for p in body.sync_trend) >= 1


def test_overview_empty_db_is_empty_not_healthy(tmp_path):
    landing = LandingStore(tmp_path / "empty.sqlite")
    body = OverviewResponse.model_validate(_client(landing).get("/api/overview").json())
    assert body.summary.raw_rows == 0          # 无 raw 表是事实 0
    assert body.summary.object_rows is None    # 未物化为 null,不是 0
    assert body.summary.materialized_objects == 0
    assert body.recent_runs == []
    assert body.sync_trend == []
    assert body.summary.last_run_at is None


def test_overview_alerts_for_quarantine_and_draft(env):
    env.con.execute(
        "INSERT INTO d2a_quarantine (source, object, keys_json, reason, created_at) "
        "VALUES (?, 'Customer', '{}', '枚举未覆盖', '2026-07-18T10:00:00')", (SOURCE,))
    env.con.commit()
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    by_id = {a.id: a for a in body.alerts}
    assert by_id["quarantine-pending"].severity == "warning"
    assert by_id["binding-draft"].severity == "info"
    # 排序:critical/warning/info
    ranks = [{"critical": 0, "warning": 1, "info": 2}[a.severity] for a in body.alerts]
    assert ranks == sorted(ranks)


def test_overview_get_has_no_side_effects(env):
    client = _client(env)
    before = env.con.execute("SELECT COUNT(*) FROM d2a_audit_log").fetchone()[0]
    runs_before = env.con.execute("SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0]
    client.get("/api/overview")
    client.get("/api/overview")
    after = env.con.execute("SELECT COUNT(*) FROM d2a_audit_log").fetchone()[0]
    runs_after = env.con.execute("SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0]
    assert (before, runs_before) == (after, runs_after)


def test_overview_raw_failure_is_null_not_partial(tmp_path):
    """任一源 raw 查询失败 → raw_rows 为 null;部分源的合计不得冒充总数。"""
    from data2agent.connect.config import load_config

    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    # source_b:正常 raw 表(有元数据列)
    landing.con.execute(
        'CREATE TABLE "raw_source_b__T1" '
        '("K" TEXT PRIMARY KEY, "_d2a_extracted_at" TEXT, "_d2a_deleted_at" TEXT)')
    landing.con.execute(
        'INSERT INTO "raw_source_b__T1" VALUES (\'k0\', \'2026-07-18T11:30:00\', NULL)')
    # source_a:坏 raw 表(缺 _d2a_deleted_at 列,查询必炸)
    landing.con.execute('CREATE TABLE "raw_source_a__BROKEN" ("K" TEXT PRIMARY KEY)')
    landing.con.commit()
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  source_a:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {tmp_path / 'a.sqlite'}\n"
        "  source_b:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {tmp_path / 'b.sqlite'}\n",
        encoding="utf-8")
    cfg = load_config(cfg_file)
    client = TestClient(create_app(cfg.landing, cfg.templates, cfg))
    body = OverviewResponse.model_validate(client.get("/api/overview").json())
    assert body.summary.raw_rows is None  # 不是 source_b 的 1 行
    assert any("查询失败" in a.reason for a in body.alerts)
