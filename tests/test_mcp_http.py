"""MCP HTTP 安全件测试:Bearer 认证中间件、每工具限流、JSONL 审计。"""

import json
from pathlib import Path

import pytest

from data2agent.platform.mcp_server.http import (
    BearerAuthMiddleware,
    RateLimiter,
    jsonl_audit_sink,
)
from data2agent.shared.store.evidence import EvidenceContext

ROOT = Path(__file__).resolve().parents[1]


def _ctx() -> EvidenceContext:
    return EvidenceContext(
        principal="test:mcp-http",
        session_id="test_session_mcp_http_0001",
        channel="demo",
    )


def test_rate_limiter_sliding_window():
    limiter = RateLimiter(2)
    limiter.check("query_objects")
    limiter.check("query_objects")
    with pytest.raises(ValueError, match="限流"):
        limiter.check("query_objects")
    limiter.check("query_metrics")  # 每工具独立计数


def test_rate_limiter_disabled():
    limiter = RateLimiter(0)
    for _ in range(1000):
        limiter.check("query_objects")


def test_bearer_auth_middleware():
    import asyncio

    httpx = pytest.importorskip("httpx")

    async def dummy_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BearerAuthMiddleware(dummy_app, "s3cret")

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/mcp")
            assert r.status_code == 401 and "Token" in r.json()["detail"]
            bad = await client.get("/mcp", params={"token": "s3cret"})
            assert bad.status_code == 401, "只认 Authorization 头,不接受 URL 参数(避免 Token 进日志)"
            ok = await client.get("/mcp", headers={"Authorization": "Bearer s3cret"})
            assert ok.status_code == 200

    asyncio.run(scenario())


def test_audit_sink_records_tool_calls(tmp_path):
    from datetime import date

    from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
    from data2agent.shared.store.dataset_publish import build_dataset
    from data2agent.middle.extract.increment import incremental_sync
    from data2agent.shared.store.landing import LandingStore
    from data2agent.platform.mcp_server.core import QueryService
    from tests.helpers import watermarks_from_pack, whitelist_from_pack
    from data2agent.shared.metamodel.loader import load_pack
    from tests.fixtures.e10.seed import build, write_db

    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    incremental_sync(SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, "digiwin_e10")),
                     landing, "digiwin_e10", watermarks_from_pack(pack, "digiwin_e10"))
    result = build_dataset(landing, pack, "digiwin_e10", auto_publish=True)
    assert result.published

    audit_file = tmp_path / "gateway_audit.jsonl"
    svc = QueryService(
        landing.db_path,
        ROOT / "templates",
        audit_sink=jsonl_audit_sink(audit_file),
        default_context=_ctx(),
    )
    res = svc.query_objects("Customer", limit=1)
    svc.query_metrics("gross_margin_rate")
    svc.propose_action("Quotation", "quote_review", "结论",
                       [{
                           "claim": "x",
                           "query_id": res["meta"]["query_id"],
                           "result_digest": res["meta"]["result_digest"],
                       }])

    records = [json.loads(line) for line in audit_file.read_text().splitlines()]
    assert [r["tool"] for r in records] == ["query_objects", "query_metrics", "propose_action"]
    assert records[0]["target"] == "Customer" and records[2]["target"] == "Quotation.quote_review"
