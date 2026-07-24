"""M6 MCP Lab 契约与边界测试。

M6-T01:先冻结 OpenAPI/模型语义;运行时长生命周期服务与 gateway 实现属后续任务。
"""

from __future__ import annotations

from datetime import date
import mimetypes
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.config import load_config
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore
from tests.helpers import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import (
    McpCallBody,
    McpLabError,
    McpQueryMeta,
    ProposalRequest,
    ProposalResponse,
)
from data2agent.mcp_server.evidence import EvidenceContext
from data2agent.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
SESSION_ID = "d2a_session_0123456789"


def _session_headers(session_id: str = SESSION_ID) -> dict[str, str]:
    return {"X-D2A-Session-ID": session_id}


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
        f"    path: {src}\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n",
        encoding="utf-8")
    return landing, cfg_file


def _openapi(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    app = create_app(cfg.landing, cfg.templates, cfg)
    return app.openapi()


def test_mcp_query_meta_schema_frozen(env):
    """McpQueryMeta 进入 OpenAPI,并冻结 principal_session 级 evidence_scope。"""
    schemas = _openapi(env)["components"]["schemas"]
    assert "McpQueryMeta" in schemas
    meta = schemas["McpQueryMeta"]
    props = meta["properties"]
    for key in (
        "query_id", "tool", "target", "row_count", "duration_ms",
        "masked_fields", "warnings", "evidence_scope", "session_id",
        "result_digest", "result_summary", "created_at", "expires_at",
        "dataset_version", "template_version", "binding_hashes",
    ):
        assert key in props, f"McpQueryMeta missing {key}"
    scope = props["evidence_scope"]
    assert (
        scope.get("const") == "principal_session"
        or scope.get("enum") == ["principal_session"]
    )
    sample = McpQueryMeta(
        query_id="q1", tool="query_objects", target="Customer",
        row_count=1, duration_ms=12, masked_fields=["phone"], warnings=[],
        session_id=SESSION_ID,
    )
    assert sample.evidence_scope == "principal_session"


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
        "query_expired", "invalid_session", "evidence_not_found",
        "evidence_principal_mismatch", "evidence_session_mismatch",
        "evidence_source_mismatch", "dataset_version_mismatch",
        "result_digest_mismatch", "evidence_integrity_failed",
        "tier_forbidden", "rate_limited", "mcp_unavailable",
        "evidence_store_unavailable", "execution_failed",
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
    """建议卡契约升级为 query_id + result_digest；detail 读取路径进入 OpenAPI。"""
    schemas = _openapi(env)["components"]["schemas"]
    assert "ProposalRequest" in schemas
    assert "ProposalResponse" in schemas
    assert "ProposalEvidence" in schemas
    resp_props = schemas["ProposalResponse"]["properties"]
    assert "governance" in resp_props
    assert "evidence" in resp_props
    assert "tier" in resp_props
    assert "session_id" in resp_props
    assert "source" in resp_props
    assert "dataset_version" in resp_props
    op = _openapi(env)["paths"]["/api/gateway/proposals"]["post"]
    assert "501" not in op["responses"]
    detail_paths = _openapi(env)["paths"]
    assert "/api/gateway/queries/{query_id}" in detail_paths
    assert "/api/gateway/proposals/{proposal_id}" in detail_paths

    with pytest.raises(ValidationError):
        ProposalRequest(
            object="SalesOrder", action="review", conclusion="c", evidence=[])
    with pytest.raises(ValidationError):
        ProposalRequest(
            object="SalesOrder",
            action="review",
            conclusion="c",
            evidence=[{"claim": "x", "query_id": "qry_x"}],
        )


def test_mcp_call_whitelist_rejects_propose_action(env):
    """Vue debug 入口白名单不扩大到 propose_action。"""
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
        "proposal_id": "prp_1",
        "at": "2026-07-21T00:00:00+00:00",
        "session_id": SESSION_ID,
        "source": SOURCE,
        "dataset_version": "ds_20260721",
        "object": "SalesOrder",
        "action": "review",
        "action_desc": "复核",
        "tier": "说",
        "conclusion": "需人工复核",
        "evidence": [{
            "claim": "金额偏高",
            "query": {
                "query_id": "qry_1",
                "source": SOURCE,
                "tool": "query_objects",
                "target": "SalesOrder",
                "normalized_query": {"tool": "query_objects", "object": "SalesOrder"},
                "dataset_version": "ds_20260721",
                "template_version": "0.1.0",
                "binding_hashes": {"SalesOrder": "sha256:abc"},
                "result_digest": "sha256:abc",
                "result_summary": {"kind": "query_objects", "returned_row_count": 1},
                "warnings": [],
                "created_at": "2026-07-21T00:00:00+00:00",
                "expires_at": "2026-07-22T00:00:00+00:00",
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
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    r2 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    q1 = r1.json()["meta"]["query_id"]
    q2 = r2.json()["meta"]["query_id"]
    assert q1 and q2 and q1 != q2
    assert q1.startswith("qry_") and q2.startswith("qry_")


def test_console_same_session_survives_query_service_recreation(env, tmp_path):
    import shutil

    client, app, _cfg = _client(env)
    r1 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert r1.status_code == 200, r1.text
    meta1 = r1.json()["meta"]

    new_templates = tmp_path / "templates-recreated"
    shutil.copytree(ROOT / "templates", new_templates)
    app.state.d2a_state["templates"] = str(new_templates)

    r2 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert r2.status_code == 200, r2.text
    meta2 = r2.json()["meta"]
    assert meta1["session_id"] == SESSION_ID
    assert meta2["session_id"] == SESSION_ID
    assert meta1["query_id"] != meta2["query_id"]


def test_console_different_sessions_produce_isolated_evidence(env):
    client, _app, _cfg = _client(env)
    r1 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers("d2a_session_alpha_0123456789"))
    r2 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers("d2a_session_beta_0123456789"))
    assert r1.status_code == 200 and r2.status_code == 200
    meta1 = r1.json()["meta"]
    meta2 = r2.json()["meta"]
    assert meta1["session_id"] == "d2a_session_alpha_0123456789"
    assert meta2["session_id"] == "d2a_session_beta_0123456789"
    assert meta1["query_id"] != meta2["query_id"]
    assert meta1["result_digest"] != ""
    assert meta2["result_digest"] != ""


def test_shared_query_service_allows_propose_after_mcp_call(env):
    """mcp-call 写入的 query_id 可被同实例 propose_action 引用。"""
    client, app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}},
        headers=_session_headers())
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    svc = app.state.d2a_state["query_service"]
    assert svc is not None
    card = svc.propose_action(
        "Quotation", "quote_review", "需复核报价",
        [{
            "claim": "报价行可见",
            "query_id": meta["query_id"],
            "result_digest": meta["result_digest"],
        }],
        context=EvidenceContext(
            principal="console:configured",
            session_id=SESSION_ID,
            channel="console",
        ),
    )
    assert card["proposal_id"]
    assert card["evidence"][0]["query"]["query_id"] == meta["query_id"]


