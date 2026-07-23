"""呆滞库存 R4/R6 与转用候选内部结果表。

物料、BOM 和工单之间的关联尚未核对，因此所有候选结果保持为空。
"""

from __future__ import annotations

from datetime import date

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.landing import LandingStore

from .dead_stock_attribution import _as_of_date, _replace_empty

SPECIAL_CONDITION_TABLE = "D2A_SPECIAL_CONDITION_EVIDENCE"
DUPLICATE_CANDIDATE_TABLE = "D2A_DUPLICATE_MATERIAL_CANDIDATE"
BOM_USAGE_TABLE = "D2A_MATERIAL_BOM_USAGE"
SUBSTITUTE_CANDIDATE_TABLE = "D2A_MATERIAL_SUBSTITUTE_CANDIDATE"

_SPECIAL_INFO = TableInfo(SPECIAL_CONDITION_TABLE, [
    ("plant_id", "text"), ("item_code", "text"), ("parent_item_code", "text"),
    ("parent_item_name", "text"), ("restriction_type", "text"), ("restriction_text", "text"),
    ("qty_per", "real"), ("denominator", "real"), ("trace_type", "text"),
    ("calculation_status", "text"), ("related_department", "text"),
    ("related_employee", "text"), ("warnings", "text"), ("as_of_date", "text"),
    ("calculation_version", "text"),
], ["plant_id", "item_code", "parent_item_code"])

_DUPLICATE_INFO = TableInfo(DUPLICATE_CANDIDATE_TABLE, [
    ("item_code", "text"), ("candidate_item_code", "text"), ("item_name", "text"),
    ("candidate_item_name", "text"), ("normalized_specification", "text"),
    ("match_method", "text"), ("trace_type", "text"), ("calculation_status", "text"),
    ("warnings", "text"), ("as_of_date", "text"), ("calculation_version", "text"),
], ["item_code", "candidate_item_code"])

_BOM_USAGE_INFO = TableInfo(BOM_USAGE_TABLE, [
    ("plant_id", "text"), ("item_code", "text"), ("parent_item_code", "text"),
    ("parent_item_name", "text"), ("qty_per", "real"), ("denominator", "real"),
    ("restriction_text", "text"), ("open_mo_count", "int"), ("open_mo_plan_qty", "real"),
    ("potential_required_qty", "real"), ("trace_type", "text"),
    ("calculation_status", "text"), ("warnings", "text"), ("as_of_date", "text"),
    ("calculation_version", "text"),
], ["plant_id", "item_code", "parent_item_code"])

_SUBSTITUTE_INFO = TableInfo(SUBSTITUTE_CANDIDATE_TABLE, [
    ("source_plant_id", "text"), ("item_code", "text"), ("target_plant_id", "text"),
    ("candidate_parent_item_code", "text"), ("candidate_type", "text"), ("basis", "text"),
    ("potential_consume_qty", "real"), ("open_mo_count", "int"),
    ("open_mo_plan_qty", "real"), ("constraint_note", "text"), ("trace_type", "text"),
    ("calculation_status", "text"), ("warnings", "text"), ("as_of_date", "text"),
    ("calculation_version", "text"),
], ["source_plant_id", "item_code", "target_plant_id", "candidate_parent_item_code", "candidate_type"])


def materialize_dead_stock_attribution_m3b(store: LandingStore, source: str) -> int:
    """创建空的 R4/R6 与转用候选集，避免推断未核对关系。"""
    as_of = _as_of_date(store, source)
    batch_id = f"mat_{date.fromisoformat(as_of):%Y%m%d}_m3b"
    for info in (_SPECIAL_INFO, _DUPLICATE_INFO, _BOM_USAGE_INFO, _SUBSTITUTE_INFO):
        _replace_empty(store, source, info, batch_id)
    return 0
