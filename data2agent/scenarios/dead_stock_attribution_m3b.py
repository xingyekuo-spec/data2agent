"""呆滞库存 M3b/M3c 扩展：R4/R6 低置信候选与可消耗候选。"""

from __future__ import annotations

import json
import re
from datetime import date

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.landing import LandingStore, raw_table_name

from .dead_stock import RESULT_TABLE as DEAD_STOCK_ITEM_TABLE
from .dead_stock_attribution import (
    _ATTRIBUTION_INFO,
    _active_table,
    _as_of_date,
    _replace_rows,
)

SPECIAL_CONDITION_TABLE = "D2A_SPECIAL_CONDITION_EVIDENCE"
DUPLICATE_CANDIDATE_TABLE = "D2A_DUPLICATE_MATERIAL_CANDIDATE"
BOM_USAGE_TABLE = "D2A_MATERIAL_BOM_USAGE"
SUBSTITUTE_CANDIDATE_TABLE = "D2A_MATERIAL_SUBSTITUTE_CANDIDATE"
CALCULATION_VERSION = "dead-stock-attribution-v3"

_SPECIAL_INFO = TableInfo(
    name=SPECIAL_CONDITION_TABLE,
    columns=[
        ("plant_id", "text"), ("item_code", "text"), ("parent_item_code", "text"),
        ("parent_item_name", "text"), ("restriction_type", "text"),
        ("restriction_text", "text"), ("qty_per", "real"), ("denominator", "real"),
        ("trace_type", "text"), ("calculation_status", "text"),
        ("related_department", "text"), ("related_employee", "text"),
        ("warnings", "text"), ("as_of_date", "text"), ("calculation_version", "text"),
    ],
    pk=["plant_id", "item_code", "parent_item_code"],
)

_DUPLICATE_INFO = TableInfo(
    name=DUPLICATE_CANDIDATE_TABLE,
    columns=[
        ("item_code", "text"), ("candidate_item_code", "text"),
        ("item_name", "text"), ("candidate_item_name", "text"),
        ("normalized_specification", "text"), ("match_method", "text"),
        ("trace_type", "text"), ("calculation_status", "text"),
        ("warnings", "text"), ("as_of_date", "text"), ("calculation_version", "text"),
    ],
    pk=["item_code", "candidate_item_code"],
)

_BOM_USAGE_INFO = TableInfo(
    name=BOM_USAGE_TABLE,
    columns=[
        ("plant_id", "text"), ("item_code", "text"), ("parent_item_code", "text"),
        ("parent_item_name", "text"), ("qty_per", "real"), ("denominator", "real"),
        ("restriction_text", "text"), ("open_mo_count", "int"),
        ("open_mo_plan_qty", "real"), ("potential_required_qty", "real"),
        ("trace_type", "text"), ("calculation_status", "text"),
        ("warnings", "text"), ("as_of_date", "text"), ("calculation_version", "text"),
    ],
    pk=["plant_id", "item_code", "parent_item_code"],
)

_SUBSTITUTE_INFO = TableInfo(
    name=SUBSTITUTE_CANDIDATE_TABLE,
    columns=[
        ("source_plant_id", "text"), ("item_code", "text"),
        ("target_plant_id", "text"), ("candidate_parent_item_code", "text"),
        ("candidate_type", "text"), ("basis", "text"),
        ("potential_consume_qty", "real"), ("open_mo_count", "int"),
        ("open_mo_plan_qty", "real"), ("constraint_note", "text"),
        ("trace_type", "text"), ("calculation_status", "text"),
        ("warnings", "text"), ("as_of_date", "text"), ("calculation_version", "text"),
    ],
    pk=["source_plant_id", "item_code", "target_plant_id", "candidate_parent_item_code", "candidate_type"],
)


