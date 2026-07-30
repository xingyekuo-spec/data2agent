"""M4-T03:统一 Run steps 写入测试(sync/apply/reconcile/ingest 生命周期)。"""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect import mapping_apply as mapping_apply_mod
from data2agent.connect import reconcile as reconcile_mod
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.mapping_apply import apply_objects
from data2agent.connect.reconcile import reconcile
from tests.helpers import whitelist_from_pack
from data2agent.ingest.app import create_app as create_ingest_app
from data2agent.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def landing_synced(tmp_path, pack):
    """seed 源库 → 一次增量同步后的落地库(带 sync steps)。"""
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    report = incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
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


def test_sync_paused_step_not_failed(tmp_path, pack):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    base = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))

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
                              watermarks_from_pack(pack, SOURCE),
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


# ---- apply ----


def test_apply_writes_object_steps(landing_synced, pack):
    landing, _ = landing_synced
    report = apply_objects(landing, pack, SOURCE)
    (run_id,) = landing.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run WHERE run_type = 'apply'").fetchone()
    steps = landing.steps_for_run(run_id)
    assert len(steps) == len(report.results)
    for step in steps:
        assert step["kind"] == "object"
        assert step["status"] == "ok"
        assert step["rows_in"] >= step["rows_out"] >= 0
    names = {s["target"] for s in steps}
    assert {"Customer", "Material", "Quotation", "SalesOrder", "SalesOrderLine"} <= names


def test_apply_circuit_writes_aborted_step(landing_synced, pack):
    landing, _ = landing_synced
    # 制造一行未知枚举 → threshold=0.0 必触发熔断
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET RESULT_STATE = \'X\' WHERE Id = 7')
    landing.con.commit()
    report = apply_objects(landing, pack, SOURCE, threshold=0.0)
    assert report.aborted
    (run_id,) = landing.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run WHERE run_type = 'apply'").fetchone()
    steps = {s["target"]: s for s in landing.steps_for_run(run_id)}
    assert steps["Quotation"]["status"] == "aborted"
    assert "隔离率" in (steps["Quotation"]["error"] or "")
    assert steps["Quotation"]["rows_in"] == 180
    assert steps["Quotation"]["rows_out"] == 179
    assert steps["Quotation"]["quarantined"] == 1
    assert steps["Quotation"]["batch_id"]
    # 其他对象不受影响
    assert steps["Customer"]["status"] == "ok"


def test_apply_unexpected_error_closes_step(landing_synced, pack, monkeypatch):
    landing, _ = landing_synced

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mapping_apply_mod, "apply_object", boom)
    with pytest.raises(RuntimeError, match="boom"):
        apply_objects(landing, pack, SOURCE)
    (run_id,) = landing.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run WHERE run_type = 'apply'").fetchone()
    run = landing.con.execute(
        "SELECT status FROM d2a_sync_run WHERE id = ?", (run_id,)).fetchone()
    step = landing.steps_for_run(run_id)[0]
    assert run["status"] == "failed"
    assert step["status"] == "failed"
    assert step["finished_at"] is not None
    assert "boom" in step["error"]


