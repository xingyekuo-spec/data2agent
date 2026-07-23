"""中间机管理 FastAPI:配置 / 首次浏览器配置 / 状态 / 日志 / 调试。"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape
from pydantic import BaseModel, Field

from ..admin_common.config_edit import MIDDLE_EDITABLE, merge_whitelist_and_save
from ..admin_common.home_layout import HomeLayout
from ..admin_common.logs import tail_lines
from ..admin_common.secrets_file import apply_secrets_to_environ, save_secrets
from ..admin_common.setup_yaml import (
    build_middle_connect_yaml,
    build_odbc_dsn,
    write_yaml,
)
from ..connect.config import ConnectConfig, SourceConfig, load_config
from ..connect.landing import LandingStore
from ..connect.scheduler import build_adapter, run_sync_cycle

from .status import build_status

_PKG = Path(__file__).resolve().parent
_ADMIN_TEMPLATES = _PKG.parent / "admin_templates"
_MIDDLE_TEMPLATES = _PKG / "templates"
_ADMIN_STATIC = _ADMIN_TEMPLATES / "static"
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _make_templates() -> Jinja2Templates:
    env = Environment(
        loader=ChoiceLoader([
            FileSystemLoader(str(_MIDDLE_TEMPLATES)),
            FileSystemLoader(str(_ADMIN_TEMPLATES)),
            PrefixLoader({"admin": FileSystemLoader(str(_ADMIN_TEMPLATES))}),
        ]),
        autoescape=select_autoescape(["html", "xml"]),
    )
    return Jinja2Templates(env=env)


class ConfigPatch(BaseModel):
    templates: str | None = None
    landing: str | None = None
    sources: dict[str, Any] | None = None


class TriggerBody(BaseModel):
    action: str
    source: str | None = None


class TestConnectionBody(BaseModel):
    source: str | None = None


class SetupBody(BaseModel):
    """浏览器首次/完整配置(替代 setup-middle.ps1)。密码只写入 secrets.env。"""
    platform_url: str = Field(..., description="http://平台IP:8850")
    erp_server: str
    erp_database: str
    erp_user: str
    erp_password: str
    erp_port: int = 1433
    ingest_token: str
    admin_token: str
    sync_every: str = "30m"
    lookback: str = "3d"
    batch_size: int = 5000
    rows_per_second: int = 2000


_DSN_PATTERN = re.compile(
    r"(?i)(password\s*=|pwd\s*=|server\s*=|data\s+source\s*=|"
    r"uid\s*=|user\s+id\s*=|integrated\s+security|"
    r"file:[^\s?]+|\.sqlite\b|\.db\b)"
)


def _env_set(name: str | None) -> bool | None:
    if not name:
        return None
    return os.environ.get(name) is not None


def _client_host(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host or ""


def _config_subset(cfg: ConnectConfig) -> dict:
    out: dict[str, Any] = {"templates": cfg.templates, "landing": cfg.landing, "sources": {}}
    for name, scfg in cfg.sources.items():
        src: dict[str, Any] = {
            "windows": scfg.windows,
            "rate": {"batch_size": scfg.rate.batch_size,
                     "rows_per_second": scfg.rate.rows_per_second},
            "lookback": scfg.lookback,
            "sync_every": scfg.sync_every,
            "tables": {
                tbl: {"mode": spec.mode, "watermark": spec.watermark}
                for tbl, spec in (scfg.tables or {}).items()
            },
            "sink": {"url": scfg.sink.url,
                     "token_env": scfg.sink.token_env,
                     "token_env_set": _env_set(scfg.sink.token_env)},
            "dsn_env": scfg.dsn_env,
            "dsn_env_set": _env_set(scfg.dsn_env),
        }
        out["sources"][name] = src
    return out


def _patch_to_dict(body: ConfigPatch) -> dict[str, Any]:
    return body.model_dump(exclude_none=True)


def _resolve_source(cfg: ConnectConfig, source: str | None) -> tuple[str, SourceConfig]:
    if source is not None:
        scfg = cfg.sources.get(source)
        if scfg is None:
            raise HTTPException(404, f"配置中没有源 '{source}',可用:{sorted(cfg.sources)}")
        return source, scfg
    name = next(iter(cfg.sources))
    return name, cfg.sources[name]


def _sanitize_detail(message: str) -> str:
    if _DSN_PATTERN.search(message):
        return "连接失败(响应中已省略凭据/连接串细节)"
    return message[:500]


def _probe_connection(name: str, scfg: SourceConfig, landing_path: str) -> list[str]:
    landing = LandingStore(landing_path)
    adapter = build_adapter(name, scfg, landing)
    tables: list[str] = []
    for tbl in sorted(adapter.whitelist):
        adapter.table_info(tbl)
        tables.append(tbl)
    return tables


def _validate_merged(path: Path, patch: dict[str, Any]) -> tuple[bool, list[dict[str, str]]]:
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy2(path, tmp_path)
    try:
        return merge_whitelist_and_save(tmp_path, MIDDLE_EDITABLE, patch, validate=load_config)
    finally:
        tmp_path.unlink(missing_ok=True)


def create_app(
    config_path: str | Path | None = None,
    token: str | None = None,
    log_path: str | Path | None = None,
    home: str | Path | None = None,
) -> FastAPI:
    """config_path 可空:配合 home 做浏览器首次配置(needs_setup)。"""
    home_layout = HomeLayout.from_path(home) if home is not None else None
    if home_layout is not None:
        home_layout.ensure_dirs()
        if home_layout.secrets_env.is_file():
            apply_secrets_to_environ(home_layout.secrets_env)
        if token is None:
            token = os.environ.get("D2A_MIDDLE_ADMIN_TOKEN") or None

    if config_path is not None:
        cfg_path = Path(config_path)
    elif home_layout is not None:
        cfg_path = home_layout.connect_yaml
    else:
        raise ValueError("create_app 需要 config_path 或 home")

    _log_path = Path(log_path) if log_path else (
        home_layout.logs_dir / "d2a-connector.log" if home_layout else None
    )
    _log_dir = (
        home_layout.logs_dir if home_layout is not None
        else (_log_path.parent if _log_path is not None else None)
    )
    _LOG_FILES = {
        "connector": "d2a-connector.log",   # 抽取 / 推送
        "admin": "d2a-middle-admin.log",    # 管理界面自身(500 报错栈在此)
        "launcher": "d2a-launcher.log",     # 便携包启动器:进程重启记录
    }

    state = {"token": token}

    def needs_setup() -> bool:
        return not cfg_path.is_file()

    def auth(request: Request) -> None:
        path = request.url.path
        # 首次配置:仅本机可无 Token 访问 setup / 读状态
        if needs_setup():
            if path in ("/api/setup", "/api/setup/status") or path.startswith("/api/setup"):
                if _client_host(request) not in _LOOPBACK:
                    raise HTTPException(403, "首次配置仅允许本机访问")
                return
            if path in ("/config", "/", "/status", "/logs") or path.startswith("/static"):
                return
            raise HTTPException(409, "尚未完成首次配置,请打开 /config")

        tok = state["token"]
        if not tok:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")
        if supplied != tok:
            raise HTTPException(401, "需要有效的管理界面登录密码")

    def reload_config() -> ConnectConfig:
        if needs_setup():
            raise HTTPException(409, "尚未完成首次配置")
        return load_config(cfg_path)

    app = FastAPI(title="data2agent 中间机管理")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])
    templates = _make_templates()

    def page_ctx(request: Request) -> dict[str, Any]:
        return {
            "static_url": "/static",
            "needs_token": bool(state["token"]) and not needs_setup(),
            "needs_setup": needs_setup(),
        }

    @app.get("/")
    def index() -> RedirectResponse:
        if needs_setup():
            return RedirectResponse("/config", status_code=302)
        return RedirectResponse("/status", status_code=302)

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "status.html", page_ctx(request))

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "config.html", page_ctx(request))

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "logs.html", page_ctx(request))

    @api.get("/setup/status")
    def setup_status() -> dict:
        return {
            "needs_setup": needs_setup(),
            "config_path": str(cfg_path),
            "home": str(home_layout.root) if home_layout else None,
        }

    @api.post("/setup")
    def run_setup(body: SetupBody, request: Request) -> dict:
        if _client_host(request) not in _LOOPBACK:
            raise HTTPException(403, "首次配置仅允许本机访问")
        if home_layout is None:
            raise HTTPException(400, "未启用 --home,无法浏览器首次配置")
        if not body.platform_url.startswith("http"):
            return {"ok": False, "errors": [{"field": "platform_url", "message": "须为 http(s) URL"}]}
        if not body.admin_token.strip() or not body.ingest_token.strip():
            return {"ok": False, "errors": [{"field": "token", "message": "Token 不能为空"}]}

        home_layout.ensure_dirs()
        data = build_middle_connect_yaml(
            home_layout,
            platform_url=body.platform_url,
            sync_every=body.sync_every,
            lookback=body.lookback,
            batch_size=body.batch_size,
            rows_per_second=body.rows_per_second,
        )
        # 校验可加载(凭据在 env)
        dsn = build_odbc_dsn(
            server=body.erp_server,
            database=body.erp_database,
            user=body.erp_user,
            password=body.erp_password,
            port=body.erp_port,
        )
        save_secrets(home_layout.secrets_env, {
            "D2A_E10_DSN": dsn,
            "D2A_INGEST_TOKEN": body.ingest_token.strip(),
            "D2A_MIDDLE_ADMIN_TOKEN": body.admin_token.strip(),
        })
        apply_secrets_to_environ(home_layout.secrets_env)
        write_yaml(cfg_path, data)
        try:
            load_config(cfg_path)
        except Exception as e:
            cfg_path.unlink(missing_ok=True)
            return {"ok": False, "errors": [{"field": "", "message": str(e)}]}

        state["token"] = body.admin_token.strip()
        return {
            "ok": True,
            "restart_required": True,
            "message": "配置已写入。请用刚设置的管理界面登录密码登录;"
                       "抽取服务需另行启动或重启后生效。",
            "admin_token_hint": "已保存到 config/secrets.env",
        }

    @api.get("/config")
    def get_config() -> dict:
        if needs_setup():
            return {"needs_setup": True, "sources": {}}
        out = _config_subset(reload_config())
        out["needs_setup"] = False
        return out

    @api.post("/config")
    def post_config(body: ConfigPatch) -> dict:
        patch = _patch_to_dict(body)
        ok, errors = merge_whitelist_and_save(
            cfg_path, MIDDLE_EDITABLE, patch, validate=load_config)
        return {"ok": ok, "errors": errors, "restart_required": True}

    @api.post("/config/validate")
    def validate_config(body: ConfigPatch) -> dict:
        ok, errors = _validate_merged(cfg_path, _patch_to_dict(body))
        return {"ok": ok, "errors": errors}

    @api.get("/status")
    def get_status() -> dict:
        return build_status(reload_config())

    @api.get("/logs")
    def get_logs(service: str = "connector", lines: int = 200,
                 level: str | None = None) -> dict:
        capped = max(1, min(lines, 1000))
        if service not in _LOG_FILES:
            return {"ok": False,
                    "text": f"未知服务 '{service}',可用:{sorted(_LOG_FILES)}"}
        if _log_dir is not None:
            path = _log_dir / _LOG_FILES[service]
        elif service == "connector" and _log_path is not None:
            path = _log_path
        else:
            return {"ok": False, "text": "未配置日志目录"}
        ok, text = tail_lines(path, lines=capped, level=level)
        return {"ok": ok, "text": text}

    @api.post("/test-connection")
    def test_connection(body: TestConnectionBody = TestConnectionBody()) -> dict:
        cfg = reload_config()
        started = time.perf_counter()
        try:
            name, scfg = _resolve_source(cfg, body.source)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_probe_connection, name, scfg, cfg.landing)
                tables = future.result(timeout=10.0)
        except FuturesTimeoutError:
            return {"ok": False, "error": "timeout", "detail": "连接测试超过 10 秒"}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__,
                    "detail": _sanitize_detail(str(e))}
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {"ok": True, "elapsed_ms": elapsed_ms, "tables": tables}

    @api.post("/actions/trigger")
    def trigger_action(body: TriggerBody) -> dict:
        if body.action == "reconcile":
            raise HTTPException(400, "中间机管理 v1 不支持 reconcile")
        if body.action != "sync":
            raise HTTPException(400, f"不支持的动作 '{body.action}'")
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        executed = run_sync_cycle(name, scfg, cfg.landing)
        return {"action": "sync", "source": name, "executed": executed,
                "overlap_warning": True,
                "note": "" if executed else "错峰窗口外,未发起(窗口约束同样生效)"}

    app.include_router(api)
    # HTML 也走轻量检查:首次配置时放行
    @app.middleware("http")
    async def _html_gate(request: Request, call_next):
        if request.url.path.startswith("/api"):
            return await call_next(request)
        if needs_setup() and request.url.path not in (
            "/config", "/", "/status", "/logs"
        ) and not request.url.path.startswith("/static"):
            return RedirectResponse("/config")
        return await call_next(request)

    if _ADMIN_STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=_ADMIN_STATIC), name="static")
    return app
