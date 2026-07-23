"""QueryService(MCP 工具核心)测试:走完整管道(seed → sync → apply)后查对象层。"""

import gc
import weakref
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.sync import whitelist_from_pack
from data2agent.mcp_server.core import MASK, QueryService
from data2agent.mcp_server.evidence import (
    EvidenceContext,
    EvidenceStore,
    QueryEvidenceRecord,
    canonical_json_dumps,
)
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


def _ctx(session_id: str = "test_session_mcp_core_0001") -> EvidenceContext:
    return EvidenceContext(
        principal="test:mcp-core",
        session_id=session_id,
        channel="demo",
    )


def _pipeline(dirpath: Path) -> Path:
    """seed → sync → build_dataset(auto_publish),返回落地库路径。"""
    src = dirpath / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(dirpath / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published and result.dataset_version
    return dirpath / "landing.sqlite"


@pytest.fixture(scope="module")
def svc(tmp_path_factory) -> QueryService:
    db = _pipeline(tmp_path_factory.mktemp("pipeline"))
    return QueryService(db, ROOT / "templates", default_context=_ctx())


def test_object_catalog(svc):
    catalog = svc.query_objects()
    names = {o["object"] for o in catalog["objects"]}
    assert names == {
        "Customer", "DeadStockAttribution", "DeadStockItem",
        "DuplicateMaterialCandidate", "EcnChangeEvidence", "Material",
        "MaterialBomUsage", "MaterialOrderEvidence", "MaterialSubstituteCandidate",
        "ProductionLossEvidence", "PurchaseOverbuyEvidence", "Quotation",
        "SalesOrder", "SalesOrderLine", "SpecialConditionEvidence",
    }


def test_query_rows_with_joined_code(svc):
    res = svc.query_objects("SalesOrder", limit=5)
    assert res["meta"]["binding_status"] == "draft" and res["meta"]["note"]
    assert len(res["rows"]) == 5
    row = res["rows"][0]
    assert row["currency"] in {"USD", "EUR", "JPY", "CNY"}, "币别应经 join 解码为编码"
    assert row["customer"].startswith("C"), "客户应解码为客户编号"


def test_empty_object_query_persists_frozen_template_columns(svc):
    """无结果 evidence 仍要说明查询的对象字段形状，不能退化为 columns=[]。"""
    result = svc.query_objects("Customer", filters={"customer_code": "C-NOT-FOUND"})
    assert result["rows"] == []
    summary = result["meta"]["result_summary"]
    assert summary["returned_row_count"] == 0
    assert summary["columns"] == [
        "customer_code", "name", "region", "currency", "payment_days", "contact",
    ]


def test_enum_filter_uses_object_values(svc):
    res = svc.query_objects("Quotation", filters={"result": "成交"}, limit=200)
    assert res["rows"] and all(r["result"] == "成交" for r in res["rows"])
    with pytest.raises(ValueError, match="取值须为"):
        svc.query_objects("Quotation", filters={"result": "W"})  # 源系统编码不应暴露


def test_sensitive_fields_masked(svc):
    res = svc.query_objects("Customer", limit=3)
    assert res["meta"]["masked_fields"] == ["contact"]
    assert all(r["contact"] == MASK for r in res["rows"])
    res = svc.query_objects("Material", limit=3)
    assert all(r["standard_cost"] == MASK for r in res["rows"])


def test_order_by(svc):
    res = svc.query_objects("SalesOrder", order_by="total_amount", desc=True, limit=10)
    amounts = [r["total_amount"] for r in res["rows"]]
    assert amounts == sorted(amounts, reverse=True)


def test_unknown_object_lists_available(svc):
    with pytest.raises(ValueError, match="未知对象"):
        svc.query_objects("Nope")


def test_metric_catalog(svc):
    catalog = svc.query_metrics()
    by_id = {m["metric"]: m for m in catalog["metrics"]}
    assert by_id["gross_margin_rate"]["implemented"] is True
    assert by_id["substitute_consumable_quantity"]["implemented"] is True
    assert by_id["overdue_receivable_amount"]["implemented"] is False


def test_gross_margin_by_month(svc):
    res = svc.query_metrics("gross_margin_rate")
    assert res["implemented"] and res["group_by"] == "月"
    assert res["meta"]["warning"], "draft 指标必须带口径警示"
    for row in res["rows"]:
        assert 0 < row["value"] < 1


def test_gross_margin_equivalent_after_state_migration(svc):
    """回归锚点:指标改用派生状态过滤后,数值须与 raw 穿透版完全一致。"""
    res = svc.query_metrics("gross_margin_rate", group_by="客户", limit=200)
    c015 = next(r for r in res["rows"] if str(r["group"]).startswith("C015"))
    assert c015["value"] == 0.3085  # E2 起历次验证的基准值(seed=42)


def test_state_filter_via_gateway(svc):
    res = svc.query_objects("SalesOrder", filters={"state": "已作废"}, limit=200)
    assert res["rows"] and all(r["state"] == "已作废" for r in res["rows"])
    with pytest.raises(ValueError, match="取值须为"):
        svc.query_objects("SalesOrder", filters={"state": "Y"})  # 源码值不出网


def test_quote_response_by_customer(svc):
    res = svc.query_metrics("quote_response_hours", group_by="客户", limit=5)
    assert res["rows"] and all(r["value"] > 0 for r in res["rows"])
    with pytest.raises(ValueError, match="group_by"):
        svc.query_metrics("quote_response_hours", group_by="品类")


def test_unimplemented_metric_explains(svc):
    res = svc.query_metrics("overdue_receivable_amount")
    assert res["implemented"] is False and "应收" in res["reason"]


def test_query_ids_are_traceable(svc):
    a = svc.query_objects("Customer", limit=1)["meta"]["query_id"]
    b = svc.query_metrics("gross_margin_rate")["meta"]["query_id"]
    assert a != b and a.startswith("qry_") and b.startswith("qry_")


def test_query_success_persists_evidence_and_audit(svc):
    res = svc.query_objects("Customer", limit=1)
    meta = res["meta"]
    landing = LandingStore(svc.db_path)
    try:
        evidence = landing.get_gateway_query_evidence(meta["query_id"])
        assert evidence is not None
        assert evidence.session_id == meta["session_id"]
        assert evidence.result_digest == meta["result_digest"]
        assert evidence.dataset_version == meta["dataset_version"]
        audits = landing.list_gateway_audit(session_id=meta["session_id"])
        assert any(a.query_id == meta["query_id"] and a.reason_code == "ok" for a in audits)
    finally:
        landing.con.close()


def test_propose_action_card(svc):
    cust = svc.query_objects("Customer", filters={"customer_code": "C002"})
    margin = svc.query_metrics("gross_margin_rate", group_by="客户")
    card = svc.propose_action(
        "Quotation", "quote_review", "谨慎接 —— 参考结论",
        [
            {
                "claim": "C002 账期 90 天",
                "query_id": cust["meta"]["query_id"],
                "result_digest": cust["meta"]["result_digest"],
            },
            {
                "claim": "历史毛利约 29.6%",
                "query_id": margin["meta"]["query_id"],
                "result_digest": margin["meta"]["result_digest"],
            },
        ],
    )
    assert card["tier"] == "说" and card["proposal_id"].startswith("prp_")
    assert len(card["evidence"]) == 2
    assert card["evidence"][0]["query"]["tool"] == "query_objects"
    assert card["evidence"][0]["query"]["result_digest"].startswith("sha256:")
    assert any("draft" in c for c in card["caveats"]), "口径警示应从被引用查询聚合而来"
    assert "未执行任何写操作" in card["governance"]

    landing = LandingStore(svc.db_path)
    try:
        proposal = landing.get_gateway_proposal(card["proposal_id"])
        snapshots = landing.list_gateway_proposal_evidence(card["proposal_id"])
        assert proposal is not None
        assert proposal.session_id == svc.default_context.session_id
        assert proposal.dataset_version == card["dataset_version"]
        assert len(snapshots) == 2
    finally:
        landing.con.close()


def test_propose_action_rejects_untraceable_evidence(svc):
    with pytest.raises(ValueError, match="query_expired"):
        svc.propose_action("Quotation", "quote_review", "结论",
                           [{"claim": "编造的数字", "query_id": "qry_missing", "result_digest": "sha256:" + "0" * 64}])
    with pytest.raises(ValueError, match="不能为空"):
        svc.propose_action("Quotation", "quote_review", "结论", [])


def test_propose_action_rejects_digest_session_and_dataset_mismatch(svc):
    cust = svc.query_objects("Customer", filters={"customer_code": "C002"})
    margin = svc.query_metrics("gross_margin_rate", group_by="客户")
    other = QueryService(
        svc.db_path,
        ROOT / "templates",
        default_context=_ctx("test_session_other_mcp_core_0001"),
    )
    foreign = other.query_objects("Customer", filters={"customer_code": "C002"})

    with pytest.raises(ValueError, match="result_digest_mismatch"):
        svc.propose_action(
            "Quotation",
            "quote_review",
            "结论",
            [{
                "claim": "错误 digest",
                "query_id": cust["meta"]["query_id"],
                "result_digest": "sha256:" + "f" * 64,
            }],
        )

    with pytest.raises(ValueError, match="evidence_session_mismatch"):
        svc.propose_action(
            "Quotation",
            "quote_review",
            "结论",
            [{
                "claim": "跨会话",
                "query_id": foreign["meta"]["query_id"],
                "result_digest": foreign["meta"]["result_digest"],
            }],
        )

    landing = LandingStore(svc.db_path)
    try:
        landing.insert_gateway_query_evidence(
            QueryEvidenceRecord(
                query_id="qry_dataset_other",
                principal=svc.default_context.principal,
                session_id=svc.default_context.session_id,
                channel=svc.default_context.channel,
                source=svc.source,
                tool="query_objects",
                target="Customer",
                normalized_query_json=canonical_json_dumps(
                    {"tool": "query_objects", "source": svc.source, "object": "Customer"}
                ),
                dataset_version="ds_other",
                template_version="0.1.0",
                binding_hashes_json=canonical_json_dumps({"Customer": "sha256:abc"}),
                result_digest="sha256:" + "1" * 64,
                result_summary_json=canonical_json_dumps(
                    {"kind": "query_objects", "returned_row_count": 1, "rows_preview": []}
                ),
                warnings_json=canonical_json_dumps([]),
                row_count=1,
                created_at="2026-07-22T10:00:00+00:00",
                expires_at="2099-07-23T10:00:00+00:00",
            ),
        )
        with pytest.raises(ValueError, match="dataset_version_mismatch"):
            svc.propose_action(
                "Quotation",
                "quote_review",
                "结论",
                [
                    {
                        "claim": "客户档案",
                        "query_id": cust["meta"]["query_id"],
                        "result_digest": cust["meta"]["result_digest"],
                    },
                    {
                        "claim": "混用其它 dataset",
                        "query_id": "qry_dataset_other",
                        "result_digest": "sha256:" + "1" * 64,
                    },
                ],
            )
    finally:
        landing.con.close()


def test_propose_action_unknown_action_lists_available(svc):
    with pytest.raises(ValueError, match="quote_review"):
        svc.propose_action(
            "Quotation",
            "nope",
            "结论",
            [{"claim": "x", "query_id": "qry_1", "result_digest": "sha256:" + "0" * 64}],
        )


def test_tier_ceiling_enforced(svc):
    view_only = QueryService(
        svc.db_path, ROOT / "templates", max_tier="看", default_context=_ctx("test_session_view_only_0001"),
    )
    q = view_only.query_objects("Customer", limit=1)
    with pytest.raises(ValueError, match="档位上限"):
        view_only.propose_action("Quotation", "quote_review", "结论",
                                 [{
                                     "claim": "x",
                                     "query_id": q["meta"]["query_id"],
                                     "result_digest": q["meta"]["result_digest"],
                                 }])


def test_review_demo_chain(svc):
    from data2agent.showroom.review_demo import build_review, render_card

    card = build_review(svc, "C002", "矶钓竿", 2000, 28.0)
    assert card["action"] == "quote_review" and card["tier"] == "说"
    assert card["conclusion"].split(" ")[0] in {"接", "谨慎接", "不接"}
    assert len(card["evidence"]) == 3, "客户档案 / 历史成交 / 毛利基线三条依据"
    text = render_card(card, "C002 · 矶钓竿 · 2000 支 · 目标价 28")
    assert "接单评审建议卡" in text and "口径警示" in text


def test_object_layer_not_published_rejects(tmp_path):
    empty = LandingStore(tmp_path / "empty.sqlite")  # 只有系统表,无 published
    svc = QueryService(tmp_path / "empty.sqlite", ROOT / "templates")
    with pytest.raises(ValueError, match="not_published"):
        svc.query_objects("SalesOrder")
    assert empty  # fixture 保持连接存活


def test_concurrent_query_ids_unique(svc):
    """并发查询不得产生重复 query_id。"""
    import concurrent.futures

    def once():
        return svc.query_objects("Customer", limit=1)["meta"]["query_id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: once(), range(40)))
    assert len(ids) == len(set(ids))
    assert all(i.startswith("qry_") for i in ids)


def test_mcp_tool_wiring(svc):
    """FastMCP 装配冒烟:两个只读工具按名注册(无 mcp 包时跳过)。"""
    pytest.importorskip("mcp")
    import asyncio

    from data2agent.mcp_server.server import create_server

    server = create_server(svc.db_path, ROOT / "templates")
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"query_objects", "query_metrics", "propose_action"}


def test_mcp_session_context_tracks_session_objects_without_retaining_them():
    """旧 ServerSession 释放后，其 evidence session 不能被新连接继承。"""
    from data2agent.mcp_server.server import _SessionEvidenceContextResolver

    class FakeSession:
        pass

    resolver = _SessionEvidenceContextResolver(principal="mcp:test", channel="mcp_stdio")
    first = FakeSession()
    first_ref = weakref.ref(first)
    first_context = resolver.context_for(first)
    assert resolver.context_for(first).session_id == first_context.session_id

    del first
    gc.collect()
    assert first_ref() is None

    second_context = resolver.context_for(FakeSession())
    assert second_context.session_id != first_context.session_id


def test_invalid_proposals_are_persistently_audited(svc):
    """输入校验阶段的拒绝也必须可追溯，不能只审计查询存在后的拒绝。"""
    context = _ctx("test_session_mcp_core_reject_audit")
    with pytest.raises(ValueError, match="claim/query_id/result_digest"):
        svc.propose_action(
            "Quotation", "quote_review", "结论",
            [{"claim": "缺少摘要", "query_id": "qry_missing"}],
            context=context,
        )
    with pytest.raises(ValueError, match="未知对象"):
        svc.propose_action(
            "Unknown", "quote_review", "结论", [], context=context,
        )
    with pytest.raises(ValueError, match="result_digest 格式非法"):
        svc.propose_action(
            "Quotation", "quote_review", "结论",
            [{
                "claim": "格式错误",
                "query_id": "qry_missing",
                "result_digest": "not-a-digest",
            }],
            context=context,
        )

    landing = LandingStore(svc.db_path)
    try:
        records = EvidenceStore(landing).list_audit(
            principal=context.principal,
            session_id=context.session_id,
        )
    finally:
        landing.con.close()
    assert sorted((r.outcome, r.reason_code, r.target) for r in records) == sorted([
        ("rejected", "invalid_params", "Quotation.quote_review"),
        ("rejected", "unknown_target", "Unknown.quote_review"),
        ("rejected", "invalid_params", "Quotation.quote_review"),
    ])