def _normalise_spec(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


_GENERIC_SPECS = {"见图纸", "按采购规格书"}


def _warehouses(store: LandingStore, source: str) -> dict[tuple[str, str], list[str]]:
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    rows = store.con.execute(
        f"SELECT plant_id, warehouse_code, item_code FROM {dead} "
        "WHERE determination_status = 'dead_stock' AND _d2a_deleted_at IS NULL",
    )
    out: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        out.setdefault((str(row["plant_id"]), str(row["item_code"])), []).append(str(row["warehouse_code"]))
    return out


def _special_condition_evidence(store: LandingStore, source: str, as_of: str) -> list[dict]:
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    item = _active_table(store, source, "ITEM")
    bom = _active_table(store, source, "BOM_D")
    sql = f"""
    SELECT ds.plant_id, child.ITEM_CODE AS item_code,
           parent.ITEM_CODE AS parent_item_code, parent.ITEM_NAME AS parent_item_name,
           b.QTY_PER, b.DENOMINATOR, b.REMARK
    FROM {bom} b
    JOIN {item} child ON child.Id = b.SUB_ITEM_FEATURE_ID AND child._d2a_deleted_at IS NULL
    JOIN {item} parent ON parent.Id = b.PARENT_ITEM_ID AND parent._d2a_deleted_at IS NULL
    JOIN {dead} ds ON ds.item_code = child.ITEM_CODE
                    AND ds.determination_status = 'dead_stock'
                    AND ds._d2a_deleted_at IS NULL
    WHERE b._d2a_deleted_at IS NULL
      AND (INSTR(COALESCE(b.REMARK, ''), '仅适用') > 0
           OR INSTR(COALESCE(b.REMARK, ''), '指定品牌') > 0)
    """
    warnings = json.dumps(["仅基于 BOM 结构化备注识别特殊使用条件，需业务复核"], ensure_ascii=False)
    return [{
        "plant_id": row["plant_id"],
        "item_code": row["item_code"],
        "parent_item_code": row["parent_item_code"],
        "parent_item_name": row["parent_item_name"],
        "restriction_type": "bom_remark_keyword",
        "restriction_text": row["REMARK"],
        "qty_per": float(row["QTY_PER"]) if row["QTY_PER"] is not None else None,
        "denominator": float(row["DENOMINATOR"]) if row["DENOMINATOR"] is not None else None,
        "trace_type": "indirect",
        "calculation_status": "candidate",
        "related_department": None,
        "related_employee": None,
        "warnings": warnings,
        "as_of_date": as_of,
        "calculation_version": CALCULATION_VERSION,
    } for row in store.con.execute(sql)]


def _duplicate_candidates(store: LandingStore, source: str, as_of: str) -> list[dict]:
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    item = _active_table(store, source, "ITEM")
    dead_codes = {
        str(row["item_code"])
        for row in store.con.execute(
            f"SELECT DISTINCT item_code FROM {dead} "
            "WHERE determination_status = 'dead_stock' AND _d2a_deleted_at IS NULL",
        )
    }
    by_spec: dict[str, list[dict]] = {}
    for row in store.con.execute(
        f"SELECT ITEM_CODE, ITEM_NAME, ITEM_SPECIFICATION FROM {item} "
        "WHERE _d2a_deleted_at IS NULL AND COALESCE(ITEM_SPECIFICATION, '') <> ''",
    ):
        normalised = _normalise_spec(row["ITEM_SPECIFICATION"])
        if normalised and normalised not in _GENERIC_SPECS:
            by_spec.setdefault(normalised, []).append(dict(row))

    warnings = json.dumps(["仅基于 ITEM.ITEM_SPECIFICATION 规范化精确匹配，未证明物料可替代"], ensure_ascii=False)
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for normalised, group in by_spec.items():
        if len(group) < 2:
            continue
        for base in group:
            if base["ITEM_CODE"] not in dead_codes:
                continue
            for candidate in group:
                if candidate["ITEM_CODE"] == base["ITEM_CODE"]:
                    continue
                key = (str(base["ITEM_CODE"]), str(candidate["ITEM_CODE"]))
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "item_code": base["ITEM_CODE"],
                    "candidate_item_code": candidate["ITEM_CODE"],
                    "item_name": base["ITEM_NAME"],
                    "candidate_item_name": candidate["ITEM_NAME"],
                    "normalized_specification": normalised,
                    "match_method": "normalized_exact_specification",
                    "trace_type": "indirect",
                    "calculation_status": "candidate",
                    "warnings": warnings,
                    "as_of_date": as_of,
                    "calculation_version": CALCULATION_VERSION,
                })
    return rows


