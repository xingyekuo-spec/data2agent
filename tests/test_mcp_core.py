"""QueryService(MCP 工具核心)测试:走完整管道(seed → sync → apply)后查对象层。"""

from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_apply import apply_objects
from data2agent.connect.sync import whitelist_from_pack
from data2agent.mcp_server.core import MASK, QueryService
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


def _pipeline(dirpath: Path) -> Path:
    """seed → sync → apply,返回落地库路径。"""
    src = dirpath / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(dirpath / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    report = apply_objects(landing, pack, SOURCE)
    assert not report.aborted
    return dirpath / "landing.sqlite"


@pytest.fixture(scope="module")
def svc(tmp_path_factory) -> QueryService:
    db = _pipeline(tmp_path_factory.mktemp("pipeline"))
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


def test_object_layer_not_materialized_guides_user(tmp_path):
    empty = LandingStore(tmp_path / "empty.sqlite")  # 只有系统表,无 obj_*
    svc = QueryService(tmp_path / "empty.sqlite", ROOT / "templates")
    with pytest.raises(ValueError, match="尚未物化"):
        svc.query_objects("SalesOrder")
    assert empty  # fixture 保持连接存活


def test_mcp_tool_wiring(svc):
    """FastMCP 装配冒烟:两个只读工具按名注册(无 mcp 包时跳过)。"""
    pytest.importorskip("mcp")
    import asyncio

    from data2agent.mcp_server.server import create_server

    server = create_server(svc.db_path, ROOT / "templates")
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"query_objects", "query_metrics"}
