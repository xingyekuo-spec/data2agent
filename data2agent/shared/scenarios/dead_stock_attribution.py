"""呆滞库存 M2 内部结果表。

现有现场资料未确认采购、收货、需求来源、供应商与库存之间的关联字段，且未
提供 MOQ 字段。因此只创建空证据集；不得使用 E10-like 参考库的假定关联。
"""

from __future__ import annotations

from datetime import date

from ..store.table import TableInfo
from ..store.landing import LandingStore, raw_table_name

from .dead_stock import RESULT_TABLE as DEAD_STOCK_ITEM_TABLE

PURCHASE_EVIDENCE_TABLE = "D2A_PURCHASE_OVERBUY_EVIDENCE"
PRODUCTION_EVIDENCE_TABLE = "D2A_PRODUCTION_LOSS_EVIDENCE"
ATTRIBUTION_TABLE = "D2A_DEAD_STOCK_ATTRIBUTION"
CALCULATION_VERSION = "dead-stock-attribution-v2-pending-relationships"

_PURCHASE_INFO = TableInfo(PURCHASE_EVIDENCE_TABLE, [
    ("plant_id", "text"), ("po_no", "text"), ("po_line_no", "text"), ("item_code", "text"),
    ("supplier_id", "text"), ("po_date", "text"), ("demand_qty", "real"),
    ("net_received_qty", "real"), ("purchase_qty", "real"), ("moq", "real"),
    ("actual_excess_qty", "real"), ("planned_moq_excess_qty", "real"),
    ("moq_forced_excess_qty", "real"), ("manual_excess_qty", "real"), ("inventory_qty", "real"),
    ("trace_type", "text"), ("calculation_status", "text"), ("related_department", "text"),
    ("related_employee", "text"), ("warnings", "text"), ("as_of_date", "text"),
    ("calculation_version", "text"),
], ["plant_id", "po_no", "po_line_no", "item_code"])

_PRODUCTION_INFO = TableInfo(PRODUCTION_EVIDENCE_TABLE, [
    ("plant_id", "text"), ("mo_no", "text"), ("item_code", "text"), ("mo_status", "text"),
    ("output_basis_qty", "real"), ("qty_per", "real"), ("denominator", "real"),
    ("allowed_loss_rate", "real"), ("fixed_loss_qty", "real"), ("issued_qty", "real"),
    ("returned_qty", "real"), ("net_issued_qty", "real"), ("standard_required_qty", "real"),
    ("allowed_issue_qty", "real"), ("excess_issue_qty", "real"), ("trace_type", "text"),
    ("calculation_status", "text"), ("related_department", "text"), ("related_employee", "text"),
    ("warnings", "text"), ("as_of_date", "text"), ("calculation_version", "text"),
], ["plant_id", "mo_no", "item_code"])

_ATTRIBUTION_INFO = TableInfo(ATTRIBUTION_TABLE, [
    ("plant_id", "text"), ("warehouse_code", "text"), ("item_code", "text"),
    ("root_cause", "text"), ("evidence_id", "text"), ("confidence", "real"),
    ("confidence_level", "text"), ("rule_version", "text"), ("trace_type", "text"),
    ("evidence_object", "text"), ("evidence_summary", "text"),
    ("related_department", "text"), ("related_employee", "text"), ("warnings", "text"),
    ("as_of_date", "text"), ("calculation_version", "text"),
], ["plant_id", "warehouse_code", "item_code", "root_cause", "evidence_id"])


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _as_of_date(store: LandingStore, source: str) -> str:
    table = _quoted(raw_table_name(source, DEAD_STOCK_ITEM_TABLE))
    row = store.con.execute(
        f"SELECT MAX(as_of_date) AS as_of_date FROM {table} WHERE _d2a_deleted_at IS NULL",
    ).fetchone()
    if not row["as_of_date"]:
        raise ValueError("呆滞库存快照不存在，无法初始化归因结果表")
    return str(row["as_of_date"])


def _replace_empty(store: LandingStore, source: str, info: TableInfo, batch_id: str) -> None:
    store.ensure_raw_table(source, info)
    table = _quoted(raw_table_name(source, info.name))
    store.con.execute(f"UPDATE {table} SET _d2a_deleted_at = CURRENT_TIMESTAMP")
    store.con.commit()


def materialize_dead_stock_attribution(store: LandingStore, source: str) -> int:
    """创建空的 R2/R5 证据集，避免在未验证关系上做推断。"""
    as_of = _as_of_date(store, source)
    batch_id = f"mat_{date.fromisoformat(as_of):%Y%m%d}_m2"
    for info in (_PURCHASE_INFO, _PRODUCTION_INFO, _ATTRIBUTION_INFO):
        _replace_empty(store, source, info, batch_id)
    return 0
