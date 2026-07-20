"""控制台应用:FastAPI 单页 + JSON API + 运维动作。

安全:
- 只读视图直接查落地库;动作(sync / reconcile / apply / retry)复用 connect
  引擎,错峰窗口 / 白名单 / 只读适配器约束原样生效,控制台不开新的旁路;
- 可选 Bearer Token(--token 或环境变量 D2A_CONSOLE_TOKEN),内网部署建议启用;
- 未加载 --config 时为纯只读模式,动作接口返回 409 并说明原因。
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape

from ..admin_common.config_edit import PLATFORM_EDITABLE, merge_whitelist_and_save
from ..admin_common.home_layout import HomeLayout
from ..admin_common.logs import tail_lines
from ..admin_common.secrets_file import apply_secrets_to_environ, save_secrets
from ..admin_common.setup_yaml import build_platform_yaml, write_yaml
from ..connect.config import ConnectConfig, load_config
from ..connect.landing import LandingStore
from ..connect.mapping_apply import MappingCircuitBreaker, apply_object, apply_objects
from ..connect.scheduler import run_reconcile_cycle, run_sync_cycle
from ..metamodel.loader import load_pack
from . import data_browser as br
from . import observability as obs
from .contracts import (
    AccessAuditPage,
    ActionBody,
    ActionExecutionResult,
    ApplyActionResult,
    AuditRecord,
    ConfigPatch,
    ConfigSaveResponse,
    ConfigViewResponse,
    HttpError,
    LogsResponse,
    McpCallBody,
    McpToolResult,
    ObjectRowsPageResponse,
    ObjectSummary,
    OverviewResponse,
    PipelineResponse,
    ProposalRequest,
    ProposalResponse,
    QuarantineRecord,
    RawDataPageResponse,
    RawTableCatalogResponse,
    RawTablePageResponse,
    RequestError,
    RetryActionResult,
    RunDetailResponse,
    RunStatus,
    RunSummary,
    RunType,
    ServicesStatusResponse,
    SetupBody,
    SetupFailureResponse,
    SetupResponse,
    SetupStatusResponse,
    SetupSuccessResponse,
    TemplateObject,
    ValidationResult,
)
from .ui import UI_HTML

_RESP_HTTP_ERROR = {
    401: {"model": HttpError, "description": "缺少或无效的 Bearer Token"},
    403: {"model": HttpError, "description": "禁止访问"},
    409: {"model": HttpError, "description": "冲突/未配置/只读/熔断"},
    422: {
        "model": RequestError,
        "description": "请求参数错误(HTTPException 字符串 detail 或 FastAPI 校验列表)",
    },
    500: {"model": HttpError, "description": "未处理异常"},
}

_RESP_HTTP_ERROR_STUB = {
    501: {"model": HttpError, "description": "契约桩:端点在所属里程碑实现前返回 501"},
}

_SETUP_API_PATHS = frozenset({"/api/setup", "/api/setup/status"})
_AUDIT_SQL_BUDGET = 4096

_PKG = Path(__file__).resolve().parent
_ADMIN_TEMPLATES = _PKG.parent / "admin_templates"
_CONSOLE_TEMPLATES = _PKG / "templates"
_ADMIN_STATIC = _ADMIN_TEMPLATES / "static"
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _make_templates() -> Jinja2Templates:
    """双搜索路径: console/templates + admin_templates; admin/ 前缀继承基 layout。"""
    env = Environment(
        loader=ChoiceLoader([
            FileSystemLoader(str(_CONSOLE_TEMPLATES)),
            FileSystemLoader(str(_ADMIN_TEMPLATES)),
            PrefixLoader({"admin": FileSystemLoader(str(_ADMIN_TEMPLATES))}),
        ]),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return Jinja2Templates(env=env)


def _budget_text(value: str, budget: int = _AUDIT_SQL_BUDGET) -> str:
    if len(value.encode("utf-8", "ignore")) <= budget:
        return value
    raw = value.encode("utf-8", "ignore")[:budget]
    return raw.decode("utf-8", "ignore") + "…[已截断]"


_INGEST_HEALTH = "http://127.0.0.1:8850/ingest/health"
_MCP_URL = "http://127.0.0.1:8848/mcp"
_MCP_HOST, _MCP_PORT = "127.0.0.1", 8848
_APPLY_LOG_STALE_SEC = 30 * 60
_LOG_FILES = {
    "ingest": "d2a-ingest.log",
    "apply": "d2a-apply.log",
    "mcp": "d2a-mcp.log",
    "console": "d2a-console.log",
    "launcher": "d2a-launcher.log",   # 便携包启动器:进程重启 / 崩溃记录
}
_MCP_TOOLS = frozenset({"query_objects", "query_metrics"})

_FAILED_STATUSES = ("failed", "aborted")


def _map_run(r) -> dict:
    """d2a_sync_run 行 → RunSummary(带时区时间;错误安全截断)。"""
    started = obs.aware(r["started_at"])
    finished = obs.aware(r["finished_at"])
    return {
        "id": r["id"],
        "type": r["run_type"] if "run_type" in r.keys() else None,
        "status": r["status"],
        "source": r["source"],
        "started_at": started,
        "finished_at": finished,
        "duration_ms": (
            round((finished - started).total_seconds() * 1000)
            if started and finished else None),
        "tables": r["tables"],
        "rows": r["rows"],
        "quarantined": None,
        "dataset_version": None,
        "detail": obs.safe_error_summary(r["detail"]),
        "error": obs.safe_error_summary(
            r["detail"] if r["status"] in _FAILED_STATUSES else None),
        "error_id": None,
    }


def _map_step(s) -> dict:
    """d2a_run_step 行 → RunStep(水位 JSON 还原为值)。"""
    started = obs.aware(s["started_at"])
    finished = obs.aware(s["finished_at"])

    def _wm(v: str | None):
        if v is None:
            return None
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return None  # 损坏的水位证据按缺失处理,不原样透传

    return {
        "id": s["id"],
        "ordinal": s["ordinal"],
        "kind": s["kind"],
        "name": s["target"],
        "status": s["status"],
        "started_at": started,
        "finished_at": finished,
        "duration_ms": (
            round((finished - started).total_seconds() * 1000)
            if started and finished else None),
        "batch_id": s["batch_id"],
        "rows_in": s["rows_in"],
        "rows_out": s["rows_out"],
        "quarantined": s["quarantined"],
        "repaired": s["repaired"],
        "soft_deleted": s["soft_deleted"],
        "watermark_before": _wm(s["watermark_before"]),
        "watermark_after": _wm(s["watermark_after"]),
        "error": obs.safe_error_summary(s["error"]),
        "error_id": s["error_id"],
    }

# 数量口径说明(M3):名称、口径、数据来源;页面按此展示,不让用户猜数字含义
_COUNT_NOTES = [
    {"name": "raw_rows",
     "semantics": "当前配置范围内、未逻辑删除的 raw 活跃行数合计",
     "source": "raw_* 表 COUNT(*)"},
    {"name": "object_rows",
     "semantics": "已物化 obj_* 表行数合计;与 raw 因隔离/软删有差",
     "source": "obj_* 表 COUNT(*)"},
    {"name": "quarantine_pending",
     "semantics": "未处理(resolved_at 为空)的隔离行数",
     "source": "d2a_quarantine"},
]


def _client_host(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def _new_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)


def _probe_http(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500, "http"
    except urllib.error.HTTPError as e:
        return e.code < 500, "http"
    except Exception:
        return False, "http"


def _probe_tcp(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "tcp"
    except OSError:
        return False, "tcp"


def _probe_apply(log_dir: Path | None) -> tuple[bool, str]:
    if log_dir is not None:
        log_file = log_dir / _LOG_FILES["apply"]
        if log_file.exists():
            age = time.time() - log_file.stat().st_mtime
            if age <= _APPLY_LOG_STALE_SEC:
                return True, "log_mtime"
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5, check=False)
            text = out.stdout
        else:
            out = subprocess.run(
                ["pgrep", "-f", "data2agent.connect"],
                capture_output=True, text=True, timeout=5, check=False)
            return out.returncode == 0, "process"
        return "data2agent.connect" in text, "process"
    except Exception:
        return False, "process"


def _actions_sync_reconcile(cfg: ConnectConfig | None) -> bool:
    if cfg is None:
        return False
    for scfg in cfg.sources.values():
        if scfg.adapter != "mssql_readonly":
            continue
        if not scfg.dsn_env or scfg.dsn_env == "D2A_E10_DSN_PLACEHOLDER":
            return False
        if not os.environ.get(scfg.dsn_env):
            return False
    return True


def _platform_config_subset(cfg: ConnectConfig) -> dict[str, Any]:
    return {"templates": cfg.templates, "landing": cfg.landing}


def _validate_merged(path: Path, patch: dict[str, Any]) -> tuple[bool, list[dict[str, str]]]:
    """在临时副本上合并并 load_config,不写原文件。"""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy2(path, tmp_path)
    try:
        return merge_whitelist_and_save(tmp_path, PLATFORM_EDITABLE, patch, validate=load_config)
    finally:
        tmp_path.unlink(missing_ok=True)


def create_app(landing: str | None = None, templates: str = "templates",
               config: ConnectConfig | None = None, token: str | None = None,
               config_path: str | Path | None = None,
               log_dir: str | Path | None = None,
               home: str | Path | None = None) -> FastAPI:
    """landing/templates 可空:配合 home 做浏览器首次配置(needs_setup)。"""
    home_layout = HomeLayout.from_path(home) if home is not None else None
    if home_layout is not None:
        home_layout.ensure_dirs()
        if home_layout.secrets_env.is_file():
            apply_secrets_to_environ(home_layout.secrets_env)
        if token is None:
            token = os.environ.get("D2A_CONSOLE_TOKEN") or None
        if config is None and home_layout.platform_yaml.is_file():
            config = load_config(home_layout.platform_yaml)
            config_path = home_layout.platform_yaml

    if config is not None:  # 配置在场时以其为准,避免两套路径
        landing, templates = config.landing, config.templates
    elif landing is None:
        if home_layout is None:
            raise ValueError("create_app 需要 landing 或 home")
        landing, templates = "", templates

    _config_path = Path(config_path) if config_path else (
        home_layout.platform_yaml if home_layout else None
    )
    _log_dir = Path(log_dir) if log_dir else (
        home_layout.logs_dir if home_layout else None
    )

    state: dict[str, Any] = {
        "token": token,
        "config": config,
        "landing": landing or "",
        "templates": templates,
        "pack": None,
        "config_path": _config_path,
        "log_dir": _log_dir,
    }
    if state["landing"] and not (
        home_layout is not None and (_config_path is None or not Path(_config_path).is_file())
    ):
        state["pack"] = load_pack(templates)

    def needs_setup() -> bool:
        if home_layout is None:
            return False
        path = state["config_path"]
        return path is None or not Path(path).is_file()

    def hydrate_from_disk() -> None:
        path = state["config_path"]
        if path is None or not Path(path).is_file():
            return
        cfg = load_config(path)
        state["config"] = cfg
        state["landing"] = cfg.landing
        state["templates"] = cfg.templates
        state["pack"] = load_pack(cfg.templates)

    def auth(request: Request) -> None:
        path = request.url.path
        if path == "/api/data/raw" or path.startswith("/api/data/raw/"):
            return
        if needs_setup():
            if path in ("/api/setup", "/api/setup/status") or path.startswith("/api/setup"):
                if _client_host(request) not in _LOOPBACK:
                    raise HTTPException(403, "首次配置仅允许本机访问")
                return
            if path in ("/config", "/", "/logs", "/debug", "/v0") or path.startswith("/static"):
                return
            raise HTTPException(409, "尚未完成首次配置,请打开 /config")

        tok = state["token"]
        if not tok:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")
        if supplied != tok:
            raise HTTPException(401, "需要有效的管理界面登录密码")

    def store() -> LandingStore:
        if needs_setup() or not state["landing"]:
            raise HTTPException(409, "尚未完成首次配置")
        return LandingStore(state["landing"])

    def require_config() -> ConnectConfig:
        cfg = state["config"]
        if cfg is None:
            raise HTTPException(
                409, "控制台以只读模式运行(未加载 --config),动作不可用")
        return cfg

    def require_config_path() -> Path:
        path = state["config_path"]
        if path is None:
            raise HTTPException(409, "未配置 config_path(--config),配置 API 不可用")
        return Path(path)

    def _auth_supplied(request: Request) -> str:
        return request.headers.get("authorization", "").removeprefix("Bearer ").strip()

    def require_raw_browse_auth(db: LandingStore, request: Request, *,
                                source: str | None, resource: str,
                                offset: int | None = None,
                                limit: int | None = None) -> None:
        tok = state["token"]
        if not tok:
            db.log_access(
                subject="anonymous", resource_type="raw", source=source,
                resource=resource, allowed=False,
                reason_code="token_not_configured",
                page_offset=offset, page_limit=limit)
            raise HTTPException(403, "raw 浏览需配置控制台 Token 并显式认证")
        if _auth_supplied(request) != tok:
            db.log_access(
                subject="anonymous", resource_type="raw", source=source,
                resource=resource, allowed=False, reason_code="unauthorized",
                page_offset=offset, page_limit=limit)
            raise HTTPException(401, "需要有效的管理界面登录密码")

    def _is_raw_api_path(path: str) -> bool:
        return path == "/api/data/raw" or path.startswith("/api/data/raw/")

    def _raw_audit_target(request: Request) -> tuple[str | None, str]:
        path = request.url.path
        if path == "/api/data/raw":
            return None, "__catalog__"
        params = request.path_params
        if "source" in params and "table" in params:
            return str(params["source"]), str(params["table"])
        parts = path.split("/")
        if len(parts) >= 6:
            return parts[4], parts[5]
        return None, "__unknown__"

    def _query_int_or_none(request: Request, name: str) -> int | None:
        raw = request.query_params.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _require_aware_dt(value: datetime | None, name: str) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise HTTPException(422, f"{name} 必须为带时区 ISO 8601 时间")
        return value

    def _filter_time(value: datetime) -> str:
        return value.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

    def require_pack():
        pack = state["pack"]
        if pack is None:
            raise HTTPException(409, "尚未完成首次配置或模板不可用")
        return pack

    def default_source() -> str:
        cfg = state["config"]
        return next(iter(cfg.sources), "digiwin_e10") if cfg else "digiwin_e10"

    def _mcp_in_process(tool: str, params: dict[str, Any]) -> dict:
        from ..mcp_server.core import QueryService

        svc = QueryService(state["landing"], state["templates"], default_source())
        if tool == "query_objects":
            return svc.query_objects(**params)
        return svc.query_metrics(**params)

    def _mcp_http(tool: str, params: dict[str, Any]) -> dict:
        mcp_token = os.environ.get("D2A_MCP_TOKEN", "")
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": params}}
        import json

        data = json.dumps(body).encode()
        req = urllib.request.Request(
            _MCP_URL, data=data,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {mcp_token}"} if mcp_token else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise HTTPException(502, f"MCP HTTP 调用失败({e.code}):{detail}") from e
        except Exception as e:
            raise HTTPException(502, f"MCP HTTP 不可用:{e}") from e
        if "error" in payload:
            raise HTTPException(502, f"MCP 返回错误:{payload['error']}")
        return payload.get("result", payload)

    app = FastAPI(title="data2agent 运维控制台")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])
    jinja = _make_templates()

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        if not _is_raw_api_path(request.url.path):
            return JSONResponse(
                status_code=422,
                content={"detail": jsonable_encoder(exc.errors())},
            )
        try:
            db = store()
            source, resource = _raw_audit_target(request)
            offset = _query_int_or_none(request, "offset")
            limit = _query_int_or_none(request, "limit")
            try:
                require_raw_browse_auth(
                    db, request, source=source, resource=resource,
                    offset=offset, limit=limit)
            except HTTPException as auth_error:
                return JSONResponse(
                    status_code=auth_error.status_code,
                    content={"detail": auth_error.detail},
                )
            db.log_access(
                subject="console-admin", resource_type="raw", source=source,
                resource=resource, allowed=False, reason_code="invalid_query",
                page_offset=offset, page_limit=limit)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
        except Exception:
            return JSONResponse(
                status_code=500,
                content={"detail": "访问审计写入失败,raw 浏览已关闭"},
            )
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors())},
        )

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=getattr(app, "version", "0.1.0"),
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["HTTPBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "Token",
            "description": (
                "Console management Bearer Token. "
                "Prefer Authorization header; query ?token= is legacy-only and not "
                "recommended for Vue Console."
            ),
        }
        # Token is optional at runtime (disabled when unset). After first-time setup
        # completes with a token, /api/setup* also require Bearer — same as other
        # management APIs. During needs_setup the runtime skips token checks.
        for path, item in schema.get("paths", {}).items():
            if not path.startswith("/api"):
                continue
            for method, op in item.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                # 普通管理 API 的 Token 可按部署关闭;raw 原始数据浏览始终
                # 要求显式 Bearer,与运行时强门禁保持一致。
                op["security"] = (
                    [{"HTTPBearer": []}]
                    if _is_raw_api_path(path)
                    else [{"HTTPBearer": []}, {}]
                )
                if path in _SETUP_API_PATHS:
                    op["description"] = (
                        (op.get("description") or "")
                        + ("\n\n" if op.get("description") else "")
                        + "Auth: skipped only while needs_setup=true (first-time bootstrap). "
                        "After configuration, Bearer is required when D2A_CONSOLE_TOKEN is set."
                    ).strip()
        # M4:运行/审计列表的总数响应头显式声明(类型层必须可见)
        for path in ("/api/runs", "/api/audit"):
            get_op = schema.get("paths", {}).get(path, {}).get("get")
            if get_op is not None:
                get_op.setdefault("responses", {}).setdefault("200", {}) \
                    .setdefault("headers", {})["X-Total-Count"] = {
                        "schema": {"type": "integer"},
                        "description": "当前筛选条件下的总数(分页用)",
                    }
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    def page_ctx(request: Request) -> dict[str, Any]:
        return {
            "static_url": "/static",
            "needs_token": bool(state["token"]) and not needs_setup(),
            "needs_setup": needs_setup(),
        }

    @app.get("/")
    def index(request: Request):
        if needs_setup():
            return RedirectResponse("/config", status_code=302)
        return jinja.TemplateResponse(request, "dashboard.html", page_ctx(request))

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request) -> HTMLResponse:
        return jinja.TemplateResponse(request, "config.html", page_ctx(request))

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request) -> HTMLResponse:
        return jinja.TemplateResponse(request, "logs.html", page_ctx(request))

    @app.get("/debug", response_class=HTMLResponse)
    def debug_page(request: Request) -> HTMLResponse:
        return jinja.TemplateResponse(request, "debug.html", page_ctx(request))

    @app.get("/v0", response_class=HTMLResponse)
    def v0() -> str:
        return UI_HTML

    # ---- 首次配置 ----

    @api.get(
        "/setup/status",
        response_model=SetupStatusResponse,
        responses={403: _RESP_HTTP_ERROR[403]},
    )
    def setup_status() -> SetupStatusResponse:
        return SetupStatusResponse(
            needs_setup=needs_setup(),
            config_path=str(state["config_path"]) if state["config_path"] else None,
            home=str(home_layout.root) if home_layout else None,
        )

    @api.post(
        "/setup",
        response_model=SetupResponse,
        responses={400: {"model": HttpError}, 403: _RESP_HTTP_ERROR[403]},
    )
    def run_setup(
        body: SetupBody, request: Request
    ) -> SetupSuccessResponse | SetupFailureResponse:
        if _client_host(request) not in _LOOPBACK:
            raise HTTPException(403, "首次配置仅允许本机访问")
        if home_layout is None:
            raise HTTPException(400, "未启用 --home,无法浏览器首次配置")
        if not body.ingest_token.strip() or not body.console_token.strip():
            return SetupFailureResponse(
                ok=False,
                errors=[{"field": "token", "message": "Token 不能为空"}],
            )

        home_layout.ensure_dirs()
        mcp = (body.mcp_token or "").strip() or _new_token()
        save_secrets(home_layout.secrets_env, {
            "D2A_INGEST_TOKEN": body.ingest_token.strip(),
            "D2A_CONSOLE_TOKEN": body.console_token.strip(),
            "D2A_MCP_TOKEN": mcp,
        })
        apply_secrets_to_environ(home_layout.secrets_env)
        data = build_platform_yaml(home_layout)
        cfg_path = home_layout.platform_yaml
        write_yaml(cfg_path, data)
        try:
            load_config(cfg_path)
        except Exception as e:
            cfg_path.unlink(missing_ok=True)
            return SetupFailureResponse(
                ok=False,
                errors=[{"field": "", "message": str(e)}],
            )

        state["token"] = body.console_token.strip()
        state["config_path"] = cfg_path
        hydrate_from_disk()
        return SetupSuccessResponse(
            ok=True,
            restart_required=True,
            message=(
                "配置已写入。请用刚设置的管理界面登录密码登录;"
                "接收 / 物化 / MCP 服务需另行启动或重启后生效。"
            ),
            mcp_token_generated=not bool((body.mcp_token or "").strip()),
        )

    # ---- 只读视图 ----

    @api.get(
        "/overview",
        response_model=OverviewResponse,
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def overview() -> dict:
        db = store()
        pack = require_pack()
        cfg = state["config"]
        sources = sorted({r[0] for r in db.con.execute(
            "SELECT DISTINCT source FROM d2a_sync_state")}
            | (set(cfg.sources) if cfg else set()))
        out_sources = []
        for s in sources:
            sync_state = [dict(r) for r in db.con.execute(
                "SELECT table_name, watermark_col, high_water, last_run_at "
                "FROM d2a_sync_state WHERE source = ? ORDER BY table_name", (s,))]
            (quarantined,) = db.con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine WHERE source = ? AND resolved_at IS NULL",
                (s,)).fetchone()
            out_sources.append({"source": s, "state": sync_state, "quarantined": quarantined})
        objects = []
        for o in pack.objects:
            try:
                row = db.con.execute(
                    f'SELECT COUNT(*) AS n, MAX("_d2a_mapped_at") AS m FROM "obj_{o.object}"'
                ).fetchone()
                rows, mapped_at = row["n"], row["m"]
            except sqlite3.OperationalError:
                rows, mapped_at = None, None  # 尚未物化
            (q,) = db.con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine WHERE object = ? AND resolved_at IS NULL",
                (o.object,)).fetchone()
            objects.append({"object": o.object, "display_name": o.display_name,
                            "rows": rows, "mapped_at": mapped_at, "quarantined": q})

        # ---- M3 观测聚合(observability;查询失败按字段降级为 null + 告警)----
        query_failures: list[str] = []
        # raw 行数:任一源查询失败则整体为 null,部分合计不得冒充总数
        raw_rows_total = 0
        raw_failed = False
        agg_sources = sorted(set(cfg.sources) if cfg else {s["source"] for s in out_sources})
        for s in agg_sources:
            try:
                tables = obs.raw_table_names(db, s)
            except Exception:
                raw_failed = True
                continue
            r_rows, _r_latest = obs.raw_stats(db, s, tables)
            if r_rows is None:
                raw_failed = True
            else:
                raw_rows_total += r_rows
        if raw_failed:
            query_failures.append("raw 行数查询失败(部分源),raw_rows 置为不可检测")
        obj_stats = obs.object_stats(db, pack)
        obj_errors = [v["error"] for v in obj_stats.values() if v.get("error")]
        if obj_errors:
            query_failures.append(obj_errors[0])
        materialized = [v for v in obj_stats.values() if v["rows"] is not None]
        object_rows = (
            sum(v["rows"] for v in materialized)
            if materialized and not obj_errors
            else None
        )
        try:
            (lr,) = db.con.execute("SELECT MAX(last_run_at) FROM d2a_sync_state").fetchone()
            last_run_at = obs.aware(lr)
        except sqlite3.Error:
            last_run_at = None
            query_failures.append("最近运行时间查询失败(d2a_sync_state)")
        mapped_vals = [v["mapped_at"] for v in obj_stats.values() if v["mapped_at"] is not None]
        try:
            app_version = importlib.metadata.version("data2agent")
        except importlib.metadata.PackageNotFoundError:
            app_version = None  # 开发环境无包元数据:明确 null(unknown)
        bs = obs.binding_summary(pack)
        qp = obs.quarantine_pending(db)
        default_src = next(iter(agg_sources), "digiwin_e10")
        nodes = obs.compute_nodes(db, pack, cfg, default_src,
                                  component_version=app_version)
        recent = obs.recent_runs(db)
        if recent is None:
            query_failures.append("最近运行查询失败(d2a_sync_run)")
        trend = obs.sync_trend(db)
        if trend is None:
            query_failures.append("抽取趋势查询失败(d2a_sync_run)")
        alerts = obs.build_alerts(nodes, quarantine=qp, drafts=bs["draft"],
                                  query_failures=query_failures)

        return {"landing": state["landing"], "readonly": cfg is None,
                "actions_sync_reconcile": _actions_sync_reconcile(cfg),
                "sources": out_sources, "objects": objects,
                "needs_setup": False,
                "generated_at": datetime.now().astimezone(),
                "summary": {
                    "raw_rows": None if raw_failed else raw_rows_total,
                    "object_rows": object_rows,
                    "materialized_objects": len(materialized),
                    "template_objects": len(pack.objects),
                    "quarantine_pending": qp,
                    "last_run_at": last_run_at,
                    "data_updated_at": max(mapped_vals) if mapped_vals else None,
                },
                "versions": {
                    "app": app_version, "template": pack.version,
                    "dataset": None, "object": None,  # v0.3 前恒 null(尚未启用)
                },
                "binding_summary": bs,
                "alerts": alerts,
                "recent_runs": recent,
                "sync_trend": trend,
                "count_notes": _COUNT_NOTES}

    @api.get(
        "/runs",
        response_model=list[RunSummary],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409],
                   422: _RESP_HTTP_ERROR[422]},
    )
    def runs(response: Response, limit: int = 50, offset: int = 0,
             type: RunType | None = None,
             status: RunStatus | None = None) -> list[dict]:
        """运行列表:数组 wire shape + X-Total-Count 响应头(M4)。

        筛选 type/status 与领域模型一致(数据库列 run_type 只是内部实现);
        排序固定 started_at DESC, id DESC;时间规范化为带时区。
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "limit 须为 1..100,offset 须 >= 0")
        where: list[str] = []
        params: list[Any] = []
        if type is not None:
            where.append("run_type = ?")
            params.append(type)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        db = store()
        (total,) = db.con.execute(
            f"SELECT COUNT(*) FROM d2a_sync_run{wsql}", params).fetchone()
        response.headers["X-Total-Count"] = str(total)
        rows = db.con.execute(
            f"SELECT * FROM d2a_sync_run{wsql} "
            "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset])
        return [_map_run(r) for r in rows]

    @api.get(
        "/runs/{run_id}",
        response_model=RunDetailResponse,
        responses={401: _RESP_HTTP_ERROR[401], 404: {"model": HttpError},
                   409: _RESP_HTTP_ERROR[409]},
    )
    def run_detail(run_id: int) -> dict:
        """运行详情:统一 Run + step;历史无 step 返回 legacy_unavailable(不伪造)。"""
        db = store()
        r = db.con.execute(
            "SELECT * FROM d2a_sync_run WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            raise HTTPException(404, f"运行 #{run_id} 不存在")
        steps = db.steps_for_run(run_id)
        steps_state = (
            "available"
            if steps or r["steps_recorded"] == 1
            else "legacy_unavailable"
        )
        return {
            **_map_run(r),
            "steps_state": steps_state,
            "steps": [_map_step(s) for s in steps],
        }

    @api.get(
        "/quarantine",
        response_model=list[QuarantineRecord],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def quarantine(object: str | None = None) -> list[dict]:
        where, params = "resolved_at IS NULL", []
        if object:
            where += " AND object = ?"
            params.append(object)
        return [dict(r) for r in store().con.execute(
            f"SELECT id, source, object, keys_json, reason, created_at "
            f"FROM d2a_quarantine WHERE {where} ORDER BY id DESC LIMIT 200", params)]

    @api.get(
        "/audit",
        response_model=list[AuditRecord],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409],
                   422: _RESP_HTTP_ERROR[422]},
    )
    def audit(response: Response, limit: int = 50, offset: int = 0,
              source: str | None = None, action: str | None = None,
              from_: datetime | None = Query(None, alias="from"),
              to: datetime | None = None) -> list[dict]:
        """SQL 操作审计(d2a_audit_log):筛选 + X-Total-Count(M4)。

        时间区间为带时区 ISO 8601 闭开区间 [from, to);筛选全部参数化;
        排序固定 ts DESC, id DESC。不用 SQL 文本推断 Run 状态。
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "limit 须为 1..100,offset 须 >= 0")
        from_ = _require_aware_dt(from_, "from")
        to = _require_aware_dt(to, "to")
        if from_ is not None and to is not None and from_ >= to:
            raise HTTPException(422, "时间区间非法:from 必须早于 to(闭开区间)")
        where: list[str] = []
        params: list[Any] = []
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if action is not None:
            where.append("action = ?")
            params.append(action)
        if from_ is not None:
            where.append("ts >= ?")
            params.append(_filter_time(from_))
        if to is not None:
            where.append("ts < ?")
            params.append(_filter_time(to))
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        db = store()
        (total,) = db.con.execute(
            f"SELECT COUNT(*) FROM d2a_audit_log{wsql}", params).fetchone()
        response.headers["X-Total-Count"] = str(total)
        rows = db.con.execute(
            f"SELECT id, ts, source, action, sql, rows, duration_ms "
            f"FROM d2a_audit_log{wsql} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset])
        return [{
            "id": r["id"],
            "ts": obs.aware(r["ts"]),
            "source": r["source"],
            "action": r["action"],
            "sql": _budget_text(r["sql"]),
            "rows": r["rows"],
            "duration_ms": r["duration_ms"],
        } for r in rows]

    @api.get(
        "/audit/access",
        response_model=AccessAuditPage,
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409],
                   422: _RESP_HTTP_ERROR[422]},
    )
    def audit_access(limit: int = 50, offset: int = 0,
                     subject: str | None = None,
                     resource_type: str | None = None,
                     allowed: bool | None = None,
                     from_: datetime | None = Query(None, alias="from"),
                     to: datetime | None = None) -> dict:
        """控制台数据访问审计(d2a_console_access_audit,M4)。

        只含主体/目标/允许与否/查询形状/行数;不含 Token、查询值原文、返回值。
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "limit 须为 1..100,offset 须 >= 0")
        if resource_type is not None and resource_type not in ("raw", "object"):
            raise HTTPException(422, f"未知 resource_type '{resource_type}'")
        from_ = _require_aware_dt(from_, "from")
        to = _require_aware_dt(to, "to")
        if from_ is not None and to is not None and from_ >= to:
            raise HTTPException(422, "时间区间非法:from 必须早于 to(闭开区间)")
        where: list[str] = []
        params: list[Any] = []
        if subject is not None:
            where.append("subject = ?")
            params.append(subject)
        if resource_type is not None:
            where.append("resource_type = ?")
            params.append(resource_type)
        if allowed is not None:
            where.append("allowed = ?")
            params.append(1 if allowed else 0)
        if from_ is not None:
            where.append("ts >= ?")
            params.append(_filter_time(from_))
        if to is not None:
            where.append("ts < ?")
            params.append(_filter_time(to))
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        db = store()
        (total,) = db.con.execute(
            f"SELECT COUNT(*) FROM d2a_console_access_audit{wsql}", params).fetchone()
        rows = db.con.execute(
            f"SELECT * FROM d2a_console_access_audit{wsql} "
            "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset])
        return {
            "items": [{
                "id": r["id"],
                "ts": obs.aware(r["ts"]),
                "subject": r["subject"],
                "resource_type": r["resource_type"],
                "source": r["source"],
                "resource": r["resource"],
                "allowed": bool(r["allowed"]),
                "reason_code": r["reason_code"],
                "offset": r["page_offset"],
                "limit": r["page_limit"],
                "returned_rows": r["returned_rows"],
                "request_id": r["request_id"],
            } for r in rows],
            "offset": offset,
            "limit": limit,
            "total": total,
            "generated_at": datetime.now().astimezone(),
        }

    # ---- 配置 / 服务 / 日志 / 调试 ----

    @api.get(
        "/config",
        response_model=ConfigViewResponse,
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def get_config() -> dict:
        if needs_setup():
            return {"needs_setup": True, "templates": "", "landing": ""}
        path = require_config_path()
        out = _platform_config_subset(load_config(path))
        out["needs_setup"] = False
        return out

    @api.post(
        "/config",
        response_model=ConfigSaveResponse,
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def post_config(body: ConfigPatch) -> dict:
        path = require_config_path()
        patch = body.model_dump(exclude_none=True)
        ok, errors = merge_whitelist_and_save(
            path, PLATFORM_EDITABLE, patch, validate=load_config)
        if ok:
            hydrate_from_disk()
        return {"ok": ok, "errors": errors, "restart_required": True}

    @api.post(
        "/config/validate",
        response_model=ValidationResult,
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def validate_config(body: ConfigPatch) -> dict:
        path = require_config_path()
        ok, errors = _validate_merged(path, body.model_dump(exclude_none=True))
        return {"ok": ok, "errors": errors}

    @api.get(
        "/services",
        response_model=ServicesStatusResponse,
        responses={401: _RESP_HTTP_ERROR[401]},
    )
    def services() -> dict:
        ingest_ok, ingest_method = _probe_http(_INGEST_HEALTH)
        mcp_ok, mcp_method = _probe_http(_MCP_URL)
        if not mcp_ok:
            mcp_ok, mcp_method = _probe_tcp(_MCP_HOST, _MCP_PORT)
        apply_ok, apply_method = _probe_apply(state["log_dir"])
        return {
            "ingest": {"ok": ingest_ok, "method": ingest_method},
            "mcp": {"ok": mcp_ok, "method": mcp_method},
            "apply": {"ok": apply_ok, "method": apply_method},
            "console": {"ok": True, "method": "self"},
        }

    @api.get(
        "/logs",
        response_model=LogsResponse,
        responses={
            400: {"model": HttpError},
            401: _RESP_HTTP_ERROR[401],
        },
    )
    def get_logs(service: str, lines: int = 200, level: str | None = None) -> dict:
        if service not in _LOG_FILES:
            raise HTTPException(400, f"未知服务 '{service}',可用:{sorted(_LOG_FILES)}")
        log_dir = state["log_dir"]
        if log_dir is None:
            return {"ok": False, "text": "未配置日志目录(--log-dir)"}
        capped = max(1, min(lines, 1000))
        ok, text = tail_lines(Path(log_dir) / _LOG_FILES[service], lines=capped, level=level)
        return {"ok": ok, "text": text}

    @api.get(
        "/debug/raw-table",
        response_model=RawTablePageResponse,
        responses={
            400: {"model": HttpError},
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError},
            409: _RESP_HTTP_ERROR[409],
        },
    )
    def debug_raw_table(table: str, offset: int = 0, limit: int = 50) -> dict:
        if not table.startswith("raw_"):
            raise HTTPException(400, "仅允许 raw_* 表")
        db = store()
        allowed = {r[0] for r in db.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'")}
        if table not in allowed:
            raise HTTPException(404, f"表 '{table}' 不存在或不在 raw_* 白名单")
        capped = max(1, min(limit, 200))
        off = max(0, offset)
        rows = [dict(r) for r in db.con.execute(
            f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (capped, off))]
        (total,) = db.con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return {"table": table, "offset": off, "limit": capped, "total": total, "rows": rows}

    @api.post(
        "/debug/mcp-call",
        response_model=McpToolResult,
        responses={
            400: {"model": HttpError},
            401: _RESP_HTTP_ERROR[401],
            409: _RESP_HTTP_ERROR[409],
            502: {"model": HttpError},
            503: {"model": HttpError},
        },
    )
    def debug_mcp_call(body: McpCallBody) -> dict:
        if body.tool not in _MCP_TOOLS:
            raise HTTPException(400, f"工具 '{body.tool}' 不在白名单,可用:{sorted(_MCP_TOOLS)}")
        try:
            return _mcp_in_process(body.tool, body.params)
        except Exception as inproc_err:
            try:
                return _mcp_http(body.tool, body.params)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(
                    503,
                    f"MCP 不可用(进程内:{inproc_err};HTTP 亦失败,请确认 d2a-mcp 已启动)") from inproc_err

    # ---- 动作(复用 connect 引擎,窗口 / 白名单原样生效)----

    def _scfg(cfg: ConnectConfig, source: str):
        scfg = cfg.sources.get(source)
        if scfg is None:
            raise HTTPException(404, f"配置中没有源 '{source}',可用:{sorted(cfg.sources)}")
        return scfg

    @api.post(
        "/actions/sync",
        response_model=ActionExecutionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError},
            409: _RESP_HTTP_ERROR[409],
        },
    )
    def action_sync(body: ActionBody) -> dict:
        cfg = require_config()
        executed = run_sync_cycle(
            body.source, _scfg(cfg, body.source), require_pack(), cfg.landing)
        return {"executed": executed,
                "note": "" if executed else "错峰窗口外,未发起(窗口约束对控制台同样生效)"}

    @api.post(
        "/actions/reconcile",
        response_model=ActionExecutionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError},
            409: _RESP_HTTP_ERROR[409],
        },
    )
    def action_reconcile(body: ActionBody) -> dict:
        cfg = require_config()
        executed = run_reconcile_cycle(
            body.source, _scfg(cfg, body.source), require_pack(),
            cfg.landing, deep=body.deep)
        return {"executed": executed,
                "note": "" if executed else "错峰窗口外,未发起"}

    @api.post(
        "/actions/apply",
        response_model=ApplyActionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            409: _RESP_HTTP_ERROR[409],
        },
    )
    def action_apply(body: ActionBody) -> dict:
        report = apply_objects(store(), require_pack(), body.source)
        return {"executed": True, "results": [asdict(r) for r in report.results],
                "aborted": [r.object for r in report.aborted]}

    @api.post(
        "/actions/retry",
        response_model=RetryActionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError},
            409: _RESP_HTTP_ERROR[409],
            422: _RESP_HTTP_ERROR[422],
        },
    )
    def action_retry(body: ActionBody) -> dict:
        if not body.object:
            raise HTTPException(422, "retry 需要 object 参数")
        pack = require_pack()
        tpl = next((o for o in pack.objects if o.object == body.object), None)
        if tpl is None:
            raise HTTPException(404, f"未知对象 '{body.object}'")
        try:
            result = apply_object(store(), tpl, body.source)
        except MappingCircuitBreaker as e:
            raise HTTPException(409, f"重试触发熔断:{e}") from e
        return {"executed": True, **asdict(result)}

    # ---- v0.2 M3:真实观测端点 ----

    def _probe_mcp() -> tuple[bool, str]:
        ok, method = _probe_http(_MCP_URL)
        if not ok:
            ok, method = _probe_tcp(_MCP_HOST, _MCP_PORT)
        return ok, method

    @api.get(
        "/pipeline",
        response_model=PipelineResponse,
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def pipeline() -> dict:
        """真实管道状态:固定 7 节点 + 折叠总体状态(观测口径见 observability)。

        服务探测与数据健康分开:MCP/ingest 进程健康不覆盖数据 stale。
        """
        db = store()
        cfg = state["config"]
        probes = {
            "ingest": lambda: _probe_http(_INGEST_HEALTH),
            "mcp": _probe_mcp,
        }
        try:
            component_version = importlib.metadata.version("data2agent")
        except importlib.metadata.PackageNotFoundError:
            component_version = None
        return obs.build_pipeline(db, require_pack(), cfg, default_source(),
                                  probes=probes, component_version=component_version)

    # ---- v0.2 契约桩(M2)----
    # schema 先行供前端类型生成与 Mock;真实实现归属 M4–M6,
    # 实现前一律返回 501,不得返回伪造成功或空数据。

    _STUB_501 = "契约桩:端点已声明,将在所属里程碑实现;不得视为成功或空数据"

    @api.get(
        "/data/raw",
        response_model=RawTableCatalogResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            403: {"model": HttpError, "description": "未配置控制台 Token,raw 目录关闭"},
            409: _RESP_HTTP_ERROR[409],
            500: _RESP_HTTP_ERROR[500],
        },
    )
    def data_raw_catalog(request: Request) -> dict:
        """raw 目录:当前配置允许且确实存在的表(不含 SQLite 内部表)。"""
        db = store()
        require_raw_browse_auth(db, request, source=None, resource="__catalog__")
        cfg = state["config"]
        pack = require_pack()
        try:
            items, warnings = br.raw_catalog(
                db, pack, br.allowed_sources(pack, sorted(cfg.sources) if cfg else []))
        except Exception as e:
            try:
                db.log_access(
                    subject="console-admin", resource_type="raw", source=None,
                    resource="__catalog__", allowed=False,
                    reason_code="catalog_failed")
            except Exception as audit_error:
                raise HTTPException(500, "raw 目录失败且访问审计写入失败") from audit_error
            raise HTTPException(500, "raw 目录失败") from e
        try:
            db.log_access(
                subject="console-admin", resource_type="raw", source=None,
                resource="__catalog__", allowed=True, reason_code="ok",
                returned_rows=len(items))
        except Exception as audit_error:
            raise HTTPException(500, "raw 目录访问审计写入失败") from audit_error
        return {"items": items, "warnings": warnings,
                "generated_at": datetime.now().astimezone()}

    @api.get(
        "/data/raw/{source}/{table}",
        response_model=RawDataPageResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            403: {"model": HttpError, "description": "未配置控制台 Token,raw 浏览关闭"},
            404: {"model": HttpError},
            409: _RESP_HTTP_ERROR[409],
            422: _RESP_HTTP_ERROR[422],
        },
    )
    def data_raw(source: str, table: str, request: Request,
                 offset: int = 0, limit: int = 50, q: str = "") -> dict:
        """raw 白名单分页浏览(强鉴权 + 访问审计,§4.7)。

        必须配置控制台 Token 且请求携带有效 Bearer;每次尝试(允许/拒绝)
        都写不泄密访问审计;审计失败则请求失败关闭。
        """
        db = store()
        pack = require_pack()
        cfg = state["config"]
        require_raw_browse_auth(
            db, request, source=source, resource=table, offset=offset, limit=limit)
        try:
            br.require_source(
                br.allowed_sources(pack, sorted(cfg.sources) if cfg else []), db, source)
            br.require_raw_table(db, source, table, br.allowed_raw_tables(pack, source))
        except br.BrowseError as e:
            db.log_access(
                subject="console-admin", resource_type="raw", source=source,
                resource=table, allowed=False, reason_code="not_in_catalog",
                page_offset=offset, page_limit=limit)
            raise HTTPException(e.status, e.detail) from e
        try:
            cols = br.raw_column_meta(db, pack, source, table)
            page = br.browse_table(
                db, br.physical_raw(source, table), cols,
                limit=limit, offset=offset, q=q,
                base_where="_d2a_deleted_at IS NULL")
        except br.BrowseError as e:
            db.log_access(
                subject="console-admin", resource_type="raw", source=source,
                resource=table, allowed=False, reason_code="invalid_query",
                page_offset=offset, page_limit=limit)
            raise HTTPException(e.status, e.detail) from e
        except Exception as e:
            try:
                db.log_access(
                    subject="console-admin", resource_type="raw", source=source,
                    resource=table, allowed=False, reason_code="browse_failed",
                    page_offset=offset, page_limit=limit)
            except Exception as audit_error:
                raise HTTPException(500, "raw 浏览失败且访问审计写入失败") from audit_error
            raise HTTPException(500, "raw 浏览失败") from e
        db.log_access(
            subject="console-admin", resource_type="raw", source=source,
            resource=table, allowed=True, reason_code="ok",
            page_offset=offset, page_limit=limit,
            returned_rows=len(page["rows"]))
        warnings = [f"列 {c['name']} 分类未知,按未确认处理展示"
                    for c in cols if c["classification"] == "unknown"]
        return {
            "source": source,
            "table": table,
            "columns": cols,
            "rows": page["rows"],
            "truncations": page["truncations"],
            "offset": offset,
            "limit": limit,
            "total": page["total"],
            "sort": page["sort"],
            "query": q,
            "searchable": page["searchable"],
            "warnings": warnings,
            "generated_at": datetime.now().astimezone(),
        }

    @api.get(
        "/objects",
        response_model=list[ObjectSummary],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def objects_catalog() -> list[dict]:
        """对象目录:模板 ∩ 物化状态;未物化为 rows=null + warning,不伪装 0。"""
        db = store()
        pack = require_pack()
        out = []
        for tpl in pack.objects:
            try:
                row = db.con.execute(
                    f'SELECT COUNT(*) AS n, MAX("_d2a_mapped_at") AS m '
                    f'FROM {br.quote_ident(br.physical_object(tpl.object))}').fetchone()
                rows, mapped_at, warning = row["n"], obs.aware(row["m"]), None
            except sqlite3.Error:
                rows, mapped_at, warning = None, None, "尚未物化"
            (qp,) = db.con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine "
                "WHERE object = ? AND resolved_at IS NULL", (tpl.object,)).fetchone()
            out.append({
                "object": tpl.object,
                "display_name": tpl.display_name,
                "domain": tpl.domain,
                "rows": rows,
                "mapped_at": mapped_at,
                "quarantined": qp,
                "version": None,
                "searchable": bool(tpl.keys),
                "warning": warning,
            })
        return out

    @api.get(
        "/objects/{object}",
        response_model=ObjectRowsPageResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError},
            409: {"model": HttpError, "description": "模板存在但尚未物化"},
            422: _RESP_HTTP_ERROR[422],
        },
    )
    def object_rows(object: str, offset: int = 0,
                    limit: int = 50, q: str = "") -> dict:
        """对象分页浏览(敏感属性服务端永久脱敏)。"""
        db = store()
        pack = require_pack()
        tpl = next((o for o in pack.objects if o.object == object), None)
        if tpl is None:
            raise HTTPException(404, f"未知对象 '{object}'")
        if not br.table_exists(db, br.physical_object(object)):
            raise HTTPException(409, f"对象 '{object}' 尚未物化")
        cols = br.object_column_meta(db, tpl)
        try:
            page = br.browse_table(
                db, br.physical_object(object), cols,
                limit=limit, offset=offset, q=q)
        except br.BrowseError as e:
            raise HTTPException(e.status, e.detail) from e
        warnings = [f"列 {c['name']} 分类未知,按未确认处理展示"
                    for c in cols if c["classification"] == "unknown"]
        return {
            "object": object,
            "columns": cols,
            "rows": page["rows"],
            "truncations": page["truncations"],
            "offset": offset,
            "limit": limit,
            "total": page["total"],
            "sort": page["sort"],
            "query": q,
            "searchable": page["searchable"],
            "warnings": warnings,
            "generated_at": datetime.now().astimezone(),
        }

    @api.get(
        "/templates",
        response_model=list[TemplateObject],
        responses={401: _RESP_HTTP_ERROR[401], 501: _RESP_HTTP_ERROR_STUB[501]},
        tags=["v0.2-stub"],
    )
    def templates_view() -> list[TemplateObject]:
        raise HTTPException(501, f"{_STUB_501}(M5 模板只读展示)")

    @api.post(
        "/gateway/proposals",
        response_model=ProposalResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            422: _RESP_HTTP_ERROR[422],
            501: _RESP_HTTP_ERROR_STUB[501],
        },
        tags=["v0.2-stub"],
    )
    def gateway_proposals(body: ProposalRequest) -> ProposalResponse:
        raise HTTPException(501, f"{_STUB_501}(M6 MCP Lab 建议卡)")

    app.include_router(api)

    @app.middleware("http")
    async def _html_gate(request: Request, call_next):
        if request.url.path.startswith("/api"):
            return await call_next(request)
        if needs_setup() and request.url.path not in (
            "/config", "/", "/logs", "/debug", "/v0"
        ) and not request.url.path.startswith("/static"):
            return RedirectResponse("/config")
        return await call_next(request)

    if _ADMIN_STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=_ADMIN_STATIC), name="static")
    return app
