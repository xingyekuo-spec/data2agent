"""呆滞库存 M3a 扩展归因：R1 订单取消/减量与 R3 ECN 变更未消化。"""

from __future__ import annotations

import json
from datetime import date

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.landing import LandingStore, raw_table_name

from .dead_stock import RESULT_TABLE as DEAD_STOCK_ITEM_TABLE
from .dead_stock_attribution import (
    ATTRIBUTION_TABLE,
    _ATTRIBUTION_INFO,
    _active_table,
    _as_of_date,
    _replace_rows,
)

ORDER_EVIDENCE_TABLE = "D2A_MATERIAL_ORDER_EVIDENCE"
ECN_EVIDENCE_TABLE = "D2A_ECN_CHANGE_EVIDENCE"
CALCULATION_VERSION = "dead-stock-attribution-v2"

_ORDER_INFO = TableInfo(
    name=ORDER_EVIDENCE_TABLE,
    columns=[
        ("plant_id", "text"), ("item_code", "text"),
        ("sales_order_no", "text"), ("po_no", "text"), ("po_line_no", "text"),
        ("order_date", "text"), ("demand_event_date", "text"),
        ("demand_event_type", "text"), ("demand_qty", "real"),
        ("planned_qty", "real"), ("shipped_qty", "real"),
        ("cancelled_or_reduced_qty", "real"), ("purchase_qty", "real"),
        ("purchase_date", "text"), ("trace_type", "text"),
        ("calculation_status", "text"), ("related_department", "text"),
        ("related_employee", "text"), ("warnings", "text"),
        ("as_of_date", "text"), ("calculation_version", "text"),
    ],
    pk=["plant_id", "item_code", "sales_order_no", "po_no", "po_line_no"],
)

_ECN_INFO = TableInfo(
    name=ECN_EVIDENCE_TABLE,
    columns=[
        ("plant_id", "text"), ("ecn_no", "text"), ("item_code", "text"),
        ("replacement_item_code", "text"), ("ecn_date", "text"),
        ("effective_date", "text"), ("handle", "text"), ("reason_desc", "text"),
        ("trace_type", "text"), ("calculation_status", "text"),
        ("related_department", "text"), ("related_employee", "text"),
        ("warnings", "text"), ("as_of_date", "text"),
        ("calculation_version", "text"),
    ],
    pk=["plant_id", "ecn_no", "item_code", "replacement_item_code"],
)


