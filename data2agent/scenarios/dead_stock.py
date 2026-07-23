"""呆滞库存 M1 预计算（基于已核对的 E10 字段）。

现场资料目前只确认了库存余额、最后出库/入库日期等字段，未确认物料编码、
库存状态、单位成本、原始快照日和跨表关系。因而本实现只发布可核验的库存
事实；所有呆滞判定保持 ``unknown``，不能把资料中不存在的字段当作事实。
"""

from __future__ import annotations

from datetime import date

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.landing import LandingStore, raw_table_name

from .e10_dead_stock_schema import DEAD_STOCK_M1_COLUMNS

RESULT_TABLE = "D2A_DEAD_STOCK_ITEM"
CALCULATION_VERSION = "dead-stock-v2-verified-table-fields"
DEFAULT_THRESHOLD_DAYS = 90

_RESULT_INFO = TableInfo(
    name=RESULT_TABLE,
    columns=[
        ("item_code", "text"), ("plant_id", "text"), ("warehouse_code", "text"),
        ("item_name", "text"), ("specification", "text"), ("material_type", "text"),
        ("inventory_qty", "real"), ("unit_cost", "real"), ("dead_stock_amount", "real"),
        ("last_issue_date", "text"), ("last_receipt_date", "text"),
        ("first_stock_in_date", "text"), ("age_anchor_date", "text"),
        ("dead_stock_days", "int"), ("threshold_days", "int"),
        ("determination_status", "text"), ("inventory_status", "text"),
        ("as_of_date", "text"), ("calculation_version", "text"),
    ],
    pk=["plant_id", "warehouse_code", "item_code"],
)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _active_table(store: LandingStore, source: str, logical_table: str) -> str:
    table = raw_table_name(source, logical_table)
    exists = store.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()
    if exists is None:
        raise ValueError(f"dead_stock_item_v2 缺少已同步原始表: {logical_table}")
    return _quoted(table)


def _require_columns(store: LandingStore, source: str, logical_table: str, required: frozenset[str]) -> None:
    physical = raw_table_name(source, logical_table)
    columns = {str(row["name"]) for row in store.con.execute(f"PRAGMA table_info({_quoted(physical)})")}
    missing = sorted(required - columns)
    if missing:
        raise ValueError(
            f"dead_stock_item_v2 原始表 {logical_table} 缺少已核对字段: {', '.join(missing)}",
        )


def _extracted_as_of(store: LandingStore, warehouse: str) -> date:
    row = store.con.execute(
        f"SELECT MAX(SUBSTR(_d2a_extracted_at, 1, 10)) AS as_of_date "
        f"FROM {warehouse} WHERE _d2a_deleted_at IS NULL",
    ).fetchone()
    as_of = _parse_date(row["as_of_date"])
    if as_of is None:
        raise ValueError("dead_stock_item_v2 无法从平台抽取时间确定数据日期")
    return as_of


def materialize_dead_stock_item(store: LandingStore, source: str) -> int:
    """物化已核对的仓库库存事实，缺少口径时保持 unknown。"""
    warehouse = _active_table(store, source, "ITEM_WAREHOUSE")
    _require_columns(store, source, "ITEM_WAREHOUSE", DEAD_STOCK_M1_COLUMNS)
    as_of = _extracted_as_of(store, warehouse)

    duplicate = store.con.execute(
        f"""SELECT ITEM_ID, WAREHOUSE_ID, COUNT(*) AS count
            FROM {warehouse} WHERE _d2a_deleted_at IS NULL
            GROUP BY ITEM_ID, WAREHOUSE_ID HAVING COUNT(*) > 1 LIMIT 1""",
    ).fetchone()
    if duplicate is not None:
        raise ValueError(
            "dead_stock_item_v2 无法确定 ITEM_WAREHOUSE 的业务主键；"
            "请补充同一 ITEM_ID/WAREHOUSE_ID 多行时的主键和余额口径",
        )

    rows: list[dict] = []
    for row in store.con.execute(
        f"""SELECT ITEM_ID, WAREHOUSE_ID, INVENTORY_QTY, LAST_ISSUE_DATE, LAST_RECEIPT_DATE
            FROM {warehouse} WHERE _d2a_deleted_at IS NULL""",
    ):
        last_issue = _parse_date(row["LAST_ISSUE_DATE"])
        last_receipt = _parse_date(row["LAST_RECEIPT_DATE"])
        # 首次入库日及库存状态尚未核对；不能以最后入库日替代，也不能据此判呆滞。
        dead_stock_days = (as_of - last_issue).days if last_issue is not None else None
        rows.append({
            "item_code": str(row["ITEM_ID"]),
            "plant_id": "unknown",
            "warehouse_code": str(row["WAREHOUSE_ID"]),
            "item_name": None, "specification": None, "material_type": None,
            "inventory_qty": float(row["INVENTORY_QTY"] or 0),
            "unit_cost": None, "dead_stock_amount": None,
            "last_issue_date": last_issue.isoformat() if last_issue else None,
            "last_receipt_date": last_receipt.isoformat() if last_receipt else None,
            "first_stock_in_date": None,
            "age_anchor_date": last_issue.isoformat() if last_issue else None,
            "dead_stock_days": dead_stock_days,
            "threshold_days": DEFAULT_THRESHOLD_DAYS,
            "determination_status": "unknown",
            "inventory_status": "unknown",
            "as_of_date": as_of.isoformat(),
            "calculation_version": CALCULATION_VERSION,
        })

    store.ensure_raw_table(source, _RESULT_INFO)
    stage = _quoted(raw_table_name(source, RESULT_TABLE))
    store.con.execute(f"UPDATE {stage} SET _d2a_deleted_at = CURRENT_TIMESTAMP")
    store.con.commit()
    store.upsert_rows(source, _RESULT_INFO, rows, f"mat_{as_of:%Y%m%d}")
    return len(rows)
