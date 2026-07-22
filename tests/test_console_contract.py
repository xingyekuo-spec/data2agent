"""Console API contract tests: routes, wire shape, named schemas, auth, snapshot."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.admin_common.home_layout import HomeLayout  # noqa: E402
from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.config import load_config  # noqa: E402
from data2agent.connect.dataset_publish import build_dataset  # noqa: E402
from data2agent.connect.increment import incremental_sync, watermarks_from_pack  # noqa: E402
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.connect.sync import whitelist_from_pack  # noqa: E402
from data2agent.console.app import create_app  # noqa: E402
from data2agent.console.contracts import (  # noqa: E402
    ActionExecutionResult,
    ApplyActionResult,
    AuditRecord,
    ConfigViewResponse,
    HttpError,
    OverviewResponse,
    QuarantineRecord,
    RequestError,
    RunSummary,
    ServicesStatusResponse,
    SetupFailureResponse,
    SetupStatusResponse,
    SetupSuccessResponse,
)
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.showroom.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"

REQUIRED_API_ROUTES = {
    ("GET", "/api/setup/status"),
    ("POST", "/api/setup"),
    ("GET", "/api/overview"),
    ("GET", "/api/runs"),
    ("GET", "/api/quarantine"),
    ("GET", "/api/audit"),
    ("GET", "/api/config"),
    ("POST", "/api/config"),
    ("POST", "/api/config/validate"),
    ("GET", "/api/services"),
    ("GET", "/api/logs"),
    ("GET", "/api/debug/raw-table"),
    ("POST", "/api/debug/mcp-call"),
    ("POST", "/api/actions/sync"),
    ("POST", "/api/actions/reconcile"),
    ("POST", "/api/actions/apply"),
    ("POST", "/api/actions/retry"),
    ("GET", "/api/datasets"),
    ("GET", "/api/datasets/{version}"),
    ("POST", "/api/datasets/{version}/publish"),
    ("POST", "/api/datasets/{version}/rollback"),
    ("POST", "/api/mappings/{object}/preview"),
    ("GET", "/api/objects/{object}/{key}/lineage"),
}

NAMED_SUCCESS_SCHEMAS = {
    ("get", "/api/setup/status"): "SetupStatusResponse",
    ("get", "/api/overview"): "OverviewResponse",
    ("get", "/api/runs"): ("array", "RunSummary"),
    ("get", "/api/quarantine"): ("array", "QuarantineRecord"),
    ("get", "/api/audit"): ("array", "AuditRecord"),
    ("get", "/api/config"): "ConfigViewResponse",
    ("post", "/api/config"): "ConfigSaveResponse",
    ("post", "/api/config/validate"): "ValidationResult",
    ("get", "/api/services"): "ServicesStatusResponse",
    ("get", "/api/logs"): "LogsResponse",
    ("get", "/api/debug/raw-table"): "RawTablePageResponse",
    ("post", "/api/debug/mcp-call"): "McpToolResult",
    ("post", "/api/actions/sync"): "ActionExecutionResult",
    ("post", "/api/actions/reconcile"): "ActionExecutionResult",
    ("post", "/api/actions/apply"): "ApplyActionResult",
    ("post", "/api/actions/retry"): "RetryActionResult",
    # 普通 anyOf(无 discriminator):保证 TS 生成 ok 的 boolean 字面量,可收窄
    ("post", "/api/setup"): ("anyOf", ("SetupSuccessResponse", "SetupFailureResponse")),
    ("get", "/api/datasets"): ("array", "DatasetSummary"),
    ("get", "/api/datasets/{version}"): "DatasetDetail",
    ("post", "/api/datasets/{version}/publish"): "DatasetActionResult",
    ("post", "/api/datasets/{version}/rollback"): "DatasetActionResult",
    ("post", "/api/mappings/{object}/preview"): "MappingPreviewResponse",
    ("get", "/api/objects/{object}/{key}/lineage"): "ObjectLineageResponse",
}

# v0.3 M2-T06: publish/rollback OpenAPI 冻结 200/404/409/500;运行时映射真实引擎结果。
DATASET_ACTION_ROUTES: set[tuple[str, str]] = {
    ("POST", "/api/datasets/{version}/publish"),
    ("POST", "/api/datasets/{version}/rollback"),
}

DATASET_ACTION_SUCCESS_SCHEMAS: dict[tuple[str, str], str] = {
    ("post", "/api/datasets/{version}/publish"): "DatasetActionResult",
    ("post", "/api/datasets/{version}/rollback"): "DatasetActionResult",
}

# 缺失版本应 404(不再是 501 契约桩)
DATASET_ACTION_RUNTIME_MISSING: list[tuple[str, str, dict]] = [
    ("post", "/api/datasets/ds-demo/publish", {}),
    ("post", "/api/datasets/ds-demo/rollback", {}),
]

# 兼容旧测试名
STUB_API_ROUTES = DATASET_ACTION_ROUTES
STUB_SUCCESS_SCHEMAS = DATASET_ACTION_SUCCESS_SCHEMAS
STUB_RUNTIME_CALLS = DATASET_ACTION_RUNTIME_MISSING


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


def _openapi_app(tmp_path):
    landing = tmp_path / "empty-landing.sqlite"
    return create_app(landing=str(landing), templates=str(ROOT / "templates"))


def _schema_ref(node: dict) -> str | None:
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    return None


def _assert_schema_not_unknown(schemas: dict, name: str) -> None:
    """Reject empty `{}` / items:{} shapes that become TypeScript `unknown`."""
    assert name in schemas, name
    node = schemas[name]
    assert node != {}, f"{name} must not be an empty schema"
    if "anyOf" in node:
        assert node["anyOf"], f"{name}.anyOf empty"
        return
    if node.get("type") == "object":
        props = node.get("properties") or {}
        addl = node.get("additionalProperties")
        assert props or addl not in (None, True, {}), (
            f"{name} object must declare properties or typed additionalProperties, got {node}"
        )
        if isinstance(addl, dict) and "$ref" in addl:
            _assert_schema_not_unknown(schemas, _schema_ref(addl))
        return
    assert "type" in node or "$ref" in node, f"{name} untyped: {node}"


def test_required_api_routes_present(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    found = set()
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                found.add((method.upper(), path))
    missing = REQUIRED_API_ROUTES - found
    assert not missing, f"missing API routes: {sorted(missing)}"
    assert len(REQUIRED_API_ROUTES) == 23


def test_success_responses_use_named_schemas(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    for (method, path), expected in NAMED_SUCCESS_SCHEMAS.items():
        content = (
            spec["paths"][path][method]["responses"]["200"]
            ["content"]["application/json"]["schema"]
        )
        if isinstance(expected, tuple) and expected[0] == "array":
            assert content.get("type") == "array", path
            name = _schema_ref(content.get("items", {}))
            assert name == expected[1], f"{path}: expected list[{expected[1]}], got {content}"
            _assert_schema_not_unknown(schemas, name)
        elif isinstance(expected, tuple) and expected[0] == "anyOf":
            refs = {_schema_ref(x) for x in content.get("anyOf", [])}
            assert refs == set(expected[1]), f"{path}: expected anyOf {expected[1]}, got {content}"
            # 成员必须带 ok 的 const 字面量,TS 才能收窄;不得退回 discriminator
            # (openapi-typescript 会把 ok 重写为字符串枚举,破坏收窄)
            assert "discriminator" not in content
            for name in expected[1]:
                ok_schema = schemas[name]["properties"]["ok"]
                assert ok_schema.get("const") in (True, False), f"{name}.ok needs const"
                _assert_schema_not_unknown(schemas, name)
        else:
            name = _schema_ref(content)
            assert name == expected, f"{path}: expected {expected}, got {content}"
            _assert_schema_not_unknown(schemas, name)


def test_json_value_schemas_are_recursive_anyof(tmp_path):
    schemas = _openapi_app(tmp_path).openapi()["components"]["schemas"]
    for name in ("JsonValue-Input", "JsonValue-Output"):
        node = schemas[name]
        assert "anyOf" in node, name
        kinds = {item.get("type") for item in node["anyOf"] if isinstance(item, dict)}
        assert {"string", "integer", "number", "boolean", "array", "object", "null"} <= kinds
        assert node.get("additionalProperties") is not True
        for item in node["anyOf"]:
            if item.get("type") == "array":
                assert item.get("items") not in ({}, None)
                assert "$ref" in item["items"]
            if item.get("type") == "object":
                assert item.get("additionalProperties") not in (True, {}, None)


def test_openapi_declares_optional_bearer_security(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    schemes = spec["components"]["securitySchemes"]
    assert "HTTPBearer" in schemes
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    optional = [{"HTTPBearer": []}, {}]
    overview = spec["paths"]["/api/overview"]["get"]
    assert overview.get("security") == optional
    assert spec["paths"]["/api/data/raw"]["get"].get("security") == [{"HTTPBearer": []}]
    assert spec["paths"]["/api/data/raw/{source}/{table}"]["get"].get("security") == [
        {"HTTPBearer": []}]
    assert spec["paths"]["/api/mappings/{object}/preview"]["post"].get("security") == [
        {"HTTPBearer": []}]
    # Setup routes use the same optional Bearer: after first-time setup + token,
    # runtime returns 401 without credentials (not permanently unauthenticated).
    assert spec["paths"]["/api/setup"]["post"].get("security") == optional
    assert spec["paths"]["/api/setup/status"]["get"].get("security") == optional
    assert "needs_setup" in (spec["paths"]["/api/setup"]["post"].get("description") or "")

def test_wire_shape_arrays_and_objects(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    overview = client.get("/api/overview").json()
    OverviewResponse.model_validate(overview)
    assert isinstance(overview, dict)
    assert "sources" in overview and "objects" in overview

    runs = client.get("/api/runs").json()
    assert isinstance(runs, list)
    assert not isinstance(runs, dict)
    RunSummary.model_validate(runs[0])

    audit = client.get("/api/audit").json()
    assert isinstance(audit, list)
    AuditRecord.model_validate(audit[0])

    quarantine = client.get("/api/quarantine").json()
    assert isinstance(quarantine, list)
    for row in quarantine:
        QuarantineRecord.model_validate(row)

    cfg = client.get("/api/config")
    # readonly app without config_path → 409
    assert cfg.status_code == 409
    HttpError.model_validate(cfg.json())


def test_config_and_services_models(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    client = TestClient(create_app(
        cfg.landing, cfg.templates, cfg, token="t",
        config_path=cfg_file, log_dir=Path(".")))
    h = {"Authorization": "Bearer t"}
    view = client.get("/api/config", headers=h).json()
    ConfigViewResponse.model_validate(view)
    services = client.get("/api/services", headers=h).json()
    ServicesStatusResponse.model_validate(services)
    assert services["console"]["ok"] is True


def test_token_missing_returns_http_error_not_empty_data(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates", token="s3cret"))
    r = client.get("/api/overview")
    assert r.status_code == 401
    err = HttpError.model_validate(r.json())
    assert err.detail
    assert "sources" not in r.json()


def test_setup_mode_blocks_management_apis(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    shutil.copytree(ROOT / "templates", home.app / "templates")
    client = TestClient(create_app(home=home.root))
    st = client.get("/api/setup/status")
    assert st.status_code == 200
    SetupStatusResponse.model_validate(st.json())
    assert st.json()["needs_setup"] is True

    blocked = client.get("/api/overview")
    assert blocked.status_code == 409
    HttpError.model_validate(blocked.json())
    assert client.get("/api/runs").status_code == 409
    assert client.get("/api/audit").status_code == 409


def test_limit_is_capped(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    # M4 口径:越界参数返回 422(不是静默截断)
    assert client.get("/api/runs", params={"limit": 9999}).status_code == 422
    assert client.get("/api/runs", params={"limit": 0}).status_code == 422
    runs = client.get("/api/runs", params={"limit": 100}).json()
    assert isinstance(runs, list) and len(runs) <= 100
    # audit 同样按 M4 口径:越界 422
    assert client.get("/api/audit", params={"limit": 9999}).status_code == 422
    audit = client.get("/api/audit", params={"limit": 100}).json()
    assert len(audit) <= 100


def test_mcp_unknown_tool_rejected(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    client = TestClient(create_app(cfg.landing, cfg.templates, cfg))
    r = client.post("/api/debug/mcp-call", json={"tool": "propose_action", "params": {}})
    assert r.status_code == 422
    RequestError.model_validate(r.json())
    body = _openapi_app(Path(cfg.landing).parent).openapi()
    tool_schema = body["components"]["schemas"]["McpCallBody"]["properties"]["tool"]
    assert tool_schema.get("enum") == ["query_objects", "query_metrics"]


def test_retry_422_string_and_validation_list(env):
    landing, cfg_file = env
    client = TestClient(create_app("ignored", "ignored", load_config(cfg_file)))
    missing = client.post("/api/actions/retry", json={"source": SOURCE})
    assert missing.status_code == 422
    missing_body = RequestError.model_validate(missing.json())
    assert isinstance(missing_body.detail, str)

    invalid = client.post("/api/actions/retry", json={"source": SOURCE, "deep": "not-bool"})
    assert invalid.status_code == 422
    invalid_body = RequestError.model_validate(invalid.json())
    assert isinstance(invalid_body.detail, list) and invalid_body.detail

    spec = _openapi_app(Path(cfg_file).parent).openapi()
    retry_422 = spec["paths"]["/api/actions/retry"]["post"]["responses"]["422"]
    assert _schema_ref(retry_422["content"]["application/json"]["schema"]) == "RequestError"


def test_setup_response_union_narrowable(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    shutil.copytree(ROOT / "templates", home.app / "templates")
    client = TestClient(create_app(home=home.root))
    fail = client.post("/api/setup", json={"ingest_token": " ", "console_token": " "})
    assert fail.status_code == 200
    SetupFailureResponse.model_validate(fail.json())
    assert "message" not in fail.json()

    ok = client.post("/api/setup", json={
        "ingest_token": "ingest-tok",
        "console_token": "console-tok",
    })
    assert ok.status_code == 200
    SetupSuccessResponse.model_validate(ok.json())
    assert "errors" not in ok.json()

    # After setup + token, setup endpoints require Bearer (matches OpenAPI optional auth).
    assert client.get("/api/setup/status").status_code == 401
    assert client.post("/api/setup", json={
        "ingest_token": "x", "console_token": "y",
    }).status_code == 401
    authed = client.get(
        "/api/setup/status", headers={"Authorization": "Bearer console-tok"})
    assert authed.status_code == 200
    assert authed.json()["needs_setup"] is False

    schemas = _openapi_app(tmp_path).openapi()["components"]["schemas"]
    for name in ("SetupSuccessResponse", "SetupFailureResponse"):
        assert "ok" in schemas[name].get("required", []), (
            f"{name}.ok must be required for TS discriminant narrowing"
        )
        assert "default" not in schemas[name]["properties"]["ok"]


def test_action_executed_false_is_success_body(env, tmp_path):
    from datetime import datetime, timedelta

    landing, cfg_file = env
    t2 = datetime.now() + timedelta(hours=2)
    t3 = datetime.now() + timedelta(hours=3)
    closed = tmp_path / "closed.yaml"
    closed.write_text(
        cfg_file.read_text(encoding="utf-8")
        + f'    windows: ["{t2:%H:%M}-{t3:%H:%M}"]\n',
        encoding="utf-8")
    client = TestClient(create_app("ignored", "ignored", load_config(closed)))
    r = client.post("/api/actions/sync", json={"source": SOURCE})
    assert r.status_code == 200
    body = ActionExecutionResult.model_validate(r.json())
    assert body.executed is False
    assert "窗口" in body.note


def test_apply_response_model(env):
    landing, cfg_file = env
    client = TestClient(create_app("ignored", "ignored", load_config(cfg_file)))
    r = client.post("/api/actions/apply", json={"source": SOURCE})
    assert r.status_code == 200
    ApplyActionResult.model_validate(r.json())


def test_errors_do_not_validate_as_success_models(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates", token="x"))
    err = client.get("/api/overview").json()
    with pytest.raises(Exception):
        OverviewResponse.model_validate(err)


def test_legacy_time_fields_remain_strings(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    run = client.get("/api/runs").json()[0]
    assert isinstance(run["started_at"], str)
    audit = client.get("/api/audit").json()[0]
    assert isinstance(audit["ts"], str)


def test_openapi_snapshot_roundtrip(tmp_path, monkeypatch):
    import tempfile as tempfile_mod

    script = ROOT / "scripts" / "export_console_openapi.py"
    out = tmp_path / "openapi.json"
    before = set(Path(tempfile_mod.gettempdir()).glob("d2a-openapi-*"))
    r1 = subprocess.run(
        [sys.executable, str(script), str(out)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert r1.returncode == 0, r1.stderr
    after = set(Path(tempfile_mod.gettempdir()).glob("d2a-openapi-*"))
    assert after == before, f"export left temp dirs behind: {after - before}"
    first = out.read_bytes()
    r2 = subprocess.run(
        [sys.executable, str(script), str(out)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert r2.returncode == 0, r2.stderr
    assert out.read_bytes() == first
    check = subprocess.run(
        [sys.executable, str(script), "--check", str(out)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr + check.stdout

    # Drift must fail check
    data = json.loads(first)
    data["info"]["title"] = "drifted"
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    drifted = subprocess.run(
        [sys.executable, str(script), "--check", str(out)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert drifted.returncode != 0
    assert "export_console_openapi.py" in (drifted.stdout + drifted.stderr)


# ---- M2 v0.2 契约桩 ----


# ---- v0.3 datasets 契约桩 ----


def test_apply_action_body_is_dedicated_in_openapi(tmp_path):
    """apply 使用 ApplyActionBody;publish 不得出现在共用 ActionBody。"""
    spec = _openapi_app(tmp_path).openapi()
    schemas = spec["components"]["schemas"]
    assert "ApplyActionBody" in schemas
    apply_props = schemas["ApplyActionBody"]["properties"]
    assert set(apply_props) == {"source", "publish"}
    assert apply_props["publish"]["default"] is True
    action_props = schemas["ActionBody"]["properties"]
    assert "publish" not in action_props
    apply_op = spec["paths"]["/api/actions/apply"]["post"]
    body_ref = apply_op["requestBody"]["content"]["application/json"]["schema"]
    assert body_ref.get("$ref", "").endswith("/ApplyActionBody")


def test_v03_dataset_action_openapi_declares_final_errors(tmp_path):
    """M2-T06: OpenAPI 冻结 200/404/409/500;缺失版本运行时 404。"""
    assert DATASET_ACTION_ROUTES == {
        ("POST", "/api/datasets/{version}/publish"),
        ("POST", "/api/datasets/{version}/rollback"),
    }
    assert DATASET_ACTION_SUCCESS_SCHEMAS == {
        ("post", "/api/datasets/{version}/publish"): "DatasetActionResult",
        ("post", "/api/datasets/{version}/rollback"): "DatasetActionResult",
    }
    spec = _openapi_app(tmp_path).openapi()
    for method, path in DATASET_ACTION_ROUTES:
        op = spec["paths"][path][method.lower()]
        assert "200" in op["responses"], path
        assert "404" in op["responses"], path
        assert "409" in op["responses"], path
        assert "500" in op["responses"], path
        assert "501" not in op["responses"], path
    client = TestClient(_openapi_app(tmp_path))
    for method, path, kwargs in DATASET_ACTION_RUNTIME_MISSING:
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 404, path


def test_proposals_no_longer_stubbed(tmp_path):
    """M6 起 proposals 不再声明 501。"""
    spec = _openapi_app(tmp_path).openapi()
    op = spec["paths"]["/api/gateway/proposals"]["post"]
    assert "501" not in op["responses"]
    assert "ProposalResponse" in (
        _schema_ref(op["responses"]["200"]["content"]["application/json"]["schema"]),
    )


def test_proposal_request_validation(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    # 空 evidence 应在进入业务逻辑前被 422 拒绝
    r = client.post("/api/gateway/proposals", json={
        "object": "SalesOrder", "action": "review", "conclusion": "c", "evidence": []})
    assert r.status_code == 422


# ---- M3 观测口径契约 ----


def test_overview_m3_blocks_present_and_tz_aware(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    body = OverviewResponse.model_validate(client.get("/api/overview").json())
    # 旧字段兼容
    assert body.sources and body.objects
    assert body.readonly is True
    # M3 块:带时区时间、版本、口径说明
    assert body.generated_at.tzinfo is not None
    assert body.versions.template
    assert body.versions.dataset is not None
    assert body.versions.object == body.versions.dataset
    assert isinstance(body.alerts, list)
    assert isinstance(body.sync_trend, list)
    assert {n.name for n in body.count_notes} >= {"raw_rows", "object_rows"}
    # 正常库:raw/object 行数为真实计数(不是 null)
    assert body.summary.raw_rows is not None and body.summary.raw_rows > 0
    assert body.summary.object_rows is not None and body.summary.object_rows > 0
    assert body.summary.template_objects >= body.summary.materialized_objects >= 1
    # 最近运行:T02 起写入真实 run_type,时间带时区
    assert body.recent_runs
    assert body.recent_runs[0].run_type in (
        "sync", "apply", "reconcile", "ingest", "publish", "rollback")
    assert body.recent_runs[0].started_at.tzinfo is not None


def test_overview_unknown_counts_are_null_not_zero(tmp_path):
    """不可检测/未物化的计数为 null(unknown),不是 0 也不是 healthy。"""
    landing = tmp_path / "empty.sqlite"
    client = TestClient(create_app(str(landing), str(ROOT / "templates")))
    body = OverviewResponse.model_validate(client.get("/api/overview").json())
    # 空库没有任何 raw 表:0 是事实
    assert body.summary.raw_rows == 0
    # 未物化:object_rows 为 null(不是 0),覆盖率为 0/N
    assert body.summary.object_rows is None
    assert body.summary.materialized_objects == 0
    assert body.summary.template_objects > 0
    assert body.versions.dataset is None and body.versions.object is None


def test_pipeline_contract_overall_and_node_fields(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    schemas = spec["components"]["schemas"]
    resp = schemas["PipelineResponse"]
    assert "overall_status" in resp["properties"]
    node_props = schemas["PipelineNode"]["properties"]
    for field in ("status_reason", "observed_at", "run_id", "source", "detail_path"):
        assert field in node_props, field
    expected = {"unknown", "idle", "running", "healthy", "warning", "failed", "stale"}
    assert set(node_props["status"]["enum"]) == expected
    assert set(resp["properties"]["overall_status"]["enum"]) == expected


# ---- M4 契约冻结 ----


def test_m4_run_summary_shape(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    schemas = spec["components"]["schemas"]
    props = schemas["RunSummary"]["properties"]
    assert set(props["type"]["anyOf"][0]["enum"]) == {
        "sync", "apply", "reconcile", "ingest", "validation", "publish", "rollback"}
    assert set(props["status"]["anyOf"][0]["enum"]) == {
        "running", "ok", "paused", "failed", "aborted"}
    for field in ("duration_ms", "quarantined", "dataset_version", "error", "error_id"):
        assert field in props, field
    detail = schemas["RunDetailResponse"]["properties"]
    assert set(detail["steps_state"]["enum"]) == {"available", "legacy_unavailable"}
    step_props = schemas["RunStep"]["properties"]
    assert set(step_props["kind"]["enum"]) == {
        "table", "object", "segment", "batch", "dataset"}
    for field in ("ordinal", "batch_id", "repaired", "soft_deleted",
                  "watermark_before", "watermark_after"):
        assert field in step_props, field
    run_params = {
        p["name"]: p["schema"]
        for p in spec["paths"]["/api/runs"]["get"]["parameters"]
    }
    assert set(run_params["type"]["anyOf"][0]["enum"]) == {
        "sync", "apply", "reconcile", "ingest", "validation", "publish", "rollback"}
    assert set(run_params["status"]["anyOf"][0]["enum"]) == {
        "running", "ok", "paused", "failed", "aborted"}


def test_m4_browse_and_audit_shapes(tmp_path):
    schemas = _openapi_app(tmp_path).openapi()["components"]["schemas"]
    raw_props = schemas["RawDataPageResponse"]["properties"]
    for field in ("columns", "truncations", "sort", "query", "searchable",
                  "warnings", "generated_at"):
        assert field in raw_props, field
    col_props = schemas["ColumnMeta"]["properties"]
    assert set(col_props["role"]["enum"]) == {"business_key", "data", "metadata"}
    assert set(col_props["classification"]["enum"]) == {"normal", "sensitive", "unknown"}
    assert set(schemas["AccessAuditItem"]["properties"]["resource_type"]["enum"]) == {
        "raw", "object", "quarantine_raw"}
    obj_props = schemas["ObjectSummary"]["properties"]
    assert "searchable" in obj_props and "warning" in obj_props
    audit_props = schemas["AuditRecord"]["properties"]
    assert "id" in audit_props


def test_runs_and_audit_declare_total_count_header(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    for path in ("/api/runs", "/api/audit", "/api/quarantine", "/api/datasets"):
        headers = spec["paths"][path]["get"]["responses"]["200"].get("headers", {})
        assert "X-Total-Count" in headers, path
        assert headers["X-Total-Count"]["schema"]["type"] == "integer"


def test_datasets_declare_500_for_corrupt_metadata(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    for path in ("/api/datasets", "/api/datasets/{version}"):
        responses = spec["paths"][path]["get"]["responses"]
        assert "500" in responses, path
    props = spec["components"]["schemas"]["DatasetSummary"]["properties"]
    assert "error_id" in props
    assert "error" in props
