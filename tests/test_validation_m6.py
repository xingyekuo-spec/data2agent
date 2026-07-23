"""M6 Validation Run：契约、不可变存储、只读编排与下载。"""

from datetime import date
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.sync import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import ValidationReportResponse
from data2agent.console.validation import build_validation_report
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def published_landing(tmp_path: Path) -> LandingStore:
    source = tmp_path / "source.sqlite"
    write_db(source, build(seed=7, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(source), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    return landing


def test_validation_storage_is_immutable_and_atomic(published_landing: LandingStore):
    db = published_landing
    run_id = db.start_run(SOURCE, "validation", commit=False)
    report = {
        "report_schema_version": 1, "run_id": run_id, "source": SOURCE,
        "overall_status": "pass", "started_at": "2026-07-22T00:00:00+00:00",
        "finished_at": "2026-07-22T00:00:01+00:00", "deployment": {},
        "dataset_version": None, "template_version": "v", "summary": {},
        "checks": [{
            "check_id": "service_reachable", "title": "服务", "status": "pass",
            "blocking": True, "summary": "正常", "started_at": "2026-07-22T00:00:00+00:00",
            "finished_at": "2026-07-22T00:00:01+00:00", "detail": {}, "evidence": [],
        }],
    }
    db.finish_run(run_id, tables=0, rows=1, commit=False)
    db.insert_validation_report(report, report["checks"], commit=False)
    db.con.commit()
    assert db.get_validation_report(run_id) == report
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.con.execute("UPDATE d2a_validation_report SET source = 'x' WHERE run_id = ?", (run_id,))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.con.execute("DELETE FROM d2a_validation_check WHERE run_id = ?", (run_id,))


def test_validation_api_writes_one_frozen_report_and_download_matches(published_landing: LandingStore):
    client = TestClient(create_app(published_landing.db_path, ROOT / "templates"))
    before = published_landing.con.execute(
        "SELECT COUNT(*) FROM d2a_dataset_version"
    ).fetchone()[0]
    started = client.post("/api/validation/run", json={"include_mcp_probe": False})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    detail = client.get(f"/api/validation/runs/{run_id}")
    assert detail.status_code == 200
    report = ValidationReportResponse.model_validate(detail.json())
    assert report.run_id == run_id
    assert [check.check_id for check in report.checks] == [
        "service_reachable", "source_connectivity", "readonly_whitelist",
        "sync_execution", "landing_and_push", "raw_presence", "published_dataset",
        "quarantine_breaker", "mapping_preview", "mcp_query", "masking",
        "evidence_integrity", "cross_surface_consistency",
    ]
    download = client.get(f"/api/validation/runs/{run_id}/report.json")
    assert download.status_code == 200
    assert download.json() == detail.json()
    assert "data2agent-validation-" in download.headers["content-disposition"]
    after = published_landing.con.execute(
        "SELECT COUNT(*) FROM d2a_dataset_version"
    ).fetchone()[0]
    assert after == before
    run = LandingStore(published_landing.db_path).con.execute(
        "SELECT run_type, status, rows FROM d2a_sync_run WHERE id = ?", (run_id,)
    ).fetchone()
    assert tuple(run) == ("validation", "ok", 13)


def test_validation_api_rejects_unknown_and_invalid_request(published_landing: LandingStore):
    client = TestClient(create_app(published_landing.db_path, ROOT / "templates"))
    missing = client.get("/api/validation/runs/99999")
    assert missing.status_code == 404
    assert missing.json()["reason_code"] == "validation_not_found"
    invalid = client.post("/api/validation/run", json={"source": "attacker"})
    assert invalid.status_code == 422


def test_validation_runs_live_mcp_probe_without_creating_evidence(published_landing: LandingStore):
    """M6 不能以历史 evidence 冒充当前 MCP 可用，也不能把健康检查写入 M5。"""
    client = TestClient(create_app(published_landing.db_path, ROOT / "templates"))
    before = published_landing.con.execute(
        "SELECT COUNT(*) FROM d2a_gateway_query_evidence"
    ).fetchone()[0]
    started = client.post("/api/validation/run", json={"include_mcp_probe": True})
    assert started.status_code == 200, started.text
    report = client.get(f"/api/validation/runs/{started.json()['run_id']}").json()
    checks = {item["check_id"]: item for item in report["checks"]}
    assert checks["mcp_query"]["status"] == "pass"
    assert checks["masking"]["status"] == "pass"
    after = LandingStore(published_landing.db_path).con.execute(
        "SELECT COUNT(*) FROM d2a_gateway_query_evidence"
    ).fetchone()[0]
    assert after == before


def test_validation_masking_fails_when_live_probe_returns_plain_sensitive_value(
    published_landing: LandingStore,
):
    pack = load_pack(ROOT / "templates")
    run_id = published_landing.start_run(SOURCE, "validation", commit=False)

    def leaking_probe(_object: str) -> dict:
        return {
            "rows": [{"contact": "plain-sensitive-value"}],
            "meta": {
                "query_id": None,
                "source": SOURCE,
                "dataset_version": published_landing.get_published_dataset(SOURCE).dataset_version,
                "template_version": pack.version,
                "masked_fields": ["contact"],
            },
        }

    report = build_validation_report(
        published_landing, run_id=run_id, pack=pack, source=SOURCE,
        config=None, include_mcp_probe=True, mcp_probe=leaking_probe,
    )
    checks = {item["check_id"]: item for item in report["checks"]}
    assert checks["mcp_query"]["status"] == "pass"
    assert checks["masking"]["status"] == "fail"
    published_landing.con.rollback()


class TestNextExpectedRun:
    """_next_expected_run 窗口感知计算。"""

    def _cfg(self, sync_every="30m", windows=None):
        from data2agent.connect.config import SourceConfig
        return SourceConfig(
            adapter="sqlite_readonly", path="x",
            tables={"CUSTOMER": {"mode": "incremental", "watermark": "UPD"}},
            sync_every=sync_every, windows=windows or [],
        )

    def test_no_windows_next_is_last_plus_sync_every(self):
        from data2agent.console.validation import _next_expected_run
        from datetime import datetime
        cfg = self._cfg(sync_every="30m")
        result = _next_expected_run(datetime(2026, 7, 23, 10, 0, 0), cfg)
        assert result == datetime(2026, 7, 23, 10, 30, 0)

    def test_window_skips_to_opening(self):
        from data2agent.console.validation import _next_expected_run
        from datetime import datetime
        cfg = self._cfg(sync_every="30m", windows=["22:00-06:30"])
        # 上次 06:20 → candidate 06:50 → 不在窗口 → 步进到 22:00
        result = _next_expected_run(datetime(2026, 7, 23, 6, 20, 0), cfg)
        assert result == datetime(2026, 7, 23, 22, 0, 0)

    def test_window_mid_run_stays(self):
        from data2agent.console.validation import _next_expected_run
        from datetime import datetime
        cfg = self._cfg(sync_every="30m", windows=["22:00-06:30"])
        # 上次 23:00 → candidate 23:30 → 仍在窗口
        result = _next_expected_run(datetime(2026, 7, 23, 23, 0, 0), cfg)
        assert result == datetime(2026, 7, 23, 23, 30, 0)

    def test_long_sync_every_not_zero_loop(self):
        from data2agent.console.validation import _next_expected_run
        from datetime import datetime
        cfg = self._cfg(sync_every="8d", windows=["22:00-06:30"])
        # 上次 7/23 08:00(窗口外) → candidate 7/31 08:00 → 不在窗口 → 跳到 22:00
        result = _next_expected_run(datetime(2026, 7, 23, 8, 0, 0), cfg)
        assert result == datetime(2026, 7, 31, 22, 0, 0)

class TestRawPresence:
    """raw_presence 检查:存在性与诚实局限性。"""

    def test_recent_sync_passes(self, published_landing):
        from data2agent.console.validation import build_validation_report
        pack = load_pack(ROOT / "templates")
        run_id = published_landing.start_run(SOURCE, "sync")
        # 为全部 6 张表创建 step 记录
        tables = ["CUSTOMER", "CURRENCY", "ITEM", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]
        for i, tbl in enumerate(tables):
            sid = published_landing.add_step(run_id, i + 1, "table", tbl)
            published_landing.update_step(sid, status="ok", rows_in=100, rows_out=100)
        published_landing.finish_run(run_id, tables=6, rows=600, status="ok")

        report = build_validation_report(
            published_landing, run_id=run_id, pack=pack, source=SOURCE,
            config=None, include_mcp_probe=False,
        )
        checks = {c["check_id"]: c for c in report["checks"]}
        assert checks["raw_presence"]["status"] == "pass"

    def test_table_without_sync_record_is_unverified(self, tmp_path):
        """缺少同步记录的表应报告 warning,不能假装正常。"""
        from data2agent.console.validation import build_validation_report
        pack = load_pack(ROOT / "templates")
        landing = LandingStore(tmp_path / "landing.sqlite")

        # 创建一个 run,只为 5 张表写 step(排除 CURRENCY)
        run_id = landing.start_run(SOURCE, "sync")
        for i, tbl in enumerate(["CUSTOMER", "ITEM", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]):
            sid = landing.add_step(run_id, i + 1, "table", tbl)
            landing.update_step(sid, status="ok", rows_in=100, rows_out=100)
        landing.finish_run(run_id, tables=5, rows=500, status="ok")

        # 确保 raw 表存在(创建空表)
        for tbl in ["CUSTOMER", "CURRENCY", "ITEM", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]:
            landing.ensure_raw_table(SOURCE, TableInfo(tbl, [("id", "int")], ["id"]))

        report = build_validation_report(
            landing, run_id=run_id, pack=pack, source=SOURCE,
            config=None, include_mcp_probe=False,
        )
        checks = {c["check_id"]: c for c in report["checks"]}
        assert checks["raw_presence"]["status"] == "warning"
        assert "CURRENCY" in checks["raw_presence"]["detail"].get("unverified", [])
        landing.con.rollback()

    def test_other_source_does_not_pollute(self, tmp_path):
        """其他数据源的 step 记录不影响当前源的逐表新鲜度。"""
        from data2agent.console.validation import build_validation_report
        pack = load_pack(ROOT / "templates")
        landing = LandingStore(tmp_path / "landing.sqlite")

        # 为 6 张表创建 raw 空表
        tables = ["CUSTOMER", "CURRENCY", "ITEM", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]
        for tbl in tables:
            landing.ensure_raw_table(SOURCE, TableInfo(tbl, [("id", "int")], ["id"]))

        # 当前源:6 张表都有最近 step 记录
        run_id = landing.start_run(SOURCE, "sync")
        for i, tbl in enumerate(tables):
            sid = landing.add_step(run_id, i + 1, "table", tbl)
            landing.update_step(sid, status="ok", rows_in=100, rows_out=100)
        landing.finish_run(run_id, tables=6, rows=600, status="ok")

        # 另一个源有同名表但过期
        other = landing.start_run("other_source", "sync")
        for i, tbl in enumerate(["CUSTOMER", "CURRENCY"]):
            sid = landing.add_step(other, i + 1, "table", tbl)
            landing.update_step(sid, status="ok", rows_in=100, rows_out=100)
        landing.finish_run(other, tables=2, rows=200, status="ok")
        # 把 other_source 的 step finished_at 改到过去
        landing.con.execute(
            "UPDATE d2a_run_step SET finished_at = '2020-01-01T00:00:00' WHERE run_id = ?",
            (other,),
        )
        landing.con.commit()

        report = build_validation_report(
            landing, run_id=run_id, pack=pack, source=SOURCE,
            config=None, include_mcp_probe=False,
        )
        checks = {c["check_id"]: c for c in report["checks"]}
        # 当前源有完整记录 → pass(不被 other_source 污染)
        assert checks["raw_presence"]["status"] == "pass"
        landing.con.rollback()
