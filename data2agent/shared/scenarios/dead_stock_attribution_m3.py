"""呆滞库存 R1/R3 内部结果表。关联字段尚未现场核对，结果保持为空。"""

from __future__ import annotations

from datetime import date

from ..store.table import TableInfo
from ..store.landing import LandingStore

from .dead_stock_attribution import _as_of_date, _replace_empty

ORDER_EVIDENCE_TABLE = "D2A_MATERIAL_ORDER_EVIDENCE"
ECN_EVIDENCE_TABLE = "D2A_ECN_CHANGE_EVIDENCE"

_ORDER_INFO = TableInfo(ORDER_EVIDENCE_TABLE, [
    ("plant_id", "text"), ("item_code", "text"), ("sales_order_no", "text"),
    ("po_no", "text"), ("po_line_no", "text"), ("order_date", "text"),
    ("demand_event_date", "text"), ("demand_event_type", "text"), ("demand_qty", "real"),
    ("planned_qty", "real"), ("shipped_qty", "real"), ("cancelled_or_reduced_qty", "real"),
    ("purchase_qty", "real"), ("purchase_date", "text"), ("trace_type", "text"),
    ("calculation_status", "text"), ("related_department", "text"),
    ("related_employee", "text"), ("warnings", "text"), ("as_of_date", "text"),
    ("calculation_version", "text"),
], ["plant_id", "item_code", "sales_order_no", "po_no", "po_line_no"])

_ECN_INFO = TableInfo(ECN_EVIDENCE_TABLE, [
    ("plant_id", "text"), ("ecn_no", "text"), ("item_code", "text"),
    ("replacement_item_code", "text"), ("ecn_date", "text"), ("effective_date", "text"),
    ("handle", "text"), ("reason_desc", "text"), ("trace_type", "text"),
    ("calculation_status", "text"), ("related_department", "text"),
    ("related_employee", "text"), ("warnings", "text"), ("as_of_date", "text"),
    ("calculation_version", "text"),
], ["plant_id", "ecn_no", "item_code", "replacement_item_code"])


def materialize_dead_stock_attribution_m3(store: LandingStore, source: str) -> int:
    """创建空的 R1/R3 证据集，避免使用未核对的单据关联。"""
    as_of = _as_of_date(store, source)
    batch_id = f"mat_{date.fromisoformat(as_of):%Y%m%d}_m3"
    for info in (_ORDER_INFO, _ECN_INFO):
        _replace_empty(store, source, info, batch_id)
    return 0