def _bom_usage(store: LandingStore, source: str, as_of: str) -> list[dict]:
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    item = _active_table(store, source, "ITEM")
    bom = _active_table(store, source, "BOM_D")
    mo = _active_table(store, source, "MO")
    sql = f"""
    WITH open_mo AS (
      SELECT ITEM_ID AS parent_item_id, PLANT_ID,
             COUNT(*) AS open_mo_count,
             SUM(COALESCE(PLAN_QTY, 0)) AS open_mo_plan_qty
      FROM {mo}
      WHERE _d2a_deleted_at IS NULL
        AND LOWER(COALESCE(STATUS, '')) NOT IN ('closed', 'completed', '结案')
      GROUP BY ITEM_ID, PLANT_ID
    )
    SELECT ds.plant_id, child.ITEM_CODE AS item_code,
           parent.ITEM_CODE AS parent_item_code, parent.ITEM_NAME AS parent_item_name,
           b.QTY_PER, b.DENOMINATOR, b.REMARK,
           COALESCE(open_mo.open_mo_count, 0) AS open_mo_count,
           COALESCE(open_mo.open_mo_plan_qty, 0) AS open_mo_plan_qty
    FROM {bom} b
    JOIN {item} child ON child.Id = b.SUB_ITEM_FEATURE_ID AND child._d2a_deleted_at IS NULL
    JOIN {item} parent ON parent.Id = b.PARENT_ITEM_ID AND parent._d2a_deleted_at IS NULL
    JOIN {dead} ds ON ds.item_code = child.ITEM_CODE
                    AND ds.determination_status = 'dead_stock'
                    AND ds._d2a_deleted_at IS NULL
    LEFT JOIN open_mo ON open_mo.parent_item_id = b.PARENT_ITEM_ID
                     AND open_mo.PLANT_ID = ds.plant_id
    WHERE b._d2a_deleted_at IS NULL
    """
    rows: list[dict] = []
    for row in store.con.execute(sql):
        denominator = float(row["DENOMINATOR"] or 1)
        qty_per = float(row["QTY_PER"] or 0)
        open_plan = float(row["open_mo_plan_qty"] or 0)
        potential = open_plan * qty_per / denominator if denominator > 0 else None
        rows.append({
            "plant_id": row["plant_id"],
            "item_code": row["item_code"],
            "parent_item_code": row["parent_item_code"],
            "parent_item_name": row["parent_item_name"],
            "qty_per": qty_per,
            "denominator": denominator,
            "restriction_text": row["REMARK"],
            "open_mo_count": int(row["open_mo_count"] or 0),
            "open_mo_plan_qty": open_plan,
            "potential_required_qty": potential,
            "trace_type": "indirect",
            "calculation_status": "ready" if potential and potential > 0 else "no_open_demand",
            "warnings": json.dumps(["BOM 使用与未结案工单按主件/工厂匹配，实际消耗需确认版本和替代约束"], ensure_ascii=False),
            "as_of_date": as_of,
            "calculation_version": CALCULATION_VERSION,
        })
    return rows


def _substitute_candidates(bom_rows: list[dict], as_of: str) -> list[dict]:
    rows: list[dict] = []
    for evidence in bom_rows:
        if evidence["calculation_status"] != "ready" or float(evidence["potential_required_qty"] or 0) <= 0:
            continue
        rows.append({
            "source_plant_id": evidence["plant_id"],
            "item_code": evidence["item_code"],
            "target_plant_id": evidence["plant_id"],
            "candidate_parent_item_code": evidence["parent_item_code"],
            "candidate_type": "bom_consumption",
            "basis": "active_bom_open_mo",
            "potential_consume_qty": evidence["potential_required_qty"],
            "open_mo_count": evidence["open_mo_count"],
            "open_mo_plan_qty": evidence["open_mo_plan_qty"],
            "constraint_note": "非替代结论，仅表示当前 BOM 与未结案工单存在潜在消耗场景",
            "trace_type": "indirect",
            "calculation_status": "candidate",
            "warnings": evidence["warnings"],
            "as_of_date": as_of,
            "calculation_version": CALCULATION_VERSION,
        })
    return rows


