"""已核对 E10 字段下的呆滞库存 M1 行为。"""

from __future__ import annotations

from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.base import TableInfo
from data2agent.shared.store.landing import LandingStore, raw_table_name
from data2agent.shared.metamodel.loader import load_pack
from data2agent.shared.scenarios.dead_stock import RESULT_TABLE, materialize_dead_stock_item

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"


def _warehouse_info(*, include_receipt_date: bool = True) -> TableInfo:
    columns = [
        ("ITEM_ID", "text"), ("WAREHOUSE_ID", "text"),
        ("INVENTORY_QTY", "real"), ("LAST_ISSUE_DATE", "text"),
    ]
    if include_receipt_date:
        columns.append(("LAST_RECEIPT_DATE", "text"))
    return TableInfo(name="ITEM_WAREHOUSE", columns=columns, pk=["ITEM_ID", "WAREHOUSE_ID"])


def _landing(tmp_path: Path, *, include_receipt_date: bool = True) -> LandingStore:
    landing = LandingStore(tmp_path / "landing.sqlite")
    info = _warehouse_info(include_receipt_date=include_receipt_date)
    landing.ensure_raw_table(SOURCE, info)
    row = {
        "ITEM_ID": "MAT-001", "WAREHOUSE_ID": "WH-A", "INVENTORY_QTY": 24,
        "LAST_ISSUE_DATE": "2026-03-01", "LAST_RECEIPT_DATE": "2026-06-15",
    }
    if not include_receipt_date:
        row.pop("LAST_RECEIPT_DATE")
    landing.upsert_rows(SOURCE, info, [row], "verified-input")
    return landing


def test_materializer_publishes_only_verified_inventory_facts(tmp_path: Path) -> None:
    landing = _landing(tmp_path)

    assert materialize_dead_stock_item(landing, SOURCE) == 1

    table = raw_table_name(SOURCE, RESULT_TABLE)
    row = landing.con.execute(
        f'SELECT * FROM "{table}" WHERE _d2a_deleted_at IS NULL',
    ).fetchone()
    assert row is not None
    assert row["item_code"] == "MAT-001"
    assert row["warehouse_code"] == "WH-A"
    assert row["last_issue_date"] == "2026-03-01"
    assert row["last_receipt_date"] == "2026-06-15"
    assert row["dead_stock_days"] is not None
    assert row["determination_status"] == "unknown"
    assert row["unit_cost"] is None and row["dead_stock_amount"] is None


def test_materializer_rejects_missing_verified_field(tmp_path: Path) -> None:
    landing = _landing(tmp_path, include_receipt_date=False)

    with pytest.raises(ValueError, match="LAST_RECEIPT_DATE"):
        materialize_dead_stock_item(landing, SOURCE)


def test_template_uses_only_verified_m1_input_table() -> None:
    pack = load_pack(ROOT / "templates")
    binding = next(obj for obj in pack.objects if obj.object == "DeadStockItem").bindings[0]

    assert binding.tables == [RESULT_TABLE, "ITEM_WAREHOUSE"]
