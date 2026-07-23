"""呆滞库存 M1 预计算。

本模块只处理稳定、确定性的库存账龄和金额。采购超采与生产损耗归因在 M2
进入独立证据对象，避免把复杂规则塞进 YAML field_map。
"""

from __future__ import annotations

from datetime import date, datetime

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.landing import LandingStore, raw_table_name

RESULT_TABLE = "D2A_DEAD_STOCK_ITEM"
CALCULATION_VERSION = "dead-stock-v1"
DEFAULT_THRESHOLD_DAYS = 90

_RESULT_INFO = TableInfo(
    name=RESULT_TABLE,
    columns=[
        ("item_code", "text"),
        ("plant_id", "text"),
        ("warehouse_code", "text"),
        ("item_name", "text"),
        ("specification", "text"),
        ("material_type", "text"),
        ("inventory_qty", "real"),
        ("unit_cost", "real"),
        ("dead_stock_amount", "real"),
        ("last_issue_date", "text"),
        ("first_stock_in_date", "text"),
        ("age_anchor_date", "text"),
        ("dead_stock_days", "int"),
        ("threshold_days", "int"),
        ("determination_status", "text"),
        ("inventory_status", "text"),
        ("as_of_date", "text"),
        ("calculation_version", "text"),
    ],
    pk=["plant_id", "warehouse_code", "item_code"],
)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _active_table(store: LandingStore, source: str, logical_table: str) -> str:
    table = raw_table_name(source, logical_table)
    exists = store.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()
    if exists is None:
        raise ValueError(f"dead_stock_item_v1 缺少已同步原始表: {logical_table}")
    return _quoted(table)


def _latest_dates(store: LandingStore, sql: str) -> dict[int, date]:
    out: dict[int, date] = {}
    for row in store.con.execute(sql):
        parsed = _parse_date(row["event_date"])
        if parsed is not None:
            out[int(row["ITEM_ID"])] = parsed
    return out


def materialize_dead_stock_item(store: LandingStore, source: str) -> int:
    """从已同步 E10 raw 表生成 M1 内部结果表，返回活跃结果行数。"""
    item = _active_table(store, source, "ITEM")
    balance = _active_table(store, source, "INV_COST_BAL")
    cost = _active_table(store, source, "INV_UNIT_COST")
    receipt = _active_table(store, source, "INV_RECEIPT")
    sales_issue = _active_table(store, source, "SALES_ISSUE")
    sales_issue_d = _active_table(store, source, "SALES_ISSUE_D")
    mo_issued = _active_table(store, source, "MO_ISSUED_SETS")

    as_of_raw = store.con.execute(
        f"SELECT MAX(SUBSTR(LAST_MODIFIED_DATE, 1, 10)) AS as_of_date "
        f"FROM {balance} WHERE _d2a_deleted_at IS NULL"
    ).fetchone()["as_of_date"]
    as_of = _parse_date(as_of_raw)
    if as_of is None:
        raise ValueError("dead_stock_item_v1 无法确定库存快照日期")

    sales_dates = _latest_dates(
        store,
        f"""
        SELECT d.ITEM_ID, MAX(h.DOC_DATE) AS event_date
        FROM {sales_issue_d} d
        JOIN {sales_issue} h ON h.Id = d.SALES_ISSUE_ID
        WHERE d._d2a_deleted_at IS NULL AND h._d2a_deleted_at IS NULL
        GROUP BY d.ITEM_ID
        """,
    )
    production_dates = _latest_dates(
        store,
        f"""
        SELECT ITEM_ID, MAX(ISSUE_DATE) AS event_date
        FROM {mo_issued}
        WHERE _d2a_deleted_at IS NULL
        GROUP BY ITEM_ID
        """,
    )
    first_receipts = _latest_dates(
        store,
        f"""
        SELECT ITEM_ID, MIN(RECEIPT_DATE) AS event_date
        FROM {receipt}
        WHERE _d2a_deleted_at IS NULL
        GROUP BY ITEM_ID
        """,
    )

    rows: list[dict] = []
    for row in store.con.execute(
        f"""
        SELECT b.ITEM_ID, b.PLANT_ID, b.WAREHOUSE_CODE, b.INVENTORY_QTY,
               b.INVENTORY_STATUS, i.ITEM_CODE, i.ITEM_NAME,
               i.ITEM_SPECIFICATION, i.CATEGORY_CODE, c.UNIT_COST
        FROM {balance} b
        JOIN {item} i ON i.Id = b.ITEM_ID AND i._d2a_deleted_at IS NULL
        LEFT JOIN {cost} c ON c.ITEM_ID = b.ITEM_ID AND c._d2a_deleted_at IS NULL
        WHERE b._d2a_deleted_at IS NULL
        """
    ):
        item_id = int(row["ITEM_ID"])
        issue_candidates = [
            value for value in (sales_dates.get(item_id), production_dates.get(item_id))
            if value is not None
        ]
        last_issue = max(issue_candidates) if issue_candidates else None
        first_receipt = first_receipts.get(item_id)
        anchor = last_issue or first_receipt
        inventory_qty = float(row["INVENTORY_QTY"] or 0)
        unit_cost = float(row["UNIT_COST"] or 0)
        inventory_status = str(row["INVENTORY_STATUS"] or "unknown")
        dead_stock_days = (as_of - anchor).days if anchor is not None else None

        if inventory_status != "usable" or anchor is None:
            determination = "unknown"
        elif inventory_qty > 0 and dead_stock_days is not None and dead_stock_days > DEFAULT_THRESHOLD_DAYS:
            determination = "dead_stock"
        else:
            determination = "active"

        rows.append({
            "item_code": row["ITEM_CODE"],
            "plant_id": row["PLANT_ID"],
            "warehouse_code": row["WAREHOUSE_CODE"],
            "item_name": row["ITEM_NAME"],
            "specification": row["ITEM_SPECIFICATION"],
            "material_type": row["CATEGORY_CODE"],
            "inventory_qty": inventory_qty,
            "unit_cost": unit_cost,
            "dead_stock_amount": round(inventory_qty * unit_cost, 2),
            "last_issue_date": last_issue.isoformat() if last_issue else None,
            "first_stock_in_date": first_receipt.isoformat() if first_receipt else None,
            "age_anchor_date": anchor.isoformat() if anchor else None,
            "dead_stock_days": dead_stock_days,
            "threshold_days": DEFAULT_THRESHOLD_DAYS,
            "determination_status": determination,
            "inventory_status": inventory_status,
            "as_of_date": as_of.isoformat(),
            "calculation_version": CALCULATION_VERSION,
        })

    store.ensure_raw_table(source, _RESULT_INFO)
    stage = _quoted(raw_table_name(source, RESULT_TABLE))
    store.con.execute(f"UPDATE {stage} SET _d2a_deleted_at = CURRENT_TIMESTAMP")
    store.con.commit()
    store.upsert_rows(source, _RESULT_INFO, rows, f"mat_{as_of:%Y%m%d}")
    return len(rows)
