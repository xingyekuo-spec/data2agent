"""呆滞库存 M1: raw E10-like → materializer → published → MCP。"""

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
        principal="test:dead-stock-m1",
        session_id="test_session_dead_stock_m1_0001",
        channel="demo",
    )


def _published(tmp_path: Path) -> tuple[LandingStore, object]:
    source_db = tmp_path / "e10.sqlite"
    write_db(source_db, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    whitelist = whitelist_from_pack(pack, SOURCE)
    assert "D2A_DEAD_STOCK_ITEM" not in whitelist
    assert {"INV_COST_BAL", "INV_RECEIPT", "SALES_ISSUE_D", "MO_ISSUED_SETS"} <= whitelist
    adapter = SqliteReadOnlyAdapter(str(source_db), whitelist)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published and result.dataset_version
    return landing, pack


def test_materializer_publishes_dead_stock_item_and_uses_receipt_fallback(tmp_path):
    landing, _ = _published(tmp_path)
    snap = resolve_published_snapshot(landing, SOURCE)
    entry = snap.objects["DeadStockItem"]
    assert entry.row_count > 0

    stage = raw_table_name(SOURCE, "D2A_DEAD_STOCK_ITEM")
    never_issued = landing.con.execute(
        f'''SELECT * FROM "{stage}"
            WHERE last_issue_date IS NULL AND first_stock_in_date IS NOT NULL
              AND _d2a_deleted_at IS NULL
            LIMIT 1'''
    ).fetchone()
    assert never_issued is not None
    assert never_issued["age_anchor_date"] == never_issued["first_stock_in_date"]
    assert never_issued["determination_status"] in {"active", "dead_stock", "unknown"}

    published = landing.con.execute(
        f'''SELECT COUNT(*) AS n FROM "{entry.physical_table}"
            WHERE determination_status = 'dead_stock' ''').fetchone()["n"]
    assert published > 0


def test_mcp_reads_published_dead_stock_object_and_metrics(tmp_path):
    landing, _ = _published(tmp_path)
    svc = QueryService(landing.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx())

    rows = svc.query_objects(
        "DeadStockItem",
        filters={"determination_status": "dead_stock", "plant_id": "P01"},
        order_by="dead_stock_days",
        desc=True,
        limit=200,
    )
    assert rows["rows"]
    assert rows["meta"]["dataset_version"]
    assert rows["meta"]["masked_fields"] == ["dead_stock_amount", "unit_cost"]
    assert all(row["unit_cost"] == MASK and row["dead_stock_amount"] == MASK for row in rows["rows"])
    assert all(row["dead_stock_days"] > row["threshold_days"] for row in rows["rows"])

    amount = svc.query_metrics("dead_stock_amount", group_by="工厂", limit=10)
    quantity = svc.query_metrics("dead_stock_quantity", group_by="工厂", limit=10)
    count = svc.query_metrics("dead_stock_item_count", group_by="工厂", limit=10)
    assert amount["implemented"] and amount["unit"] == "CNY"
    assert amount["rows"][0]["group"] == "P01" and amount["rows"][0]["value"] > 0
    assert quantity["rows"][0]["value"] > 0
    assert count["rows"][0]["value"] > 0
    assert amount["meta"]["dataset_version"] == rows["meta"]["dataset_version"]
