"""M4-T03:统一 Run steps 写入测试(sync / reconcile 生命周期)。"""

from datetime import date
from pathlib import Path

import pytest

from data2agent.middle.extract import reconcile as reconcile_mod
from data2agent.middle.extract.adapters.base import TableInfo
from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import incremental_sync
from data2agent.middle.extract.reconcile import reconcile
from data2agent.shared.store.landing import LandingStore, raw_table_name
from tests.fixtures.e10.seed import build, write_db
from tests.helpers import e10_watermarks, e10_whitelist

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"


@pytest.fixture()
def landing_synced(tmp_path):
    """seed 源库 → 一次增量同步后的落地库(带 sync steps)。"""
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), e10_whitelist())
    report = incremental_sync(adapter, landing, SOURCE, e10_watermarks())
    return landing, report


# ---- sync ----


def test_sync_writes_table_steps(landing_synced):
    landing, report = landing_synced
    steps = landing.steps_for_run(report.run_id)
    assert len(steps) == len(report.tables)
    assert [s["ordinal"] for s in steps] == list(range(1, len(steps) + 1))
    for step in steps:
        assert step["kind"] == "table"
        assert step["status"] == "ok"
        assert step["rows_in"] == step["rows_out"] and step["rows_in"] > 0
        assert step["batch_id"]
        assert step["finished_at"]
    # 水位 JSON 有证据(initial 策略 before 为 null,after 有值)
    wm_steps = [s for s in steps if s["watermark_after"] is not None]
    assert wm_steps, "水位列表应有 watermark_after"
    assert all(s["watermark_after"].startswith('"') for s in wm_steps)


def test_sync_paused_step_not_failed(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    base = SqliteReadOnlyAdapter(str(src), e10_whitelist())

    class TwoTableAdapter:
        def tables(self):
            return list(base.tables())[:2]

        def __getattr__(self, name):
            return getattr(base, name)

    calls = {"n": 0}

    def stop_in_second_table():
        # 允许:表1开始(1)、表1批次(2)、表2开始(3);表2首个批次(4)暂停
        calls["n"] += 1
        return calls["n"] <= 3

    report = incremental_sync(TwoTableAdapter(), landing, SOURCE,
                              e10_watermarks(),
                              should_continue=stop_in_second_table)
    assert report.paused
    steps = landing.steps_for_run(report.run_id)
    statuses = {s["status"] for s in steps}
    assert "ok" in statuses
    assert "paused" in statuses
    assert "failed" not in statuses  # 窗口暂停不是失败
    (run_status,) = landing.con.execute(
        "SELECT status FROM d2a_sync_run WHERE id = ?", (report.run_id,)).fetchone()
    assert run_status == "paused"


# ---- reconcile ----


def test_reconcile_writes_segment_steps(landing_synced):
    landing, _ = landing_synced
    src_adapter = SqliteReadOnlyAdapter(
        str(Path(landing.db_path).parent / "source.sqlite"),
        e10_whitelist())
    report = reconcile(src_adapter, landing, SOURCE, e10_watermarks())
    (run_id,) = landing.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run WHERE run_type = 'reconcile'").fetchone()
    steps = landing.steps_for_run(run_id)
    assert steps, "reconcile 应写 segment steps"
    for step in steps:
        assert step["kind"] == "segment"
        assert step["status"] == "ok"
        assert ":" in step["target"]  # 表:段标签


def test_reconcile_repair_error_closes_step(landing_synced, monkeypatch):
    landing, _ = landing_synced
    src_adapter = SqliteReadOnlyAdapter(
        str(Path(landing.db_path).parent / "source.sqlite"),
        e10_whitelist())
    landing.con.execute(
        f'DELETE FROM "{raw_table_name(SOURCE, "CURRENCY")}" WHERE Id = 1')
    landing.con.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("repair boom")

    monkeypatch.setattr(reconcile_mod, "_repair_full", boom)
    with pytest.raises(RuntimeError, match="repair boom"):
        reconcile(src_adapter, landing, SOURCE, e10_watermarks())
    (run_id,) = landing.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run WHERE run_type = 'reconcile'").fetchone()
    step = next(step for step in landing.steps_for_run(run_id)
                if step["target"].startswith("CURRENCY:"))
    assert step["status"] == "failed"
    assert step["finished_at"] is not None
    assert "repair boom" in step["error"]


def test_reconcile_partial_failure_keeps_completed_facts(tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    infos = [
        TableInfo(name="A", columns=[("K", "text")], pk=["K"]),
        TableInfo(name="B", columns=[("K", "text")], pk=["K"]),
    ]
    for info in infos:
        landing.ensure_raw_table(SOURCE, info)
        landing.upsert_rows(SOURCE, info, [{"K": "1"}], f"batch-{info.name}")

    class PartialFailureAdapter:
        def tables(self):
            return infos

        def table_count(self, info):
            if info.name == "B":
                raise RuntimeError("count boom")
            return 1

    with pytest.raises(RuntimeError, match="count boom"):
        reconcile(PartialFailureAdapter(), landing, SOURCE)

    run = landing.con.execute(
        "SELECT * FROM d2a_sync_run WHERE run_type = 'reconcile' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert run["status"] == "failed"
    assert run["tables"] == 1
    assert run["rows"] == 0
    steps = landing.steps_for_run(run["id"])
    assert [(s["target"], s["status"]) for s in steps] == [
        ("A:全表", "ok"),
        ("B:全表", "failed"),
    ]


def test_reconcile_monthly_stats_error_writes_failed_step(tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    info = TableInfo(name="A", columns=[("K", "text"), ("W", "text")], pk=["K"])
    landing.ensure_raw_table(SOURCE, info)
    landing.upsert_rows(
        SOURCE, info, [{"K": "1", "W": "2026-01-15 00:00:00"}], "batch-A")
    landing.set_high_water(SOURCE, "A", "W", "2026-01-15 00:00:00", "batch-A")

    class StatsFailureAdapter:
        def tables(self):
            return [info]

        def segment_stats(self, *args, **kwargs):
            raise RuntimeError("stats boom")

    with pytest.raises(RuntimeError, match="stats boom"):
        reconcile(StatsFailureAdapter(), landing, SOURCE, {"A": "W"})

    run = landing.con.execute(
        "SELECT * FROM d2a_sync_run WHERE run_type = 'reconcile' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert run["status"] == "failed"
    steps = landing.steps_for_run(run["id"])
    assert len(steps) == 1
    assert steps[0]["target"].startswith("A:")
    assert steps[0]["status"] == "failed"
    assert "stats boom" in steps[0]["error"]
