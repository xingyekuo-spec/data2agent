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
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
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
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
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
        "invalid_params", "unknown_target", "not_materialized", "not_published",
        "query_expired", "tier_forbidden", "rate_limited", "mcp_unavailable",
        "execution_failed",
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


def test_proposal_schema_frozen(env):
    """建议卡契约保持 ProposalResponse;运行时不再是 501 桩。"""
    schemas = _openapi(env)["components"]["schemas"]
    assert "ProposalRequest" in schemas
    assert "ProposalResponse" in schemas
    assert "ProposalEvidence" in schemas
    resp_props = schemas["ProposalResponse"]["properties"]
    assert "governance" in resp_props
    assert "evidence" in resp_props
    assert "tier" in resp_props
    op = _openapi(env)["paths"]["/api/gateway/proposals"]["post"]
    assert "501" not in op["responses"]

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


# ---- M6-T02: 长生命周期 QueryService ----


def _client(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    app = create_app(cfg.landing, cfg.templates, cfg)
    return TestClient(app), app, cfg


def test_mcp_call_reuses_query_service_across_requests(env):
    """同一 Console 进程内连续查询应递增 query_id(共享 QueryService)。"""
    client, _app, _cfg = _client(env)
    r1 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}})
    r2 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    q1 = r1.json()["meta"]["query_id"]
    q2 = r2.json()["meta"]["query_id"]
    assert q1 and q2 and q1 != q2
    assert q1.startswith("q") and q2.startswith("q")


def test_shared_query_service_allows_propose_after_mcp_call(env):
    """mcp-call 写入的 query_id 可被同实例 propose_action 引用。"""
    client, app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}})
    assert r.status_code == 200, r.text
    qid = r.json()["meta"]["query_id"]
    svc = app.state.d2a_state["query_service"]
    assert svc is not None
    card = svc.propose_action(
        "Quotation", "quote_review", "需复核报价",
        [{"claim": "报价行可见", "query_id": qid}])
    assert card["proposal_id"]
    assert card["evidence"][0]["query"]["query_id"] == qid


def test_query_service_resets_when_config_signature_changes(env, tmp_path):
    """签名变化后旧 query 日志清空,且新 ID 不得与旧 ID 重号(避免 evidence 错绑)。"""
    import shutil

    client, app, _cfg = _client(env)
    r1 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}})
    r1b = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}})
    assert r1.status_code == 200 and r1b.status_code == 200
    old_ids = {r1.json()["meta"]["query_id"], r1b.json()["meta"]["query_id"]}
    assert len(old_ids) == 2
    old_svc = app.state.d2a_state["query_service"]
    stale_qid = r1b.json()["meta"]["query_id"]  # 仅存在于旧服务日志

    # 换一套等价 templates 路径 → 配置签名变化 → 原子替换服务并清空日志
    new_templates = tmp_path / "templates-reloaded"
    shutil.copytree(ROOT / "templates", new_templates)
    app.state.d2a_state["templates"] = str(new_templates)

    r2 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}})
    assert r2.status_code == 200, r2.text
    new_qid = r2.json()["meta"]["query_id"]
    assert new_qid not in old_ids
    new_svc = app.state.d2a_state["query_service"]
    assert new_svc is not old_svc
    with pytest.raises(ValueError, match="无法溯源"):
        new_svc.propose_action(
            "Quotation", "quote_review", "新服务不应看见旧 ID",
            [{"claim": "x", "query_id": stale_qid}])
    # 旧 evidence 经 gateway 也必须失败,不能因重号误绑到新查询
    expired = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "旧 evidence",
        "evidence": [{"claim": "x", "query_id": stale_qid}],
    })
    assert expired.status_code == 409
    assert expired.json()["reason_code"] == "query_expired"


def test_mcp_call_invalid_filters_shape_returns_invalid_params(env):
    """filters 非对象不得伪装为 mcp_unavailable。"""
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects",
        "params": {"object": "Customer", "filters": [1]},
    })
    assert r.status_code == 422, r.text
    err = McpLabError.model_validate(r.json())
    assert err.reason_code == "invalid_params"
    assert err.tool == "query_objects"
    assert err.retryable is False


@pytest.mark.parametrize(
    "params",
    [
        {"object": "Customer", "limit": "bad"},
        {"object": "Customer", "filters": {"region": {"bad": 1}}},
        {"object": ["Customer"]},
    ],
)
def test_mcp_call_malformed_params_return_invalid_params(env, params):
    """工具参数类型错误统一 422 invalid_params,不得 500/503/误报 unknown_target。"""
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": params,
    })
    assert r.status_code == 422, r.text
    err = McpLabError.model_validate(r.json())
    assert err.reason_code == "invalid_params"
    assert err.tool == "query_objects"


@pytest.mark.parametrize(
    "params",
    [
        {"filters": [1]},
        {"limit": "bad"},
        {"desc": [1]},
    ],
)
def test_mcp_call_object_catalog_rejects_bad_params(env, params):
    """未指定 object 的目录查询仍须校验其余参数,不得伪装成 200 目录。"""
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": params,
    })
    assert r.status_code == 422, r.text
    err = McpLabError.model_validate(r.json())
    assert err.reason_code == "invalid_params"