def _order_evidence(store: LandingStore, source: str, as_of: str) -> list[dict]:
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    item = _active_table(store, source, "ITEM")
    so = _active_table(store, source, "SALES_ORDER_DOC")
    so_d = _active_table(store, source, "SALES_ORDER_DOC_D")
    so_sd = _active_table(store, source, "SALES_ORDER_DOC_SD")
    po = _active_table(store, source, "PURCHASE_ORDER")
    po_d = _active_table(store, source, "PURCHASE_ORDER_D")
    po_sd = _active_table(store, source, "PURCHASE_ORDER_SD")
    po_sd1 = _active_table(store, source, "PURCHASE_ORDER_SD1")
    req = _active_table(store, source, "PO_REQ_SOURCE")
    sql = f"""
    WITH plans AS (
      SELECT SALES_ORDER_DOC_D_ID AS line_id,
             SUM(COALESCE(PLAN_QTY, 0)) AS planned_qty,
             SUM(COALESCE(SHIPPED_QTY, 0)) AS shipped_qty
      FROM {so_sd} WHERE _d2a_deleted_at IS NULL GROUP BY SALES_ORDER_DOC_D_ID
    ), po_links AS (
      SELECT req.DEMAND_NO, pol.ITEM_ID, pol.SEQUENCE_NUMBER, poh.DOC_NO AS po_no,
             poh.DOC_DATE AS purchase_date, pol.PURCHASE_QTY, MAX(psd.PLANT_ID) AS plant_id
      FROM {req} req
      JOIN {po_sd1} psd1 ON psd1.Id = req.PURCHASE_ORDER_SD1_ID AND psd1._d2a_deleted_at IS NULL
      JOIN {po_sd} psd ON psd.Id = psd1.PURCHASE_ORDER_SD_ID AND psd._d2a_deleted_at IS NULL
      JOIN {po_d} pol ON pol.Id = psd.PURCHASE_ORDER_D_ID AND pol._d2a_deleted_at IS NULL
      JOIN {po} poh ON poh.Id = pol.PURCHASE_ORDER_ID AND poh._d2a_deleted_at IS NULL
      WHERE req._d2a_deleted_at IS NULL
      GROUP BY req.Id
    )
    SELECT h.DOC_NO AS sales_order_no, h.DOC_DATE AS order_date, h.ApproveStatus,
           h.LAST_MODIFIED_DATE AS demand_event_date, h.Owner_Dept, h.Owner_Emp,
           l.BUSINESS_QTY, plans.planned_qty, plans.shipped_qty, i.ITEM_CODE,
           p.po_no, p.purchase_date, p.PURCHASE_QTY, p.SEQUENCE_NUMBER, p.plant_id
    FROM {so} h
    JOIN {so_d} l ON l.SALES_ORDER_DOC_ID = h.Id AND l._d2a_deleted_at IS NULL
    JOIN {item} i ON i.Id = l.ITEM_ID AND i._d2a_deleted_at IS NULL
    JOIN po_links p ON p.DEMAND_NO = h.DOC_NO AND p.ITEM_ID = l.ITEM_ID
    JOIN {dead} ds ON ds.item_code = i.ITEM_CODE AND ds.plant_id = p.plant_id
                    AND ds.determination_status = 'dead_stock' AND ds._d2a_deleted_at IS NULL
    LEFT JOIN plans ON plans.line_id = l.Id
    WHERE h._d2a_deleted_at IS NULL
      AND h.ApproveStatus IN ('已取消', '已减量')
      AND p.purchase_date <= SUBSTR(h.LAST_MODIFIED_DATE, 1, 10)
    """
    rows: list[dict] = []
    for row in store.con.execute(sql):
        demand = row["BUSINESS_QTY"]
        planned = row["planned_qty"]
        shipped = row["shipped_qty"]
        status = "ready"
        warnings = ["当前库存与订单/采购单按工厂、品号间接关联，未证明批次级因果关系"]
        if any(value is None for value in (demand, planned, shipped, row["PURCHASE_QTY"])):
            status = "unknown"
            reduced = None
            warnings.append("订单需求、计划/出货数量或采购数量缺失，未生成 R1 归因")
        else:
            reduced = max(float(demand) - float(shipped), float(planned) - float(shipped), 0)
        rows.append({
            "plant_id": row["plant_id"], "item_code": row["ITEM_CODE"],
            "sales_order_no": row["sales_order_no"], "po_no": row["po_no"],
            "po_line_no": str(row["SEQUENCE_NUMBER"]), "order_date": row["order_date"],
            "demand_event_date": str(row["demand_event_date"])[:10],
            "demand_event_type": row["ApproveStatus"],
            "demand_qty": float(demand) if demand is not None else None,
            "planned_qty": float(planned) if planned is not None else None,
            "shipped_qty": float(shipped) if shipped is not None else None,
            "cancelled_or_reduced_qty": reduced,
            "purchase_qty": float(row["PURCHASE_QTY"]) if row["PURCHASE_QTY"] is not None else None,
            "purchase_date": row["purchase_date"], "trace_type": "indirect",
            "calculation_status": status, "related_department": row["Owner_Dept"],
            "related_employee": row["Owner_Emp"],
            "warnings": json.dumps(warnings, ensure_ascii=False), "as_of_date": as_of,
            "calculation_version": CALCULATION_VERSION,
        })
    return rows


def _ecn_evidence(store: LandingStore, source: str, as_of: str) -> list[dict]:
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    item = _active_table(store, source, "ITEM")
    ecn = _active_table(store, source, "ECN")
    ecn_d = _active_table(store, source, "ECN_D")
    ecn_sd = _active_table(store, source, "ECN_SD")
    task = _active_table(store, source, "ECN_TASK")
    sql = f"""
    WITH task_owner AS (
      SELECT ECN_ID, MIN(DEPARTMENT_ID) AS department_id, MIN(PERSON_ID) AS person_id
      FROM {task} WHERE _d2a_deleted_at IS NULL GROUP BY ECN_ID
    )
    SELECT e.DOC_NO, e.DOC_DATE, e.REASON_DESC, e.Owner_Dept, e.Owner_Emp,
           sd.EFFECTIVE_DATE, sd.HANDLE, old.ITEM_CODE, new.ITEM_CODE AS replacement_item_code,
           ds.plant_id, task_owner.department_id, task_owner.person_id
    FROM {ecn_sd} sd
    JOIN {ecn_d} d ON d.Id = sd.ECN_D_ID AND d._d2a_deleted_at IS NULL
    JOIN {ecn} e ON e.Id = d.ECN_ID AND e._d2a_deleted_at IS NULL
    JOIN {item} old ON old.Id = sd.ORIGINAL_SUB_ITEM_FEATURE_ID AND old._d2a_deleted_at IS NULL
    JOIN {item} new ON new.Id = sd.SUB_ITEM_FEATURE_ID AND new._d2a_deleted_at IS NULL
    JOIN {dead} ds ON ds.item_code = old.ITEM_CODE AND ds.determination_status = 'dead_stock'
                    AND ds._d2a_deleted_at IS NULL
    LEFT JOIN task_owner ON task_owner.ECN_ID = e.Id
    WHERE sd._d2a_deleted_at IS NULL AND sd.EFFECTIVE_DATE <= ?
      AND LOWER(COALESCE(sd.HANDLE, '')) NOT IN ('run-out', '用完为止')
    """
    rows: list[dict] = []
    for row in store.con.execute(sql, (as_of,)):
        handle = str(row["HANDLE"] or "").strip()
        warnings = ["当前库存与 ECN 变更按工厂、品号间接关联，未证明批次级因果关系"]
        if not handle:
            warnings.append("ECN 处置方式为空，R3 置信度降为 MEDIUM")
        rows.append({
            "plant_id": row["plant_id"], "ecn_no": row["DOC_NO"], "item_code": row["ITEM_CODE"],
            "replacement_item_code": row["replacement_item_code"], "ecn_date": row["DOC_DATE"],
            "effective_date": row["EFFECTIVE_DATE"], "handle": handle or None,
            "reason_desc": row["REASON_DESC"], "trace_type": "indirect",
            "calculation_status": "ready", "related_department": row["department_id"] or row["Owner_Dept"],
            "related_employee": row["person_id"] or row["Owner_Emp"],
            "warnings": json.dumps(warnings, ensure_ascii=False), "as_of_date": as_of,
            "calculation_version": CALCULATION_VERSION,
        })
    return rows