def test_query_service_recreation_keeps_persisted_evidence_usable(env, tmp_path):
    """签名变化重建服务后,同会话的持久 query evidence 仍可被 proposal 引用。"""
    import shutil

    client, app, _cfg = _client(env)
    r1 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    r1b = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert r1.status_code == 200 and r1b.status_code == 200
    old_ids = {r1.json()["meta"]["query_id"], r1b.json()["meta"]["query_id"]}
    assert len(old_ids) == 2
    old_svc = app.state.d2a_state["query_service"]
    stale_meta = r1b.json()["meta"]

    # 换一套等价 templates 路径 → 配置签名变化 → 原子替换服务并清空日志
    new_templates = tmp_path / "templates-reloaded"
    shutil.copytree(ROOT / "templates", new_templates)
    app.state.d2a_state["templates"] = str(new_templates)

    r2 = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert r2.status_code == 200, r2.text
    new_qid = r2.json()["meta"]["query_id"]
    assert new_qid not in old_ids
    new_svc = app.state.d2a_state["query_service"]
    assert new_svc is not old_svc
    reused = new_svc.propose_action(
        "Quotation",
        "quote_review",
        "重建服务后仍可复用持久 evidence",
        [{
            "claim": "旧查询仍有效",
            "query_id": stale_meta["query_id"],
            "result_digest": stale_meta["result_digest"],
        }],
        context=EvidenceContext(
            principal="console:configured",
            session_id=SESSION_ID,
            channel="console",
        ),
    )
    assert reused["proposal_id"]
    assert reused["evidence"][0]["query"]["query_id"] == stale_meta["query_id"]

    ok = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "旧 evidence 可被持久复用",
        "evidence": [{
            "claim": "x",
            "query_id": stale_meta["query_id"],
            "result_digest": stale_meta["result_digest"],
        }],
    }, headers=_session_headers())
    assert ok.status_code == 200, ok.text
    assert ok.json()["evidence"][0]["query"]["query_id"] == stale_meta["query_id"]


