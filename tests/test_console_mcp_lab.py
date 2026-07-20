"""M6 MCP Lab 契约与边界测试。

M6-T01:先冻结 OpenAPI/模型语义;运行时长生命周期服务与 gateway 实现属后续任务。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.config import load_config
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_apply import apply_objects
from data2agent.connect.sync import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import (
    McpCallBody,
    McpLabError,
    McpQueryMeta,
    ProposalRequest,
    ProposalResponse,
)
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE), audit_hook=hook)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    apply_objects(landing, pack, SOURCE)
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n",
        encoding="utf-8")
    return landing, cfg_file


def _openapi(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    app = create_app(cfg.landing, cfg.templates, cfg)
    return app.openapi()


def test_mcp_query_meta_schema_frozen(env):
    """McpQueryMeta 进入 OpenAPI,并冻结 process 级 evidence_scope。"""
    schemas = _openapi(env)["components"]["schemas"]
    assert "McpQueryMeta" in schemas
    meta = schemas["McpQueryMeta"]
    props = meta["properties"]
    for key in (
        "query_id", "tool", "target", "row_count", "duration_ms",
        "masked_fields", "warnings", "evidence_scope",
    ):
        assert key in props, f"McpQueryMeta missing {key}"
    scope = props["evidence_scope"]
    assert scope.get("const") == "process" or scope.get("enum") == ["process"]
    sample = McpQueryMeta(
        query_id="q1", tool="query_objects", target="Customer",
        row_count=1, duration_ms=12, masked_fields=["phone"], warnings=[])
    assert sample.evidence_scope == "process"


def test_mcp_lab_error_schema_frozen(env):
    """McpLabError 进入 OpenAPI,reason_code 枚举完整。"""
    schemas = _openapi(env)["components"]["schemas"]
    assert "McpLabError" in schemas
    err = schemas["McpLabError"]
    props = err["properties"]
    for key in ("detail", "reason_code", "tool", "retryable", "error_id"):
        assert key in props, f"McpLabError missing {key}"
    reason = props["reason_code"]
    enum = reason.get("enum") or reason.get("const")
    expected = {
        "invalid_params", "unknown_target", "not_materialized", "query_expired",
        "tier_forbidden", "rate_limited", "mcp_unavailable", "execution_failed",
    }
    assert set(enum) == expected
    parsed = McpLabError(
        detail="x", reason_code="query_expired", tool="query_objects",
        retryable=False, error_id=None)
    assert parsed.retryable is False


def test_mcp_call_error_responses_use_mcp_lab_error(env):
    """mcp-call 业务/上游错误响应声明 McpLabError(非自由文本唯一出口)。"""
    spec = _openapi(env)
    op = spec["paths"]["/api/debug/mcp-call"]["post"]
    for code in ("403", "409", "429", "502", "503"):
        assert code in op["responses"], f"mcp-call missing {code}"
        schema = op["responses"][code]["content"]["application/json"]["schema"]
        ref = schema.get("$ref", "")
        assert ref.endswith("/McpLabError"), f"{code} should use McpLabError, got {schema}"


def test_proposal_schema_frozen_and_still_stub(env):
    """建议卡契约保持 ProposalResponse;T01 运行时仍为 501 桩。"""
    schemas = _openapi(env)["components"]["schemas"]
    assert "ProposalRequest" in schemas
    assert "ProposalResponse" in schemas
    assert "ProposalEvidence" in schemas
    resp_props = schemas["ProposalResponse"]["properties"]
    assert "governance" in resp_props
    assert "evidence" in resp_props
    assert "tier" in resp_props

    landing, cfg_file = env
    cfg = load_config(cfg_file)
    client = TestClient(create_app(cfg.landing, cfg.templates, cfg))
    r = client.post("/api/gateway/proposals", json={
        "object": "SalesOrder", "action": "review", "conclusion": "需复核",
        "evidence": [{"claim": "订单偏高", "query_id": "q1"}],
    })
    assert r.status_code == 501
    assert "契约桩" in r.json()["detail"]

    # 请求模型仍拒绝空 evidence
    with pytest.raises(ValidationError):
        ProposalRequest(
            object="SalesOrder", action="review", conclusion="c", evidence=[])


def test_mcp_call_whitelist_rejects_propose_action(env):
    """Jinja/Vue 共用 debug 入口白名单不扩大到 propose_action。"""
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    client = TestClient(create_app(cfg.landing, cfg.templates, cfg))
    r = client.post("/api/debug/mcp-call", json={"tool": "propose_action", "params": {}})
    assert r.status_code == 422
    body = _openapi(env)
    tool_schema = body["components"]["schemas"]["McpCallBody"]["properties"]["tool"]
    assert tool_schema.get("enum") == ["query_objects", "query_metrics"]
    assert set(McpCallBody.model_fields["tool"].annotation.__args__) == {
        "query_objects", "query_metrics",
    }


def test_mcp_query_meta_required_on_typed_data_results(env):
    """对象/指标数据结果模型要求携带 McpQueryMeta。"""
    schemas = _openapi(env)["components"]["schemas"]
    assert "McpObjectQueryResult" in schemas
    assert "McpMetricsQueryResult" in schemas
    obj_meta_ref = schemas["McpObjectQueryResult"]["properties"]["meta"]["$ref"]
    met_meta_ref = schemas["McpMetricsQueryResult"]["properties"]["meta"]["$ref"]
    assert obj_meta_ref.endswith("/McpQueryMeta")
    assert met_meta_ref.endswith("/McpQueryMeta")


def test_proposal_response_governance_is_say_tier(env):
    """governance 文案冻结为说档、未执行写操作语义。"""
    sample = ProposalResponse.model_validate({
        "proposal_id": "p1",
        "at": "2026-07-21T00:00:00+00:00",
        "object": "SalesOrder",
        "action": "review",
        "action_desc": "复核",
        "tier": "说",
        "conclusion": "需人工复核",
        "evidence": [{
            "claim": "金额偏高",
            "query": {
                "query_id": "q1",
                "tool": "query_objects",
                "target": "SalesOrder",
                "at": "2026-07-21T00:00:00+00:00",
            },
        }],
        "caveats": [],
        "governance": "「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理",
    })
    assert "说" in sample.governance
    assert "未执行" in sample.governance