def test_apply_step_create_error_closes_run(landing_synced, pack, monkeypatch):
    landing, _ = landing_synced

    def boom(*args, **kwargs):
        raise RuntimeError("step create boom")

    monkeypatch.setattr(landing, "add_step", boom)
    with pytest.raises(RuntimeError, match="step create boom"):
        apply_objects(landing, pack, SOURCE)
    run = landing.con.execute(
        "SELECT * FROM d2a_sync_run WHERE run_type = 'apply' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert "step create boom" in run["detail"]


# ---- reconcile ----


def test_reconcile_writes_segment_steps(landing_synced, pack):
    landing, _ = landing_synced
    src_adapter = SqliteReadOnlyAdapter(
        str(Path(landing.db_path).parent / "source.sqlite"),
        whitelist_from_pack(pack, SOURCE))
    # 上面的 fixture 没保留 src 路径;用 showroom seed 重建适配器
    report = reconcile(src_adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    (run_id,) = landing.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run WHERE run_type = 'reconcile'").fetchone()
    steps = landing.steps_for_run(run_id)
    assert steps, "reconcile 应写 segment steps"
    for step in steps:
        assert step["kind"] == "segment"
        assert step["status"] == "ok"
        assert ":" in step["target"]  # 表:段标签


def test_reconcile_repair_error_closes_step(landing_synced, pack, monkeypatch):
    landing, _ = landing_synced
    src_adapter = SqliteReadOnlyAdapter(
        str(Path(landing.db_path).parent / "source.sqlite"),
        whitelist_from_pack(pack, SOURCE))
    landing.con.execute(
        f'DELETE FROM "{raw_table_name(SOURCE, "CURRENCY")}" WHERE Id = 1')
    landing.con.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("repair boom")

    monkeypatch.setattr(reconcile_mod, "_repair_full", boom)
    with pytest.raises(RuntimeError, match="repair boom"):
        reconcile(src_adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
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


# ---- ingest ----


def _batch(batch_id: str, rows: list[dict]) -> dict:
    from data2agent.protocol.ingest import INGEST_PROTOCOL_VERSION
    return {
        "ingest_protocol_version": INGEST_PROTOCOL_VERSION,
        "source": SOURCE,
        "table": "CUSTOMER",
        "mode": "incremental",
        "columns": [["CUSTOMER_CODE", "text"], ["CUSTOMER_NAME", "text"]],
        "pk": ["CUSTOMER_CODE"],
        "batch_id": batch_id,
        "rows": rows,
    }


def test_ingest_run_and_retry_correlation(tmp_path):
    landing_path = tmp_path / "landing.sqlite"
    client = TestClient(create_ingest_app(landing_path))
    rows = [{"CUSTOMER_CODE": "C-1", "CUSTOMER_NAME": "甲"}]
    r1 = client.post("/ingest/batch", json=_batch("b-001", rows))
    assert r1.status_code == 200
    # 同一 batch 重试:不产生第二个矛盾 Run
    r2 = client.post("/ingest/batch", json=_batch("b-001", rows))
    assert r2.status_code == 200

    store = LandingStore(landing_path)
    runs = store.con.execute(
        "SELECT * FROM d2a_sync_run WHERE run_type = 'ingest'").fetchall()
    assert len(runs) == 1, "同一 batch 重试不应产生重复 Run"
    steps = store.steps_for_run(runs[0]["id"])
    assert len(steps) == 1
    assert steps[0]["kind"] == "batch"
    assert steps[0]["batch_id"] == "b-001"
    assert steps[0]["status"] == "ok"
    # 不同 batch → 新 Run
    r3 = client.post("/ingest/batch", json=_batch("b-002", rows))
    assert r3.status_code == 200
    (n_runs,) = LandingStore(landing_path).con.execute(
        "SELECT COUNT(*) FROM d2a_sync_run WHERE run_type = 'ingest'").fetchone()
    assert n_runs == 2


def test_ingest_batch_correlation_includes_source_and_table(tmp_path):
    landing_path = tmp_path / "landing.sqlite"
    client = TestClient(create_ingest_app(landing_path))
    body_a = _batch("same", [{"CUSTOMER_CODE": "C-1", "CUSTOMER_NAME": "甲"}])
    body_b = {
        **body_a,
        "source": "other_source",
        "table": "CUSTOMER",
        "rows": [{"CUSTOMER_CODE": "C-1", "CUSTOMER_NAME": "乙"},
                 {"CUSTOMER_CODE": "C-2", "CUSTOMER_NAME": "丙"}],
    }
    assert client.post("/ingest/batch", json=body_a).status_code == 200
    assert client.post("/ingest/batch", json=body_b).status_code == 200

    store = LandingStore(landing_path)
    runs = store.con.execute(
        "SELECT source, rows FROM d2a_sync_run WHERE run_type = 'ingest' "
        "ORDER BY id").fetchall()
    assert [(r["source"], r["rows"]) for r in runs] == [(SOURCE, 1), ("other_source", 2)]
    steps = store.con.execute(
        "SELECT target, batch_id, rows_out FROM d2a_run_step WHERE kind = 'batch' "
        "ORDER BY id").fetchall()
    assert [(s["target"], s["batch_id"], s["rows_out"]) for s in steps] == [
        (raw_table_name(SOURCE, "CUSTOMER"), "same", 1),
        (raw_table_name("other_source", "CUSTOMER"), "same", 2),
    ]


def test_ingest_observation_failure_closes_started_run(tmp_path, monkeypatch):
    landing_path = tmp_path / "landing.sqlite"
    client = TestClient(create_ingest_app(landing_path))

    def boom(*args, **kwargs):
        raise RuntimeError("step boom")

    monkeypatch.setattr(LandingStore, "add_step", boom)
    r = client.post(
        "/ingest/batch",
        json=_batch("b-observe-fail", [{"CUSTOMER_CODE": "C-1", "CUSTOMER_NAME": "甲"}]),
    )
    assert r.status_code == 500
    store = LandingStore(landing_path)
    (raw_count,) = store.con.execute(
        f'SELECT COUNT(*) FROM "{raw_table_name(SOURCE, "CUSTOMER")}"').fetchone()
    assert raw_count == 1
    run = store.con.execute(
        "SELECT * FROM d2a_sync_run WHERE run_type = 'ingest' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert "observation failed" in run["detail"]


def test_ingest_audit_failure_closes_new_run_as_failed(tmp_path, monkeypatch):
    landing_path = tmp_path / "landing.sqlite"
    client = TestClient(create_ingest_app(landing_path))

    def boom(*args, **kwargs):
        raise RuntimeError("audit boom")

    monkeypatch.setattr(LandingStore, "log_audit", boom)
    r = client.post(
        "/ingest/batch",
        json=_batch("b-audit-fail", [{"CUSTOMER_CODE": "C-1", "CUSTOMER_NAME": "甲"}]),
    )
    assert r.status_code == 500
    store = LandingStore(landing_path)
    run = store.con.execute(
        "SELECT * FROM d2a_sync_run WHERE run_type = 'ingest' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert run["status"] == "failed"
    step = store.steps_for_run(run["id"])[0]
    assert step["status"] == "failed"
    assert "audit boom" in step["error"]


def test_ingest_retry_audit_failure_keeps_prior_success(tmp_path, monkeypatch):
    landing_path = tmp_path / "landing.sqlite"
    client = TestClient(create_ingest_app(landing_path))
    rows = [{"CUSTOMER_CODE": "C-1", "CUSTOMER_NAME": "甲"}]
    assert client.post("/ingest/batch", json=_batch("b-ok", rows)).status_code == 200

    def boom(*args, **kwargs):
        raise RuntimeError("retry audit boom")

    monkeypatch.setattr(LandingStore, "log_audit", boom)
    r = client.post("/ingest/batch", json=_batch("b-ok", rows))
    assert r.status_code == 500

    store = LandingStore(landing_path)
    runs = store.con.execute(
        "SELECT * FROM d2a_sync_run WHERE run_type = 'ingest'").fetchall()
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    steps = store.steps_for_run(runs[0]["id"])
    assert len(steps) == 1
    assert steps[0]["status"] == "ok"