def test_mcp_call_invalid_filters_shape_returns_invalid_params(env):
    """filters 非对象不得伪装为 mcp_unavailable。"""
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects",
        "params": {"object": "Customer", "filters": [1]},
    }, headers=_session_headers())
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
    }, headers=_session_headers())
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
    }, headers=_session_headers())
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
    }, headers=_session_headers())
    assert call.status_code == 409, call.text
    err = McpLabError.model_validate(call.json())
    assert err.reason_code == "mcp_unavailable"
    assert err.tool == "query_objects"

    prop = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "x",
        "evidence": [{"claim": "c", "query_id": "qry_1", "result_digest": "sha256:x"}],
    }, headers=_session_headers())
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


def test_gateway_routes_require_valid_session_header(env):
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    bad_call = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers("short"))
    assert bad_call.status_code == 422
    err = McpLabError.model_validate(bad_call.json())
    assert err.reason_code == "invalid_session"

    bad_prop = client.post("/api/gateway/proposals", json={
        "object": "Quotation",
        "action": "quote_review",
        "conclusion": "x",
        "evidence": [{"claim": "c", "query_id": "qry_1", "result_digest": "sha256:x"}],
    })
    assert bad_prop.status_code == 422
    err2 = McpLabError.model_validate(bad_prop.json())
    assert err2.reason_code == "invalid_session"

    bad_query_detail = client.get("/api/gateway/queries/qry_1")
    assert bad_query_detail.status_code == 422
    err3 = McpLabError.model_validate(bad_query_detail.json())
    assert err3.reason_code == "invalid_session"


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


def test_vue_module_assets_do_not_depend_on_system_mime_mapping(tmp_path, monkeypatch):
    """便携版即使 Windows 将 .js 映射为 text/plain，也必须返回模块 MIME。"""
    dist = tmp_path / "vue-dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (assets / "index.js").write_text("export {};", encoding="utf-8")
    monkeypatch.setenv("D2A_VUE_DIST", str(dist))
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")

    client = TestClient(create_app(str(tmp_path / "landing.sqlite"), ROOT / "templates"))
    response = client.get("/assets/index.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")


# ---- M6-T03 / T04: 查询 API 与 proposal gateway ----


def test_mcp_call_meta_includes_duration_and_persisted_evidence(env):
    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert r.status_code == 200, r.text
    meta = r.json()["meta"]
    assert meta["query_id"] and str(meta["query_id"]).startswith("qry_")
    assert meta["tool"] == "query_objects"
    assert meta["target"] == "Customer"
    assert meta["evidence_scope"] == "principal_session"
    assert meta["session_id"] == SESSION_ID
    assert meta["result_digest"].startswith("sha256:")
    assert meta["result_summary"]["kind"] == "query_objects"
    assert meta["created_at"]
    assert meta["expires_at"]
    assert isinstance(meta["duration_ms"], int) and meta["duration_ms"] >= 0
    assert "contact" in meta["masked_fields"]
    assert any("draft" in w for w in meta["warnings"])


def test_mcp_call_unknown_target_returns_mcp_lab_error(env):
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    r = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Nope"}},
        headers=_session_headers())
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
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert r.status_code == 409
    err = McpLabError.model_validate(r.json())
    assert err.reason_code == "not_published"


def test_proposal_gateway_validates_digest_and_returns_proposal(env):
    from data2agent.console.contracts import McpLabError, ProposalResponse

    client, _app, _cfg = _client(env)
    q = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}},
        headers=_session_headers())
    assert q.status_code == 200
    meta = q.json()["meta"]

    bad = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "谨慎接",
        "evidence": [{
            "claim": "报价可见",
            "query_id": meta["query_id"],
            "result_digest": "sha256:test",
        }],
    }, headers=_session_headers())
    assert bad.status_code == 422, bad.text
    err = McpLabError.model_validate(bad.json())
    assert err.reason_code == "invalid_params"
    with pytest.raises(ValidationError):
        ProposalResponse.model_validate(bad.json())

    ok = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "谨慎接",
        "evidence": [{
            "claim": "报价可见",
            "query_id": meta["query_id"],
            "result_digest": meta["result_digest"],
        }],
    }, headers=_session_headers())
    assert ok.status_code == 200, ok.text
    card = ProposalResponse.model_validate(ok.json())
    assert card.proposal_id.startswith("prp_")
    assert card.evidence[0].query.query_id == meta["query_id"]


