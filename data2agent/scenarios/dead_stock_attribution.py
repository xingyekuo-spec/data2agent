"""呆滞库存 M2 核心归因预计算（R2/R2M、R5）。

库存余额与采购/工单单据没有批次级外键。本模块只以工厂、品号和时间窗口
生成间接归因证据，明确保留 trace_type、计算状态与警示，避免把相关性写成
确定因果。
"""

from __future__ import annotations

import json
import math
from datetime import date

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.landing import LandingStore, raw_table_name

from .dead_stock import RESULT_TABLE as DEAD_STOCK_ITEM_TABLE

PURCHASE_EVIDENCE_TABLE = "D2A_PURCHASE_OVERBUY_EVIDENCE"
PRODUCTION_EVIDENCE_TABLE = "D2A_PRODUCTION_LOSS_EVIDENCE"
ATTRIBUTION_TABLE = "D2A_DEAD_STOCK_ATTRIBUTION"
CALCULATION_VERSION = "dead-stock-attribution-v1"

_PURCHASE_INFO = TableInfo(
    name=PURCHASE_EVIDENCE_TABLE,
    columns=[
        ("plant_id", "text"), ("po_no", "text"), ("po_line_no", "text"),
        ("item_code", "text"), ("supplier_id", "text"),
        ("po_date", "text"), ("demand_qty", "real"),
        ("net_received_qty", "real"), ("purchase_qty", "real"),
        ("moq", "real"), ("actual_excess_qty", "real"),
        ("planned_moq_excess_qty", "real"), ("moq_forced_excess_qty", "real"),
        ("manual_excess_qty", "real"), ("inventory_qty", "real"),
        ("trace_type", "text"), ("calculation_status", "text"),
        ("related_department", "text"), ("related_employee", "text"),
        ("warnings", "text"), ("as_of_date", "text"),
        ("calculation_version", "text"),
    ],
    pk=["plant_id", "po_no", "po_line_no", "item_code"],
)

_PRODUCTION_INFO = TableInfo(
    name=PRODUCTION_EVIDENCE_TABLE,
    columns=[
        ("plant_id", "text"), ("mo_no", "text"), ("item_code", "text"),
        ("mo_status", "text"), ("output_basis_qty", "real"),
        ("qty_per", "real"), ("denominator", "real"),
        ("allowed_loss_rate", "real"), ("fixed_loss_qty", "real"),
        ("issued_qty", "real"), ("returned_qty", "real"),
        ("net_issued_qty", "real"), ("standard_required_qty", "real"),
        ("allowed_issue_qty", "real"), ("excess_issue_qty", "real"),
        ("trace_type", "text"), ("calculation_status", "text"),
        ("related_department", "text"), ("related_employee", "text"),
        ("warnings", "text"), ("as_of_date", "text"),
        ("calculation_version", "text"),
    ],
    pk=["plant_id", "mo_no", "item_code"],
)

_ATTRIBUTION_INFO = TableInfo(
    name=ATTRIBUTION_TABLE,
    columns=[
        ("plant_id", "text"), ("warehouse_code", "text"),
        ("item_code", "text"), ("root_cause", "text"),
        ("evidence_id", "text"), ("confidence", "real"),
        ("confidence_level", "text"), ("rule_version", "text"),
        ("trace_type", "text"), ("evidence_object", "text"),
        ("evidence_summary", "text"), ("related_department", "text"),
        ("related_employee", "text"), ("warnings", "text"),
        ("as_of_date", "text"), ("calculation_version", "text"),
    ],
    pk=["plant_id", "warehouse_code", "item_code", "root_cause", "evidence_id"],
)


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _active_table(store: LandingStore, source: str, logical_table: str) -> str:
    table = raw_table_name(source, logical_table)
    exists = store.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,),
    ).fetchone()
    if exists is None:
        raise ValueError(f"dead_stock_attribution_v1 缺少已同步原始表: {logical_table}")
    return _quoted(table)