def test_mcp_lab_endpoints_return_mcp_lab_error_when_needs_setup(tmp_path):
    """未完成首次配置时 mcp-call/proposal 须返回 McpLabError,而非裸 detail。"""
    import shutil

    from data2agent.admin_common.home_layout import HomeLayout
    from data2agent.console.contracts import McpLabError

    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    shutil.copytree(ROOT / "templates", home.app / "templates")
    client = TestClient(create_app(home=home.root))

    call = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer"},
    })
    assert call.status_code == 409, call.text
    err = McpLabError.model_validate(call.json())
    assert err.reason_code == "mcp_unavailable"
    assert err.tool == "query_objects"

    prop = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "x",
        "evidence": [{"claim": "c", "query_id": "q1"}],
    })
    assert prop.status_code == 409, prop.text
    err2 = McpLabError.model_validate(prop.json())
    assert err2.reason_code == "mcp_unavailable"
    assert err2.tool == "propose_action"


def test_openapi_mcp_call_declares_mcp_lab_error_statuses(tmp_path):
    """mcp-call OpenAPI 须声明 422/404/500 为 McpLabError,而非 HTTPValidationError。"""
    landing = tmp_path / "empty.sqlite"
    landing.touch()
    app = create_app(str(landing), ROOT / "templates")
    op = app.openapi()["paths"]["/api/debug/mcp-call"]["post"]["responses"]
    for code in ("404", "422", "500"):
        assert code in op, f"missing {code}"
        schema = op[code]["content"]["application/json"]["schema"]
        ref = schema.get("$ref", "")
        assert ref.endswith("/McpLabError"), (code, schema)


def test_proposal_empty_evidence_returns_mcp_lab_error(env):
    """空 evidence 须返回 McpLabError.invalid_params,而非裸 FastAPI 422。"""
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    r = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "无证据",
        "evidence": [],
    })
    assert r.status_code == 422, r.text
    err = McpLabError.model_validate(r.json())
    assert err.reason_code == "invalid_params"
    assert err.tool == "propose_action"


def test_resolve_vue_dist_under_portable_home(tmp_path, monkeypatch):
    """便携布局 home/app/console-ui/dist 应可被 resolve_vue_dist 发现。"""
    from data2agent.console.app import resolve_vue_dist

    home = tmp_path / "portable"
    dist = home / "app" / "console-ui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.delenv("D2A_VUE_DIST", raising=False)
    monkeypatch.setenv("D2A_HOME", str(home))
    # 避免仓库内真实 dist 抢先命中
    monkeypatch.setattr(
        "data2agent.console.app._REPO_ROOT", tmp_path / "not-a-repo")
    assert resolve_vue_dist() == dist.resolve()


# ---- M6-T03 / T04: 查询 API 与 proposal gateway ----


def test_mcp_call_meta_includes_duration_and_process_scope(env):
    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}})
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["query_id"] and str(meta["query_id"]).startswith("q")
    assert meta["tool"] == "query_objects"
    assert meta["target"] == "Customer"
    assert meta["evidence_scope"] == "process"
    assert isinstance(meta["duration_ms"], int) and meta["duration_ms"] >= 0
    assert "contact" in meta["masked_fields"]
    assert any("draft" in w for w in meta["warnings"])


def test_mcp_call_unknown_target_returns_mcp_lab_error(env):
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Nope"}})
    assert r.status_code == 404
    err = McpLabError.model_validate(r.json())
    assert err.reason_code == "unknown_target"
    assert err.tool == "query_objects"


def test_mcp_call_not_published_returns_mcp_lab_error(tmp_path):
    from data2agent.console.contracts import McpLabError

    landing = LandingStore(tmp_path / "empty.sqlite")
    app = create_app(str(landing.db_path), ROOT / "templates")
    client = TestClient(app)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}})
    assert r.status_code == 409
    err = McpLabError.model_validate(r.json())
    assert err.reason_code == "not_published"


def test_proposal_gateway_success_and_expired_query(env):
    from data2agent.console.contracts import McpLabError, ProposalResponse

    client, _app, _cfg = _client(env)
    q = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}})
    assert q.status_code == 200
    qid = q.json()["meta"]["query_id"]

    ok = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "谨慎接",
        "evidence": [{"claim": "报价可见", "query_id": qid}],
    })
    assert ok.status_code == 200, ok.text
    card = ProposalResponse.model_validate(ok.json())
    assert card.tier == "说"
    assert "未执行" in card.governance
    assert card.evidence[0].query.query_id == qid

    expired = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "谨慎接",
        "evidence": [{"claim": "编造", "query_id": "q99999"}],
    })
    assert expired.status_code == 409
    err = McpLabError.model_validate(expired.json())
    assert err.reason_code == "query_expired"


def test_proposal_gateway_no_side_effects(env):
    from data2agent.connect.dataset_publish import resolve_published_snapshot

    client, _app, cfg = _client(env)
    landing = LandingStore(cfg.landing)
    snap = resolve_published_snapshot(landing, SOURCE)
    cust_table = snap.objects["Customer"].physical_table

    def counts():
        import sqlite3
        con = sqlite3.connect(cfg.landing)
        try:
            q = con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine").fetchone()[0]
            runs = con.execute(
                "SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0]
            cust = con.execute(
                f'SELECT COUNT(*) FROM "{cust_table}"').fetchone()[0]
            return q, runs, cust
        finally:
            con.close()

    before = counts()
    q = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}})
    qid = q.json()["meta"]["query_id"]
    r = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "不写库",
        "evidence": [{"claim": "x", "query_id": qid}],
    })
    assert r.status_code == 200
    assert counts() == before