def test_gateway_detail_routes_fail_closed_before_evidence_store(env):
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    query_resp = client.get("/api/gateway/queries/qry_test", headers=_session_headers())
    assert query_resp.status_code == 422
    qerr = McpLabError.model_validate(query_resp.json())
    assert qerr.reason_code == "invalid_params"

    proposal_resp = client.get("/api/gateway/proposals/prp_test", headers=_session_headers())
    assert proposal_resp.status_code == 422
    perr = McpLabError.model_validate(proposal_resp.json())
    assert perr.reason_code == "invalid_params"


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
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}},
        headers=_session_headers())
    meta = q.json()["meta"]
    r = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "不写库",
        "evidence": [{
            "claim": "x",
            "query_id": meta["query_id"],
            "result_digest": meta["result_digest"],
        }],
    }, headers=_session_headers())
    assert r.status_code == 200
    assert counts() == before


def test_query_detail_returns_persisted_evidence(env):
    client, _app, _cfg = _client(env)
    q = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers())
    assert q.status_code == 200, q.text
    meta = q.json()["meta"]

    detail = client.get(
        f"/api/gateway/queries/{meta['query_id']}",
        headers=_session_headers(),
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["query_id"] == meta["query_id"]
    assert body["session_id"] == SESSION_ID
    assert body["result_digest"] == meta["result_digest"]
    assert body["result_summary"] == meta["result_summary"]
    assert body["evidence_scope"] == "principal_session"


def test_query_detail_rejects_cross_session(env):
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    q = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Customer", "limit": 1}},
        headers=_session_headers("d2a_session_alpha_0123456789"))
    meta = q.json()["meta"]

    denied = client.get(
        f"/api/gateway/queries/{meta['query_id']}",
        headers=_session_headers("d2a_session_beta_0123456789"),
    )
    assert denied.status_code == 409
    err = McpLabError.model_validate(denied.json())
    assert err.reason_code == "evidence_session_mismatch"


def test_query_detail_missing_returns_not_found(env):
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    missing = client.get(
        "/api/gateway/queries/qry_000000000000000000000000",
        headers=_session_headers(),
    )
    assert missing.status_code == 404
    err = McpLabError.model_validate(missing.json())
    assert err.reason_code == "evidence_not_found"


def test_proposal_detail_returns_frozen_snapshot(env):
    client, _app, _cfg = _client(env)
    q = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}},
        headers=_session_headers())
    meta = q.json()["meta"]
    created = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "谨慎接",
        "evidence": [{
            "claim": "报价可见",
            "query_id": meta["query_id"],
            "result_digest": meta["result_digest"],
        }],
    }, headers=_session_headers())
    assert created.status_code == 200, created.text
    proposal_id = created.json()["proposal_id"]

    detail = client.get(
        f"/api/gateway/proposals/{proposal_id}",
        headers=_session_headers(),
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["proposal_id"] == proposal_id
    assert body["session_id"] == SESSION_ID
    assert body["evidence"][0]["query"]["query_id"] == meta["query_id"]
    assert body["evidence"][0]["query"]["result_digest"] == meta["result_digest"]
    assert body["evidence"][0]["query"]["expires_at"] is None


def test_proposal_detail_rejects_cross_session(env):
    from data2agent.console.contracts import McpLabError

    client, _app, _cfg = _client(env)
    q = client.post("/api/debug/mcp-call", json={
        "tool": "query_objects", "params": {"object": "Quotation", "limit": 1}},
        headers=_session_headers("d2a_session_alpha_0123456789"))
    meta = q.json()["meta"]
    created = client.post("/api/gateway/proposals", json={
        "object": "Quotation", "action": "quote_review",
        "conclusion": "谨慎接",
        "evidence": [{
            "claim": "报价可见",
            "query_id": meta["query_id"],
            "result_digest": meta["result_digest"],
        }],
    }, headers=_session_headers("d2a_session_alpha_0123456789"))
    proposal_id = created.json()["proposal_id"]

    denied = client.get(
        f"/api/gateway/proposals/{proposal_id}",
        headers=_session_headers("d2a_session_beta_0123456789"),
    )
    assert denied.status_code == 409
    err = McpLabError.model_validate(denied.json())
    assert err.reason_code == "evidence_session_mismatch"