def _replace_rows(
    store: LandingStore, source: str, info: TableInfo, rows: list[dict], batch_id: str,
) -> None:
    store.ensure_raw_table(source, info)
    table = _quoted(raw_table_name(source, info.name))
    store.con.execute(f"UPDATE {table} SET _d2a_deleted_at = CURRENT_TIMESTAMP")
    store.con.commit()
    store.upsert_rows(source, info, rows, batch_id)


def _as_of_date(store: LandingStore, dead_stock: str) -> str:
    row = store.con.execute(
        f"SELECT MAX(as_of_date) AS as_of_date FROM {dead_stock} "
        "WHERE _d2a_deleted_at IS NULL",
    ).fetchone()
    value = row["as_of_date"]
    if not value:
        raise ValueError("dead_stock_attribution_v1 无法确定呆滞库存快照日期")
    return str(value)


def _purchase_evidence(store: LandingStore, source: str, as_of: str) -> list[dict]:
    dead_stock = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    item = _active_table(store, source, "ITEM")
    po = _active_table(store, source, "PURCHASE_ORDER")
    po_d = _active_table(store, source, "PURCHASE_ORDER_D")
    po_sd = _active_table(store, source, "PURCHASE_ORDER_SD")
    po_sd1 = _active_table(store, source, "PURCHASE_ORDER_SD1")
    po_ssd = _active_table(store, source, "PURCHASE_ORDER_SSD")
    arrival_d = _active_table(store, source, "PURCHASE_ARRIVAL_D")
    supplier = _active_table(store, source, "SUPPLIER_PURCHASE")

    sql = f"""
    WITH demand AS (
      SELECT sd.PURCHASE_ORDER_D_ID AS po_line_id,
             SUM(COALESCE(ssd.DEMAND_QTY, 0)) AS demand_qty
      FROM {po_sd} sd
      JOIN {po_sd1} sd1 ON sd1.PURCHASE_ORDER_SD_ID = sd.Id
                           AND sd1._d2a_deleted_at IS NULL
      JOIN {po_ssd} ssd ON ssd.PURCHASE_ORDER_SD1_ID = sd1.Id
                           AND ssd._d2a_deleted_at IS NULL
      WHERE sd._d2a_deleted_at IS NULL
      GROUP BY sd.PURCHASE_ORDER_D_ID
    ), receipts AS (
      SELECT PURCHASE_ORDER_D_ID AS po_line_id,
             SUM(COALESCE(RECEIPTED_BUSINESS_QTY, 0)
                 - COALESCE(RETURNED_BUSINESS_QTY, 0)) AS net_received_qty
      FROM {arrival_d}
      WHERE _d2a_deleted_at IS NULL
      GROUP BY PURCHASE_ORDER_D_ID
    )
    SELECT p.DOC_NO, p.DOC_DATE, p.SUPPLIER_ID, p.Owner_Dept, p.Owner_Emp,
           l.SEQUENCE_NUMBER, l.PURCHASE_QTY, i.ITEM_CODE,
           MAX(sd.PLANT_ID) AS plant_id, demand.demand_qty,
           receipts.net_received_qty, sp.MOQ, ds.inventory_qty
    FROM {po_d} l
    JOIN {po} p ON p.Id = l.PURCHASE_ORDER_ID AND p._d2a_deleted_at IS NULL
    JOIN {item} i ON i.Id = l.ITEM_ID AND i._d2a_deleted_at IS NULL
    JOIN {dead_stock} ds ON ds.item_code = i.ITEM_CODE
                            AND ds.determination_status = 'dead_stock'
                            AND ds._d2a_deleted_at IS NULL
    LEFT JOIN {po_sd} sd ON sd.PURCHASE_ORDER_D_ID = l.Id
                            AND sd._d2a_deleted_at IS NULL
    LEFT JOIN demand ON demand.po_line_id = l.Id
    LEFT JOIN receipts ON receipts.po_line_id = l.Id
    LEFT JOIN {supplier} sp ON sp.SUPPLIER_ID = p.SUPPLIER_ID
                               AND sp.ITEM_ID = l.ITEM_ID
                               AND sp._d2a_deleted_at IS NULL
    WHERE l._d2a_deleted_at IS NULL
      AND ds.plant_id = sd.PLANT_ID
    GROUP BY p.Id, l.Id, ds.plant_id, ds.item_code
    """
    rows: list[dict] = []
    for row in store.con.execute(sql):
        demand = row["demand_qty"]
        received = row["net_received_qty"]
        moq = row["MOQ"]
        status = "ready"
        warnings = ["当前库存与采购单按工厂、品号间接关联，未证明批次级因果关系"]
        if demand is None or received is None or moq is None or float(moq) <= 0:
            status = "unknown"
            warnings.append("需求量、净收货量或 MOQ 缺失，未计算超采归因")
            demand_qty = net_received_qty = actual_excess = planned_excess = forced = manual = None
        else:
            demand_qty = float(demand)
            net_received_qty = max(float(received), 0)
            moq_qty = float(moq)
            actual_excess = max(net_received_qty - demand_qty, 0)
            planned_excess = max(math.ceil(demand_qty / moq_qty) * moq_qty - demand_qty, 0)
            forced = min(actual_excess, planned_excess)
            manual = max(actual_excess - forced, 0)
        rows.append({
            "plant_id": row["plant_id"], "po_no": row["DOC_NO"],
            "po_line_no": str(row["SEQUENCE_NUMBER"]), "item_code": row["ITEM_CODE"],
            "supplier_id": row["SUPPLIER_ID"], "po_date": row["DOC_DATE"],
            "demand_qty": demand_qty, "net_received_qty": net_received_qty,
            "purchase_qty": float(row["PURCHASE_QTY"] or 0),
            "moq": float(moq) if moq is not None else None,
            "actual_excess_qty": actual_excess,
            "planned_moq_excess_qty": planned_excess,
            "moq_forced_excess_qty": forced, "manual_excess_qty": manual,
            "inventory_qty": float(row["inventory_qty"] or 0),
            "trace_type": "indirect", "calculation_status": status,
            "related_department": row["Owner_Dept"], "related_employee": row["Owner_Emp"],
            "warnings": json.dumps(warnings, ensure_ascii=False), "as_of_date": as_of,
            "calculation_version": CALCULATION_VERSION,
        })
    return rows