def _append_attributions(
    store: LandingStore,
    source: str,
    special_rows: list[dict],
    duplicate_rows: list[dict],
    as_of: str,
    batch_id: str,
) -> int:
    whs = _warehouses(store, source)
    rows: list[dict] = []
    for evidence in special_rows:
        for warehouse in whs.get((str(evidence["plant_id"]), str(evidence["item_code"])), []):
            rows.append({
                "plant_id": evidence["plant_id"],
                "warehouse_code": warehouse,
                "item_code": evidence["item_code"],
                "root_cause": "R4",
                "evidence_id": evidence["parent_item_code"],
                "confidence": 0.4,
                "confidence_level": "LOW",
                "rule_version": "r4-v1",
                "trace_type": "indirect",
                "evidence_object": "SpecialConditionEvidence",
                "evidence_summary": json.dumps({
                    "parent_item_code": evidence["parent_item_code"],
                    "restriction_type": evidence["restriction_type"],
                    "restriction_text": evidence["restriction_text"],
                }, ensure_ascii=False, sort_keys=True),
                "related_department": evidence["related_department"],
                "related_employee": evidence["related_employee"],
                "warnings": evidence["warnings"],
                "as_of_date": as_of,
                "calculation_version": CALCULATION_VERSION,
            })
    for evidence in duplicate_rows:
        dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
        for row in store.con.execute(
            f"SELECT plant_id, warehouse_code FROM {dead} "
            "WHERE item_code = ? AND determination_status = 'dead_stock' AND _d2a_deleted_at IS NULL",
            (evidence["item_code"],),
        ):
            rows.append({
                "plant_id": row["plant_id"],
                "warehouse_code": row["warehouse_code"],
                "item_code": evidence["item_code"],
                "root_cause": "R6",
                "evidence_id": evidence["candidate_item_code"],
                "confidence": 0.2,
                "confidence_level": "LOW",
                "rule_version": "r6-v1",
                "trace_type": "indirect",
                "evidence_object": "DuplicateMaterialCandidate",
                "evidence_summary": json.dumps({
                    "candidate_item_code": evidence["candidate_item_code"],
                    "match_method": evidence["match_method"],
                    "normalized_specification": evidence["normalized_specification"],
                }, ensure_ascii=False, sort_keys=True),
                "related_department": None,
                "related_employee": None,
                "warnings": evidence["warnings"],
                "as_of_date": as_of,
                "calculation_version": CALCULATION_VERSION,
            })
    store.upsert_rows(source, _ATTRIBUTION_INFO, rows, batch_id)
    return len(rows)


def materialize_dead_stock_attribution_m3b(store: LandingStore, source: str) -> int:
    """在 M3a 归因结果之上追加 R4/R6，并产出 M3c 可消耗候选对象。"""
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    as_of = _as_of_date(store, dead)
    batch_id = f"mat_{date.fromisoformat(as_of):%Y%m%d}_m3b"
    special_rows = _special_condition_evidence(store, source, as_of)
    duplicate_rows = _duplicate_candidates(store, source, as_of)
    bom_rows = _bom_usage(store, source, as_of)
    substitute_rows = _substitute_candidates(bom_rows, as_of)
    _replace_rows(store, source, _SPECIAL_INFO, special_rows, batch_id)
    _replace_rows(store, source, _DUPLICATE_INFO, duplicate_rows, batch_id)
    _replace_rows(store, source, _BOM_USAGE_INFO, bom_rows, batch_id)
    _replace_rows(store, source, _SUBSTITUTE_INFO, substitute_rows, batch_id)
    return _append_attributions(store, source, special_rows, duplicate_rows, as_of, batch_id)