def _append_attributions(store: LandingStore, source: str, order_rows: list[dict], ecn_rows: list[dict], as_of: str, batch_id: str) -> int:
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    warehouses: dict[tuple[str, str], list[str]] = {}
    for row in store.con.execute(
        f"SELECT plant_id, warehouse_code, item_code FROM {dead} WHERE determination_status = 'dead_stock' AND _d2a_deleted_at IS NULL",
    ):
        warehouses.setdefault((str(row["plant_id"]), str(row["item_code"])), []).append(str(row["warehouse_code"]))
    rows: list[dict] = []
    for evidence in order_rows:
        if evidence["calculation_status"] != "ready" or float(evidence["cancelled_or_reduced_qty"] or 0) <= 0:
            continue
        for warehouse in warehouses.get((str(evidence["plant_id"]), str(evidence["item_code"])), []):
            rows.append({
                "plant_id": evidence["plant_id"], "warehouse_code": warehouse, "item_code": evidence["item_code"],
                "root_cause": "R1", "evidence_id": f"{evidence['sales_order_no']}:{evidence['po_no']}:{evidence['po_line_no']}",
                "confidence": 0.9, "confidence_level": "HIGH", "rule_version": "r1-v1", "trace_type": "indirect",
                "evidence_object": "MaterialOrderEvidence",
                "evidence_summary": json.dumps({k: evidence[k] for k in ("sales_order_no", "po_no", "demand_qty", "shipped_qty", "cancelled_or_reduced_qty", "purchase_qty")}, ensure_ascii=False, sort_keys=True),
                "related_department": evidence["related_department"], "related_employee": evidence["related_employee"],
                "warnings": evidence["warnings"], "as_of_date": as_of, "calculation_version": CALCULATION_VERSION,
            })
    for evidence in ecn_rows:
        confidence, level = (0.85, "HIGH") if evidence["handle"] else (0.6, "MEDIUM")
        for warehouse in warehouses.get((str(evidence["plant_id"]), str(evidence["item_code"])), []):
            rows.append({
                "plant_id": evidence["plant_id"], "warehouse_code": warehouse, "item_code": evidence["item_code"],
                "root_cause": "R3", "evidence_id": evidence["ecn_no"], "confidence": confidence,
                "confidence_level": level, "rule_version": "r3-v1", "trace_type": "indirect",
                "evidence_object": "EcnChangeEvidence",
                "evidence_summary": json.dumps({k: evidence[k] for k in ("ecn_no", "replacement_item_code", "effective_date", "handle", "reason_desc")}, ensure_ascii=False, sort_keys=True),
                "related_department": evidence["related_department"], "related_employee": evidence["related_employee"],
                "warnings": evidence["warnings"], "as_of_date": as_of, "calculation_version": CALCULATION_VERSION,
            })
    store.upsert_rows(source, _ATTRIBUTION_INFO, rows, batch_id)
    return len(rows)


def materialize_dead_stock_attribution_m3(store: LandingStore, source: str) -> int:
    """在 M2 归因结果之上追加 R1、R3 证据和标签。"""
    dead = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    as_of = _as_of_date(store, dead)
    batch_id = f"mat_{date.fromisoformat(as_of):%Y%m%d}_m3"
    order_rows = _order_evidence(store, source, as_of)
    ecn_rows = _ecn_evidence(store, source, as_of)
    _replace_rows(store, source, _ORDER_INFO, order_rows, batch_id)
    _replace_rows(store, source, _ECN_INFO, ecn_rows, batch_id)
    return _append_attributions(store, source, order_rows, ecn_rows, as_of, batch_id)