def _production_evidence(store: LandingStore, source: str, as_of: str) -> list[dict]:
    dead_stock = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    item = _active_table(store, source, "ITEM")
    mo = _active_table(store, source, "MO")
    mo_d = _active_table(store, source, "MO_D")
    issued = _active_table(store, source, "MO_ISSUED_SETS")
    bom = _active_table(store, source, "BOM_D")
    sql = f"""
    WITH issues AS (
      SELECT MO_ID, ITEM_ID, SUM(COALESCE(ISSUED_QTY, 0)) AS issued_qty,
             SUM(COALESCE(RETURNED_QTY, 0)) AS returned_qty
      FROM {issued}
      WHERE _d2a_deleted_at IS NULL
      GROUP BY MO_ID, ITEM_ID
    ), bom_rule AS (
      SELECT PARENT_ITEM_ID, SUB_ITEM_FEATURE_ID,
             MAX(COALESCE(DENOMINATOR, 1)) AS denominator,
             MAX(COALESCE(FIXED_LOSS_RATE, 0) + COALESCE(DYNAMIC_LOSS_RATE, 0)
                 + COALESCE(ISSUE_OVERRUN_RATE, 0)) AS allowed_loss_rate
      FROM {bom}
      WHERE _d2a_deleted_at IS NULL
      GROUP BY PARENT_ITEM_ID, SUB_ITEM_FEATURE_ID
    )
    SELECT m.DOC_NO, m.STATUS, m.PLAN_QTY, m.COMPLETED_QTY,
           m.Owner_Dept, m.Owner_Emp, m.PLANT_ID,
           d.QTY_PER, i.ITEM_CODE, issues.issued_qty, issues.returned_qty,
           bom_rule.denominator, bom_rule.allowed_loss_rate
    FROM {mo_d} d
    JOIN {mo} m ON m.Id = d.MO_ID AND m._d2a_deleted_at IS NULL
    JOIN {item} i ON i.Id = d.ITEM_ID AND i._d2a_deleted_at IS NULL
    JOIN {dead_stock} ds ON ds.item_code = i.ITEM_CODE AND ds.plant_id = m.PLANT_ID
                            AND ds.determination_status = 'dead_stock'
                            AND ds._d2a_deleted_at IS NULL
    LEFT JOIN issues ON issues.MO_ID = d.MO_ID AND issues.ITEM_ID = d.ITEM_ID
    LEFT JOIN bom_rule ON bom_rule.PARENT_ITEM_ID = m.ITEM_ID
                           AND bom_rule.SUB_ITEM_FEATURE_ID = d.ITEM_ID
    WHERE d._d2a_deleted_at IS NULL
    """
    rows: list[dict] = []
    for row in store.con.execute(sql):
        closed = str(row["STATUS"]).lower() in {"closed", "completed", "结案"}
        output_basis = row["COMPLETED_QTY"] if closed else row["PLAN_QTY"]
        status = "ready" if closed else "provisional"
        warnings = ["当前库存与工单按工厂、品号间接关联，未证明批次级因果关系"]
        qty_per, denominator, loss_rate = row["QTY_PER"], row["denominator"], row["allowed_loss_rate"]
        issued_qty, returned_qty = row["issued_qty"], row["returned_qty"]
        if any(value is None for value in (output_basis, qty_per, denominator, loss_rate, issued_qty, returned_qty)) \
                or float(denominator or 0) <= 0:
            status = "unknown"
            warnings.append("工单产量、标准用量、领退料或允许损耗缺失，未计算超额领料")
            net_issued = standard_required = allowed_issue = excess_issue = None
        else:
            net_issued = max(float(issued_qty) - float(returned_qty), 0)
            standard_required = float(output_basis) * float(qty_per) / float(denominator)
            allowed_issue = standard_required * (1 + float(loss_rate))
            excess_issue = max(net_issued - allowed_issue, 0)
        rows.append({
            "plant_id": row["PLANT_ID"], "mo_no": row["DOC_NO"],
            "item_code": row["ITEM_CODE"], "mo_status": row["STATUS"],
            "output_basis_qty": float(output_basis) if output_basis is not None else None,
            "qty_per": float(qty_per) if qty_per is not None else None,
            "denominator": float(denominator) if denominator is not None else None,
            "allowed_loss_rate": float(loss_rate) if loss_rate is not None else None,
            "fixed_loss_qty": 0.0, "issued_qty": float(issued_qty) if issued_qty is not None else None,
            "returned_qty": float(returned_qty) if returned_qty is not None else None,
            "net_issued_qty": net_issued, "standard_required_qty": standard_required,
            "allowed_issue_qty": allowed_issue, "excess_issue_qty": excess_issue,
            "trace_type": "indirect", "calculation_status": status,
            "related_department": row["Owner_Dept"], "related_employee": row["Owner_Emp"],
            "warnings": json.dumps(warnings, ensure_ascii=False), "as_of_date": as_of,
            "calculation_version": CALCULATION_VERSION,
        })
    return rows


