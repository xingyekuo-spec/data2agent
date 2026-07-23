"""呆滞库存 M2: R2/R2M、R5 证据与归因的端到端测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset, resolve_published_snapshot
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.sync import whitelist_from_pack
from data2agent.mcp_server.core import MASK, QueryService
from data2agent.mcp_server.evidence import EvidenceContext
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


def _ctx() -> EvidenceContext:
    return EvidenceContext(
        principal="test:dead-stock-m2",
        session_id="test_session_dead_stock_m2_0001",
        channel="demo",
    )


def _published(tmp_path: Path) -> LandingStore:
    source_db = tmp_path / "e10.sqlite"
    write_db(source_db, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    whitelist = whitelist_from_pack(pack, SOURCE)
    assert not {table for table in whitelist if table.startswith("D2A_")}
    assert {"PURCHASE_ORDER", "PURCHASE_ORDER_SSD", "PURCHASE_ARRIVAL_D", "MO", "MO_D", "BOM_D"} <= whitelist
    adapter = SqliteReadOnlyAdapter(str(source_db), whitelist)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    return landing


def test_m2_materializes_r2_r2m_r5_evidence_and_attributions(tmp_path):
    landing = _published(tmp_path)
    snap = resolve_published_snapshot(landing, SOURCE)
    assert {"DeadStockAttribution", "PurchaseOverbuyEvidence", "ProductionLossEvidence"} <= set(snap.objects)

    purchase_stage = raw_table_name(SOURCE, "D2A_PURCHASE_OVERBUY_EVIDENCE")
    forced = landing.con.execute(
        f'''SELECT * FROM "{purchase_stage}"
            WHERE moq_forced_excess_qty > 0 AND calculation_status = 'ready' LIMIT 1'''
    ).fetchone()
    manual = landing.con.execute(
        f'''SELECT * FROM "{purchase_stage}"
            WHERE manual_excess_qty > 0 AND calculation_status = 'ready' LIMIT 1'''
    ).fetchone()
    assert forced is not None and manual is not None
    assert forced["trace_type"] == manual["trace_type"] == "indirect"

    production_stage = raw_table_name(SOURCE, "D2A_PRODUCTION_LOSS_EVIDENCE")
    r5 = landing.con.execute(
        f'''SELECT * FROM "{production_stage}"
            WHERE calculation_status = 'ready' AND excess_issue_qty > 0 LIMIT 1'''
    ).fetchone()
    provisional = landing.con.execute(
        f'''SELECT * FROM "{production_stage}"
            WHERE calculation_status = 'provisional' LIMIT 1'''
    ).fetchone()
    assert r5 is not None and provisional is not None
    assert r5["net_issued_qty"] > r5["allowed_issue_qty"]

    attribution = snap.objects["DeadStockAttribution"]
    root_causes = {
        row["root_cause"] for row in landing.con.execute(
            f'SELECT root_cause FROM "{attribution.physical_table}"'
        )
    }
    assert {"R2", "R2M", "R5"} <= root_causes
    assert not landing.con.execute(
        f'''SELECT 1 FROM "{attribution.physical_table}"
            WHERE evidence_id = ? LIMIT 1''', (provisional["mo_no"],)
    ).fetchone()


def test_mcp_exposes_m2_objects_masks_employee_and_aggregates_metrics(tmp_path):
    landing = _published(tmp_path)
    svc = QueryService(landing.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx())

    rows = svc.query_objects(
        "DeadStockAttribution", filters={"confidence_level": "MEDIUM"},
        order_by="confidence", desc=True, limit=50,
    )
    assert rows["rows"]
    assert rows["meta"]["masked_fields"] == ["related_employee"]
    assert {row["root_cause"] for row in rows["rows"]} >= {"R2", "R2M", "R5"}
    assert all(row["related_employee"] == MASK for row in rows["rows"])
    assert all(row["trace_type"] == "indirect" for row in rows["rows"])

    purchase = svc.query_objects("PurchaseOverbuyEvidence", filters={"calculation_status": "ready"}, limit=50)
    production = svc.query_objects("ProductionLossEvidence", limit=50)
    assert purchase["rows"] and production["rows"]
    assert all(row["related_employee"] == MASK for row in purchase["rows"] + production["rows"])

    coverage = svc.query_metrics("attribution_coverage_rate", group_by="工厂", limit=10)
    distribution = svc.query_metrics("attribution_distribution", group_by="根因", limit=10)
    assert coverage["implemented"] and 0 < coverage["rows"][0]["value"] <= 1
    assert distribution["implemented"]
    assert {row["group"] for row in distribution["rows"]} >= {"R2", "R2M", "R5"}
