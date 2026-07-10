"""QueryService(MCP 工具核心)测试:对象查询、脱敏、值映射、指标取数。"""

from datetime import date
from pathlib import Path

import pytest

from data2agent.mcp_server.core import MASK, QueryService
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def svc(tmp_path_factory) -> QueryService:
    db = tmp_path_factory.mktemp("showroom") / "e10.sqlite"
    write_db(db, build(seed=42, asof=date(2026, 7, 10)))
    return QueryService(db, ROOT / "templates")


def test_object_catalog(svc):
    catalog = svc.query_objects()
    names = {o["object"] for o in catalog["objects"]}
    assert names == {"Customer", "Material", "Quotation", "SalesOrder", "SalesOrderLine"}


def test_query_rows_with_joined_code(svc):
    res = svc.query_objects("SalesOrder", limit=5)
    assert res["meta"]["binding_status"] == "draft" and res["meta"]["note"]
    assert len(res["rows"]) == 5
    row = res["rows"][0]
    assert row["currency"] in {"USD", "EUR", "JPY", "CNY"}, "币别应经 join 解码为编码"
    assert row["customer"].startswith("C"), "客户应解码为客户编号"


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
    assert by_id["overdue_receivable_amount"]["implemented"] is False


def test_gross_margin_by_month(svc):
    res = svc.query_metrics("gross_margin_rate")
    assert res["implemented"] and res["group_by"] == "月"
    assert res["meta"]["warning"], "draft 指标必须带口径警示"
    for row in res["rows"]:
        assert 0 < row["value"] < 1


def test_quote_response_by_customer(svc):
    res = svc.query_metrics("quote_response_hours", group_by="客户", limit=5)
    assert res["rows"] and all(r["value"] > 0 for r in res["rows"])
    with pytest.raises(ValueError, match="group_by"):
        svc.query_metrics("quote_response_hours", group_by="品类")


def test_unimplemented_metric_explains(svc):
    res = svc.query_metrics("overdue_receivable_amount")
    assert res["implemented"] is False and "应收" in res["reason"]


def test_mcp_tool_wiring(svc, tmp_path):
    """FastMCP 装配冒烟:两个只读工具按名注册(无 mcp 包时跳过)。"""
    pytest.importorskip("mcp")
    import asyncio

    from data2agent.mcp_server.server import create_server

    db = tmp_path / "e10.sqlite"
    write_db(db, build(seed=42, asof=date(2026, 7, 10)))
    server = create_server(db, ROOT / "templates")
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"query_objects", "query_metrics"}