def _attributions(
    store: LandingStore, source: str, purchase_rows: list[dict], production_rows: list[dict], as_of: str,
) -> list[dict]:
    dead_stock = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    dead_rows = list(store.con.execute(
        f"SELECT plant_id, warehouse_code, item_code FROM {dead_stock} "
        "WHERE determination_status = 'dead_stock' AND _d2a_deleted_at IS NULL",
    ))
    warehouses: dict[tuple[str, str], list[str]] = {}
    for row in dead_rows:
        warehouses.setdefault((str(row["plant_id"]), str(row["item_code"])), []).append(
            str(row["warehouse_code"]))

    out: list[dict] = []

    def append_candidates(evidence: dict, object_name: str, candidates: list[tuple[str, float, str, dict]]) -> None:
        for root_cause, confidence, rule_version, summary in candidates:
            for warehouse in warehouses.get((str(evidence["plant_id"]), str(evidence["item_code"])), []):
                evidence_id = (
                    f"{evidence['po_no']}:{evidence['po_line_no']}"
                    if object_name == "PurchaseOverbuyEvidence" else evidence["mo_no"]
                )
                out.append({
                    "plant_id": evidence["plant_id"], "warehouse_code": warehouse,
                    "item_code": evidence["item_code"], "root_cause": root_cause,
                    "evidence_id": evidence_id, "confidence": confidence,
                    "confidence_level": "MEDIUM", "rule_version": rule_version,
                    "trace_type": evidence["trace_type"], "evidence_object": object_name,
                    "evidence_summary": json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    "related_department": evidence["related_department"],
                    "related_employee": evidence["related_employee"],
                    "warnings": evidence["warnings"], "as_of_date": as_of,
                    "calculation_version": CALCULATION_VERSION,
                })

    for evidence in purchase_rows:
        if evidence["calculation_status"] != "ready":
            continue
        actual = float(evidence["actual_excess_qty"] or 0)
        inventory = float(evidence["inventory_qty"] or 0)
        if actual <= inventory * 0.3:
            continue
        candidates: list[tuple[str, float, str, dict]] = []
        if float(evidence["moq_forced_excess_qty"] or 0) > 0:
            candidates.append(("R2", 0.7, "r2-v1", {
                "po_no": evidence["po_no"], "demand_qty": evidence["demand_qty"],
                "net_received_qty": evidence["net_received_qty"], "moq": evidence["moq"],
                "moq_forced_excess_qty": evidence["moq_forced_excess_qty"],
                "manual_excess_qty": evidence["manual_excess_qty"],
            }))
        if float(evidence["manual_excess_qty"] or 0) > 0:
            candidates.append(("R2M", 0.7, "r2m-v1", {
                "po_no": evidence["po_no"], "demand_qty": evidence["demand_qty"],
                "net_received_qty": evidence["net_received_qty"], "moq": evidence["moq"],
                "moq_forced_excess_qty": evidence["moq_forced_excess_qty"],
                "manual_excess_qty": evidence["manual_excess_qty"],
            }))
        append_candidates(evidence, "PurchaseOverbuyEvidence", candidates)

    for evidence in production_rows:
        if evidence["calculation_status"] != "ready" or float(evidence["excess_issue_qty"] or 0) <= 0:
            continue
        append_candidates(evidence, "ProductionLossEvidence", [("R5", 0.65, "r5-v1", {
            "mo_no": evidence["mo_no"], "output_basis_qty": evidence["output_basis_qty"],
            "standard_required_qty": evidence["standard_required_qty"],
            "net_issued_qty": evidence["net_issued_qty"],
            "allowed_issue_qty": evidence["allowed_issue_qty"],
            "excess_issue_qty": evidence["excess_issue_qty"],
        })])
    return out


def materialize_dead_stock_attribution(store: LandingStore, source: str) -> int:
    """按同一库存快照生成 M2 两类证据及逐标签归因结果。"""
    dead_stock = _active_table(store, source, DEAD_STOCK_ITEM_TABLE)
    as_of = _as_of_date(store, dead_stock)
    batch_id = f"mat_{date.fromisoformat(as_of):%Y%m%d}_m2"
    purchase_rows = _purchase_evidence(store, source, as_of)
    production_rows = _production_evidence(store, source, as_of)
    attribution_rows = _attributions(store, source, purchase_rows, production_rows, as_of)
    _replace_rows(store, source, _PURCHASE_INFO, purchase_rows, batch_id)
    _replace_rows(store, source, _PRODUCTION_INFO, production_rows, batch_id)
    _replace_rows(store, source, _ATTRIBUTION_INFO, attribution_rows, batch_id)
    return len(attribution_rows)
