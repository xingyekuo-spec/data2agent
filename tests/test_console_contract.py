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
from data2agent.connect.increment import incremental_sync, watermarks_from_pack  # noqa: E402
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.connect.mapping_apply import apply_objects  # noqa: E402
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
    RunSummary,
    ServicesStatusResponse,
    SetupStatusResponse,
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
    ("post", "/api/setup"): "SetupResponse",
}


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


def _openapi_app(tmp_path):
    landing = tmp_path / "empty-landing.sqlite"
    return create_app(landing=str(landing), templates=str(ROOT / "templates"))


def _schema_ref(node: dict) -> str | None:
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    return None


def test_required_api_routes_present(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    found = set()
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                found.add((method.upper(), path))
    missing = REQUIRED_API_ROUTES - found
    assert not missing, f"missing API routes: {sorted(missing)}"
    assert len(REQUIRED_API_ROUTES) == 17


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
            assert name in schemas
            assert schemas[name].get("type") == "object"
            assert schemas[name].get("properties"), f"{name} must not be empty object"
        else:
            name = _schema_ref(content)
            assert name == expected, f"{path}: expected {expected}, got {content}"
            assert name in schemas
            props = schemas[name].get("properties") or {}
            # RootModel may expose via additionalProperties / items; require non-empty schema body
            assert props or schemas[name].get("additionalProperties") is not None \
                or "anyOf" in schemas[name] or schemas[name].get("type") == "object", name


def test_openapi_declares_bearer_security(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    schemes = spec["components"]["securitySchemes"]
    assert "HTTPBearer" in schemes
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    overview = spec["paths"]["/api/overview"]["get"]
    assert {"HTTPBearer": []} in overview.get("security", [])


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
    runs = client.get("/api/runs", params={"limit": 9999}).json()
    assert isinstance(runs, list)
    assert len(runs) <= 100
    audit = client.get("/api/audit", params={"limit": 9999}).json()
    assert len(audit) <= 200


def test_mcp_unknown_tool_rejected(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    client = TestClient(create_app(cfg.landing, cfg.templates, cfg))
    r = client.post("/api/debug/mcp-call", json={"tool": "propose_action", "params": {}})
    assert r.status_code == 400
    HttpError.model_validate(r.json())


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


def test_openapi_snapshot_roundtrip(tmp_path):
    script = ROOT / "scripts" / "export_console_openapi.py"
    out = tmp_path / "openapi.json"
    r1 = subprocess.run(
        [sys.executable, str(script), str(out)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert r1.returncode == 0, r1.stderr
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
