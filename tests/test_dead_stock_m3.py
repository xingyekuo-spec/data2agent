"""呆滞库存 M3: R1/R3/R4/R6 归因与 M3c 转用候选。"""

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


def _published(tmp_path: Path) -> LandingStore:
    source_db = tmp_path / "e10.sqlite"
    write_db(source_db, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    whitelist = whitelist_from_pack(pack, SOURCE)
    assert {
        "SALES_ORDER_DOC", "SALES_ORDER_DOC_D", "PO_REQ_SOURCE", "ECN", "ECN_D",
        "ECN_SD", "ECN_TASK", "BOM_D", "MO", "ITEM",
    } <= whitelist
    adapter = SqliteReadOnlyAdapter(str(source_db), whitelist)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    return landing


def _ctx() -> EvidenceContext:
    return EvidenceContext(
        principal="test:dead-stock-m3", session_id="test_session_dead_stock_m3_0001", channel="demo",
    )


def test_m3_materializes_attribution_and_candidate_objects(tmp_path):
    landing = _published(tmp_path)
    snap = resolve_published_snapshot(landing, SOURCE)
    assert {
        "MaterialOrderEvidence", "EcnChangeEvidence", "SpecialConditionEvidence",
        "DuplicateMaterialCandidate", "MaterialBomUsage", "MaterialSubstituteCandidate",
    } <= set(snap.objects)

    orders = raw_table_name(SOURCE, "D2A_MATERIAL_ORDER_EVIDENCE")
    order = landing.con.execute(
        f'''SELECT * FROM "{orders}" WHERE calculation_status = 'ready' LIMIT 1'''
    ).fetchone()
    assert order is not None
    assert order["demand_event_type"] == "已取消"
    assert order["purchase_date"] < order["demand_event_date"]
    assert order["cancelled_or_reduced_qty"] > 0

    ecn = raw_table_name(SOURCE, "D2A_ECN_CHANGE_EVIDENCE")
    rows = list(landing.con.execute(f'SELECT * FROM "{ecn}"'))
    assert rows and all(row["handle"] != "run-out" for row in rows)
    assert all(row["effective_date"] <= "2026-07-10" for row in rows)

    table = snap.objects["DeadStockAttribution"].physical_table
    causes = {row["root_cause"] for row in landing.con.execute(f'SELECT root_cause FROM "{table}"')}
    assert {"R1", "R3", "R4", "R6"} <= causes
    assert not landing.con.execute(
        f'''SELECT 1 FROM "{table}" WHERE root_cause = 'R3' AND evidence_id LIKE '%-002' LIMIT 1'''
    ).fetchone()

    special = raw_table_name(SOURCE, "D2A_SPECIAL_CONDITION_EVIDENCE")
    special_row = landing.con.execute(
        f'''SELECT * FROM "{special}" WHERE restriction_text LIKE '%仅适用%' LIMIT 1'''
    ).fetchone()
    assert special_row is not None
    assert special_row["calculation_status"] == "candidate"

    duplicate = raw_table_name(SOURCE, "D2A_DUPLICATE_MATERIAL_CANDIDATE")
    duplicate_row = landing.con.execute(
        f'''SELECT * FROM "{duplicate}" WHERE match_method = 'normalized_exact_specification' LIMIT 1'''
    ).fetchone()
    assert duplicate_row is not None
    assert duplicate_row["item_code"] != duplicate_row["candidate_item_code"]

    bom_usage = raw_table_name(SOURCE, "D2A_MATERIAL_BOM_USAGE")
    usage_row = landing.con.execute(
        f'''SELECT * FROM "{bom_usage}" WHERE potential_required_qty > 0 LIMIT 1'''
    ).fetchone()
    assert usage_row is not None

    substitute = raw_table_name(SOURCE, "D2A_MATERIAL_SUBSTITUTE_CANDIDATE")
    candidate = landing.con.execute(
        f'''SELECT * FROM "{substitute}" WHERE candidate_type = 'bom_consumption' LIMIT 1'''
    ).fetchone()
    assert candidate is not None
    assert candidate["potential_consume_qty"] > 0


def test_mcp_exposes_m3_evidence_and_high_confidence_labels(tmp_path):
    landing = _published(tmp_path)
    svc = QueryService(landing.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx())
    attrs = svc.query_objects("DeadStockAttribution", filters={"confidence_level": "HIGH"}, limit=50)
    assert {row["root_cause"] for row in attrs["rows"]} >= {"R1", "R3"}
    assert all(row["related_employee"] == MASK for row in attrs["rows"])

    order = svc.query_objects("MaterialOrderEvidence", limit=20)
    ecn = svc.query_objects("EcnChangeEvidence", limit=20)
    assert order["rows"] and ecn["rows"]
    assert all(row["related_employee"] == MASK for row in order["rows"] + ecn["rows"])

    distribution = svc.query_metrics("attribution_distribution", group_by="根因", limit=20)
    assert {row["group"] for row in distribution["rows"]} >= {"R1", "R3", "R4", "R6"}

    lows = svc.query_objects("DeadStockAttribution", filters={"confidence_level": "LOW"}, limit=50)
    assert {row["root_cause"] for row in lows["rows"]} >= {"R4", "R6"}

    special = svc.query_objects("SpecialConditionEvidence", limit=20)
    duplicate = svc.query_objects("DuplicateMaterialCandidate", limit=20)
    bom_usage = svc.query_objects("MaterialBomUsage", limit=20)
    substitute = svc.query_objects("MaterialSubstituteCandidate", limit=20)
    assert special["rows"] and duplicate["rows"] and bom_usage["rows"] and substitute["rows"]

    consumable = svc.query_metrics("substitute_consumable_quantity", group_by="来源工厂", limit=20)
    assert consumable["implemented"]
    assert consumable["rows"] and consumable["rows"][0]["value"] > 0
