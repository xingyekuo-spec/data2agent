"""中间机管理 FastAPI:配置 / 首次浏览器配置 / 状态 / 日志 / 调试。"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import json as _json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any
from urllib.request import Request as _UrlRequest, urlopen as _urlopen

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape
from pydantic import BaseModel, Field, ValidationError

from ...shared.admin.config_edit import MIDDLE_EDITABLE, merge_whitelist_and_save
from ...shared.admin.home_layout import HomeLayout
from ...shared.admin.logs import tail_lines
from ...shared.admin.secrets_file import apply_secrets_to_environ, save_secrets
from ...shared.admin.setup_yaml import (
    build_middle_connect_yaml,
    build_odbc_dsn,
    write_yaml,
)
from ...shared.admin.suggestions import (
    field_error,
    http_error,
    suggestion_for_check,
    suggestion_for_connection,
)
from ...shared.config import ConnectConfig, SourceConfig, config_revision, load_config
from ..extract import discoverers as _discoverers  # noqa: F401  — 注册 MetadataDiscoverer
from ..extract.metadata import (
    DEFAULT_SCAN_TABLE_LIMIT,
    MetadataDiscoveryUnsupported,
    MetadataError,
    ScanStore,
    TableSummary,
    build_discoverer,
    discoverer_default_schema,
    extraction_plan_keys,
    in_extraction_plan,
    is_odbc_timeout_message,
)
from ..extract.scheduler import check_sync_preflight, run_sync_cycle
from ..extract.sync_lock import SourceSyncLock
from ...shared.store.landing import LandingStore

from .extraction_tables import (
    parse_tables_payload,
    plan_diff,
    replace_source_tables,
    table_spec_to_dict,
    validate_table_plan,
)
from .status import build_status

_SCAN_STORE = ScanStore()
_TRIGGER_EXECUTOR = ThreadPoolExecutor(max_workers=1)
log = logging.getLogger("data2agent.middle.admin")

_PKG = Path(__file__).resolve().parent
_ADMIN_TEMPLATES = _PKG.parents[1] / "shared" / "admin_templates"
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
    revision: str | None = None


class TriggerBody(BaseModel):
    action: str
    source: str | None = None
    tables: list[str] | None = None  # 限定只同步这些表(失败表定向重试)


class SilenceBody(BaseModel):
    alert_key: str
    hours: int = 24


class TestConnectionBody(BaseModel):
    source: str | None = None


class MetadataScanBody(BaseModel):
    source: str | None = None


class KeyCheckBody(BaseModel):
    source: str | None = None
    schema_name: str = Field("dbo", alias="schema")
    table: str
    columns: list[str]
    timeout_seconds: float = 30

    model_config = {"populate_by_name": True}


class WatermarkCheckBody(BaseModel):
    source: str | None = None
    schema_name: str = Field("dbo", alias="schema")
    table: str
    column: str

    model_config = {"populate_by_name": True}


class ExtractionTablesValidateBody(BaseModel):
    """校验预览；live=False 仅做结构校验，不得用于保存。"""
    source: str | None = None
    tables: dict[str, Any]
    live: bool = True

    model_config = {"populate_by_name": True}


class ExtractionTablesPutBody(BaseModel):
    """保存抽取计划。现场校验为服务端固定规则，不接受客户端 live 开关。"""
    source: str | None = None
    tables: dict[str, Any]
    revision: str | None = None


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
    sync_start_at: str | None = None
    start_date: str | None = None
    lookback: str = "3d"
    batch_size: int = 5000
    rows_per_second: int = 2000


class ConnectionInfoBody(BaseModel):
    """日常更新平台对接信息:平台地址入 YAML,Token 只写 secrets.env(不回显)。"""
    platform_url: str | None = None
    ingest_token: str | None = None
    revision: str | None = None


def _request_worker_restart(home_layout) -> None:
    """通知便携启动器重载凭据。

    标记文件是可恢复的：启动器不在时保留，下次启动后消费。
    """
    if home_layout is None:
        return
    flag = home_layout.root / "data" / "restart-workers.flag"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()


def _http_get_json(url: str, token: str | None, timeout: float) -> dict:
    """中间机管理端最小 GET JSON(连通检查用;与 sink 同为 urllib,不引新依赖)。"""
    req = _UrlRequest(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with _urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


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
            "sync_start_at": scfg.sync_start_at,
            "start_date": scfg.start_date,
            "reconcile_at": scfg.reconcile_at,
            "reconcile_deep_at": scfg.reconcile_deep_at,
            "reconcile_deep_day_of_week": scfg.reconcile_deep_day_of_week,
            "tables": {
                tbl: {
                    "mode": spec.mode,
                    "watermark": spec.watermark,
                    "schema": spec.schema or "dbo",
                    "key_columns": spec.key_columns,
                    "start_date": spec.start_date,
                    "schema_fingerprint": spec.schema_fingerprint,
                    "validated_at": spec.validated_at,
                }
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
            raise http_error(
                404,
                f"配置中没有源 '{source}',可用:{sorted(cfg.sources)}",
                "在「配置」页确认 sources 名称，或先完成首次配置",
            )
        return source, scfg
    name = next(iter(cfg.sources))
    return name, cfg.sources[name]



def _meta_http(exc: MetadataError, *, table_lookup: bool = False) -> HTTPException:
    status = 400
    if exc.code in ("connection_failed", "timeout"):
        status = 503
    elif exc.code == "permission_denied":
        status = 403
    elif exc.code == "scan_busy":
        status = 409
    elif table_lookup and exc.code in ("table_missing", "not_found", "table_not_found"):
        status = 404
    return http_error(status, exc.message, exc.suggestion, code=exc.code)


def _unsupported_http(exc: MetadataDiscoveryUnsupported) -> HTTPException:
    return http_error(
        400, str(exc),
        getattr(exc, "suggestion", "确认 adapter 类型或升级中间机包"),
        code=exc.code,
    )


def _with_conn_suggestion(result: dict) -> dict:
    if result.get("status") in ("failed",) or result.get("error"):
        result = dict(result)
        result.setdefault(
            "suggestion",
            suggestion_for_connection(result.get("error")),
        )
    return result


def _sanitize_detail(message: str) -> str:
    if _DSN_PATTERN.search(message):
        return "连接失败(响应中已省略凭据/连接串细节)"
    return message[:500]


def _probe_connection_pure(dsn: str, timeout: int = 10) -> dict:
    """纯连接测试:直接建立 ODBC 连接并执行最小探测，不依赖抽取适配器。"""
    import pyodbc
    conn = None
    try:
        conn = pyodbc.connect(dsn, readonly=True, timeout=timeout)
        conn.timeout = timeout
        cur = conn.cursor()
        cur.execute("SELECT DB_NAME()")
        db_name = cur.fetchone()[0]
        try:
            cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
            cur.fetchone()
            has_metadata = True
        except Exception:
            has_metadata = False
        return {
            "status": "connected" if has_metadata else "connected_limited",
            "database": str(db_name) if db_name else None,
            "has_metadata_access": has_metadata,
        }
    except pyodbc.Error as e:
        msg = str(e)
        if "login" in msg.lower() or "password" in msg.lower():
            return {"status": "failed", "error": "auth", "detail": _sanitize_detail(msg)}
        if is_odbc_timeout_message(msg):
            return {"status": "failed", "error": "timeout", "detail": _sanitize_detail(msg)}
        return {"status": "failed", "error": type(e).__name__, "detail": _sanitize_detail(msg)}
    except Exception as e:
        return {"status": "failed", "error": type(e).__name__,
                "detail": _sanitize_detail(str(e))}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _probe_connection_with_timeout(dsn: str, timeout: float = 10) -> dict:
    """在不等待超时线程回收的前提下执行 ODBC 探测。"""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_probe_connection_pure, dsn, timeout=int(timeout))
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            future.cancel()
            return {"status": "failed", "error": "timeout",
                    "detail": f"连接测试超过 {timeout:g} 秒"}
        except Exception as e:
            return {"status": "failed", "error": type(e).__name__,
                    "detail": _sanitize_detail(str(e))}
    finally:
        # 不使用 with：超时离开上下文会 shutdown(wait=True)，使 HTTP 请求继续等待。
        # pyodbc 的连接和查询超时负责尽快终止后台 ODBC 调用。
        pool.shutdown(wait=False, cancel_futures=True)


def _validate_merged(path: Path, patch: dict[str, Any]) -> tuple[bool, list[dict[str, str]]]:
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy2(path, tmp_path)
    try:
        return merge_whitelist_and_save(tmp_path, MIDDLE_EDITABLE, patch, validate=load_config)
    except Exception as e:
        return False, [field_error(
            "", str(e),
            "根据报错修正配置字段后重新校验",
        )]
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
        # 仅当本 home 自带 secrets 时才灌入环境并取 Token,避免同进程残留污染。
        if home_layout.secrets_env.is_file():
            apply_secrets_to_environ(home_layout.secrets_env)
            if token is None:
                token = os.environ.get("D2A_MIDDLE_ADMIN_TOKEN") or None
    elif token is None:
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
    # revision 比较和文件替换须在同一临界区内，避免最后写入者静默覆盖。
    config_write_lock = threading.Lock()

    def needs_setup() -> bool:
        return not cfg_path.is_file()

    def auth(request: Request) -> None:
        path = request.url.path
        # 首次配置:仅本机可无 Token 访问 setup / 读状态
        if needs_setup():
            if path in ("/api/setup", "/api/setup/status") or path.startswith("/api/setup"):
                if _client_host(request) not in _LOOPBACK:
                    raise http_error(
                        403, "首次配置仅允许本机访问",
                        "在本机浏览器打开管理界面完成首次配置，勿从远程主机提交 /api/setup",
                    )
                return
            if path in ("/config", "/", "/status", "/logs") or path.startswith("/static"):
                return
            raise http_error(
                409, "尚未完成首次配置,请打开 /config",
                "在本机打开 /config 完成首次配置后再调用其它 API",
            )

        tok = state["token"]
        if not tok:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")
        if supplied != tok:
            raise http_error(
                401, "需要有效的管理界面登录密码",
                "在登录框输入管理界面登录密码，或检查 Authorization: Bearer / ?token=",
            )

    def reload_config() -> ConnectConfig:
        if needs_setup():
            raise http_error(
                409, "尚未完成首次配置",
                "打开 /config 完成首次配置后再操作",
            )
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

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "runs.html", page_ctx(request))

    @app.get("/errors", response_class=HTMLResponse)
    def errors_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "errors.html", page_ctx(request))

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "config.html", page_ctx(request))

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "logs.html", page_ctx(request))

    @app.get("/metadata", response_class=HTMLResponse)
    def metadata_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "metadata.html", page_ctx(request))

    @app.get("/tables", response_class=HTMLResponse)
    def tables_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "tables.html", page_ctx(request))

    @app.get("/push-logs", response_class=HTMLResponse)
    def push_logs_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "push-logs.html", page_ctx(request))

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
            raise http_error(
                403, "首次配置仅允许本机访问",
                "在本机浏览器打开管理界面完成首次配置",
            )
        if home_layout is None:
            raise http_error(
                400, "未启用 --home,无法浏览器首次配置",
                "以 --home <目录> 启动中间机管理进程后再用浏览器配置",
            )
        if not body.platform_url.startswith("http"):
            return {"ok": False, "errors": [field_error(
                "platform_url", "须为 http(s) URL",
                "填写平台 ingest 根地址，例如 https://platform.example.com",
            )]}
        if not body.admin_token.strip() or not body.ingest_token.strip():
            return {"ok": False, "errors": [field_error(
                "token", "Token 不能为空",
                "分别填写管理界面登录密码与平台 ingest Token",
            )]}

        home_layout.ensure_dirs()
        data = build_middle_connect_yaml(
            home_layout,
            platform_url=body.platform_url,
            sync_every=body.sync_every,
            sync_start_at=body.sync_start_at,
            start_date=(body.start_date or "").strip() or None,
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
            return {"ok": False, "errors": [field_error(
                "", str(e),
                "根据报错修正 ERP/平台参数后重试首次配置",
            )]}

        state["token"] = body.admin_token.strip()
        return {
            "ok": True,
            "restart_required": True,
            "restart_automatic": True,
            "message": "配置已写入。请用刚设置的管理界面登录密码登录;"
                       "便携启动器将自动启动抽取服务。",
            "admin_token_hint": "已保存到 config/secrets.env",
        }

    @api.get("/config")
    def get_config() -> dict:
        if needs_setup():
            return {"needs_setup": True, "sources": {}, "revision": None}
        out = _config_subset(reload_config())
        out["needs_setup"] = False
        out["revision"] = config_revision(cfg_path)
        return out

    @api.post("/config")
    def post_config(body: ConfigPatch) -> dict:
        patch = _patch_to_dict(body)
        with config_write_lock:
            if needs_setup():
                raise http_error(
                    409, "尚未完成首次配置",
                    "打开 /config 完成首次配置后再保存",
                )
            current = config_revision(cfg_path)
            if body.revision is None:
                raise http_error(
                    409,
                    "已有配置的更新必须提交 revision,请刷新页面获取最新修订号。",
                    "先 GET /api/config 取 revision，再随保存请求提交",
                    hint="GET /api/config 返回 revision 字段",
                    current_revision=current,
                )
            if body.revision != current:
                raise http_error(
                    409,
                    "配置已被其他会话修改,请刷新后重新提交。",
                    "重新加载配置页，合并改动后再保存",
                    current_revision=current,
                )
            try:
                ok, errors = merge_whitelist_and_save(
                    cfg_path, MIDDLE_EDITABLE, patch, validate=load_config)
            except Exception as e:
                return {"ok": False, "errors": [field_error(
                    "", str(e),
                    "根据报错修正配置字段后重试",
                )], "restart_required": False, "revision": current}
            revision = config_revision(cfg_path) if ok else current
            if ok:
                _request_worker_restart(home_layout)
        return {
            "ok": ok, "errors": errors, "restart_required": ok,
            "restart_automatic": bool(ok and home_layout is not None),
            "revision": revision,
        }

    @api.post("/config/validate")
    def validate_config(body: ConfigPatch) -> dict:
        ok, errors = _validate_merged(cfg_path, _patch_to_dict(body))
        return {"ok": ok, "errors": errors}

    @api.post("/config/connection")
    def post_connection_info(body: ConnectionInfoBody) -> dict:
        """日常更新平台对接信息:平台地址进 YAML,Token 只写 secrets.env。

        平台换发/重置 Token 后在这里更新,无需重做首次配置;重启抽取进程生效。
        """
        with config_write_lock:
            if needs_setup():
                raise http_error(
                    409, "尚未完成首次配置",
                    "打开 /config 完成首次配置后再更新对接信息",
                )
            current = config_revision(cfg_path)
            if body.revision is None or body.revision != current:
                raise http_error(
                    409,
                    "配置已被其他会话修改,请刷新后重新提交。",
                    "重新加载配置页后再更新对接信息",
                    current_revision=current,
                )
            cfg = reload_config()
            name, scfg = _resolve_source(cfg, None)
            if body.platform_url is not None:
                url = body.platform_url.strip()
                if not url.startswith("http"):
                    return {"ok": False, "errors": [field_error(
                        "platform_url", "须为 http(s) URL",
                        "填写平台 ingest 根地址,例如 https://platform.example.com",
                    )], "restart_required": False, "revision": current}
                ok, errors = merge_whitelist_and_save(
                    cfg_path, MIDDLE_EDITABLE,
                    {"sources": {name: {"sink": {"url": url}}}},
                    validate=load_config)
                if not ok:
                    return {"ok": False, "errors": errors,
                            "restart_required": False, "revision": current}
            token_updated = False
            if body.ingest_token is not None and body.ingest_token.strip():
                if home_layout is None:
                    raise http_error(
                        409, "未启用 --home,无法写 secrets.env",
                        "以 --home <目录> 启动中间机管理进程后再更新推送口令",
                    )
                env_name = scfg.sink.token_env or "D2A_INGEST_TOKEN"
                save_secrets(home_layout.secrets_env,
                             {env_name: body.ingest_token.strip()})
                apply_secrets_to_environ(home_layout.secrets_env)
                token_updated = True
            if body.platform_url is not None or token_updated:
                _request_worker_restart(home_layout)
            revision = config_revision(cfg_path)
        return {"ok": True, "errors": [], "restart_required": True,
                "restart_automatic": home_layout is not None,
                "revision": revision, "token_updated": token_updated}

    @api.get("/config/connection-check")
    def connection_check() -> dict:
        """平台连通 + 协议兼容检查:GET {sink.url}/ingest/health(不回显 Token)。"""
        if needs_setup():
            raise http_error(
                409, "尚未完成首次配置",
                "打开 /config 完成首次配置后再测试连通",
            )
        cfg = reload_config()
        _, scfg = _resolve_source(cfg, None)
        from ...protocol.ingest import INGEST_PROTOCOL_VERSION
        base: dict[str, Any] = {
            "platform_url": scfg.sink.url,
            "local_protocol": INGEST_PROTOCOL_VERSION,
            "sink_type": scfg.sink.type,
        }
        if scfg.sink.type != "http" or not scfg.sink.url:
            return {**base, "ok": False,
                    "detail": "local 落地模式,无平台接收端点(仅开发/参考链)"}
        env_name = scfg.sink.token_env or "D2A_INGEST_TOKEN"
        token = os.environ.get(env_name) or None
        try:
            health = _http_get_json(
                scfg.sink.url.rstrip("/") + "/ingest/health", token, timeout=8)
        except Exception as e:
            code = getattr(e, "code", None)
            if code == 401:
                detail = "平台拒绝(401):推送口令无效,请在平台重新签发后更新"
            elif code == 403:
                detail = "平台拒绝(403):数据源未登记或已停用,请先在平台数据源管理中登记"
            else:
                detail = f"连接失败:{_sanitize_detail(str(e))}"
            return {**base, "ok": False, "token_configured": token is not None,
                    "detail": detail}
        supported = health.get("supported_ingest_protocol_versions")
        if isinstance(supported, list) and supported:
            supported = [str(v) for v in supported]
        else:
            remote = (health.get("active_ingest_protocol_version")
                      or health.get("ingest_protocol_version"))
            supported = [str(remote)] if remote else []
        compatible = INGEST_PROTOCOL_VERSION in supported
        return {
            **base,
            "ok": True,
            "token_configured": token is not None,
            "platform_active": (health.get("active_ingest_protocol_version")
                                or health.get("ingest_protocol_version")),
            "platform_supported": supported,
            "compatible": compatible,
            "detail": ("兼容"
                       if compatible
                       else "平台不再接受本机协议版本,请升级中间机"),
        }

    @api.get("/extraction-tables")
    def get_extraction_tables(source: str | None = None) -> dict:
        if needs_setup():
            raise http_error(
                409, "尚未完成首次配置",
                "打开 /config 完成首次配置后再查看抽取表",
            )
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, source)
        tables = {
            tbl: table_spec_to_dict(spec)
            for tbl, spec in (scfg.tables or {}).items()
        }
        return {
            "source": name,
            "revision": config_revision(cfg_path),
            "tables": tables,
            "table_count": len(tables),
        }

    @api.post("/extraction-tables/validate")
    def validate_extraction_tables(body: ExtractionTablesValidateBody) -> dict:
        if needs_setup():
            raise http_error(
                409, "尚未完成首次配置",
                "打开 /config 完成首次配置后再校验抽取表",
            )
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        try:
            parsed = parse_tables_payload(body.tables)
        except (ValidationError, ValueError) as e:
            return {
                "ok": False, "source": name,
                "errors": [field_error(
                    "tables", str(e),
                    "按 TableExtractConfig 要求修正 mode/key_columns/watermark 后重试",
                )],
                "results": [], "diff": None,
            }
        before = dict(scfg.tables or {})
        results = validate_table_plan(scfg, parsed, live=body.live)
        ok = all(r["status"] == "ready" for r in results)
        return {
            "ok": ok,
            "source": name,
            "results": results,
            "diff": plan_diff(before, parsed),
        }

    @api.put("/extraction-tables")
    def put_extraction_tables(body: ExtractionTablesPutBody) -> dict:
        if needs_setup():
            raise http_error(
                409, "尚未完成首次配置",
                "打开 /config 完成首次配置后再保存抽取表",
            )
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        try:
            parsed = parse_tables_payload(body.tables)
        except (ValidationError, ValueError) as e:
            return {
                "ok": False, "errors": [field_error(
                    "tables", str(e),
                    "按 TableExtractConfig 要求修正后重试",
                )],
                "revision": config_revision(cfg_path), "restart_required": False,
            }
        # 保存必须现场校验；忽略客户端任何 live 开关意图
        results = validate_table_plan(scfg, parsed, live=True)
        not_ready = [r for r in results if r["status"] != "ready"]
        if not_ready:
            return {
                "ok": False,
                "errors": [field_error(
                    r["table"],
                    f"{r['status']}: {r.get('detail') or ''}",
                    r.get("suggestion") or "按校验结果修正该表后重新保存",
                ) for r in not_ready],
                "results": results,
                "revision": config_revision(cfg_path),
                "restart_required": False,
            }
        with config_write_lock:
            current = config_revision(cfg_path)
            if body.revision is None:
                raise http_error(
                    409, "更新抽取表必须提交 revision",
                    "重新加载抽取表页获取最新 revision 后再保存",
                    current_revision=current,
                )
            if body.revision != current:
                raise http_error(
                    409, "配置已被其他会话修改,请刷新后重新提交。",
                    "重新加载抽取计划，合并改动后再保存",
                    current_revision=current,
                )
            before = dict(scfg.tables or {})
            ok, errors, revision = replace_source_tables(
                cfg_path, name, parsed, validate=load_config,
                stamp_validated_at=True)
        return {
            "ok": ok,
            "errors": errors,
            "results": results,
            "diff": plan_diff(before, parsed) if ok else None,
            "revision": revision,
            "restart_required": ok,
            "message": ("抽取计划已保存;connector 从下一轮开始使用新配置"
                        if ok else None),
        }

    @api.get("/status")
    def get_status() -> dict:
        return build_status(reload_config())

    @api.get("/logs")
    def get_logs(service: str = "connector", lines: int = 200,
                 level: str | None = None) -> dict:
        capped = max(1, min(lines, 1000))
        if service not in _LOG_FILES:
            return {
                "ok": False,
                "text": f"未知服务 '{service}',可用:{sorted(_LOG_FILES)}",
                "suggestion": "选择 connector / admin / launcher 之一",
            }
        if _log_dir is not None:
            path = _log_dir / _LOG_FILES[service]
        elif service == "connector" and _log_path is not None:
            path = _log_path
        else:
            return {
                "ok": False,
                "text": "未配置日志目录",
                "suggestion": "以 --home 启动或传入日志路径后重启管理进程",
            }
        ok, text = tail_lines(path, lines=capped, level=level)
        out = {"ok": ok, "text": text}
        if not ok:
            out["suggestion"] = "确认日志文件存在且进程有读权限，或切换其它日志源"
        return out

    @api.post("/connection/test")
    def test_connection(body: TestConnectionBody = TestConnectionBody()) -> dict:
        """纯连接测试:只验证 DSN 可连、数据库可访问、元数据权限。不读取 tables。"""
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        if scfg.adapter != "mssql_readonly":
            return _with_conn_suggestion({
                "status": "failed", "error": "unsupported",
                "detail": "连接测试仅支持 mssql_readonly 适配器",
            })
        dsn = os.environ.get(scfg.dsn_env or "", "")
        if not dsn:
            return _with_conn_suggestion({
                "status": "failed", "error": "missing_dsn",
                "detail": f"环境变量 {scfg.dsn_env} 未设置",
            })
        started = time.perf_counter()
        result = _probe_connection_with_timeout(dsn, timeout=10)
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return _with_conn_suggestion(result)

    def _open_discoverer(scfg: SourceConfig):
        try:
            return build_discoverer(scfg)
        except MetadataDiscoveryUnsupported as e:
            raise _unsupported_http(e) from e
        except MetadataError as e:
            raise _meta_http(e) from e

    def _default_schema_for(scfg: SourceConfig) -> str:
        try:
            return discoverer_default_schema(scfg.adapter)
        except MetadataDiscoveryUnsupported as e:
            raise _unsupported_http(e) from e

    def _planned_keys(scfg: SourceConfig) -> set[tuple[str, str]]:
        return extraction_plan_keys(
            scfg.tables, default_schema=_default_schema_for(scfg))

    def _run_scan(scan_id: str, scfg: SourceConfig, deadline_mono: float) -> None:
        discoverer = None
        try:
            if time.monotonic() > deadline_mono:
                _SCAN_STORE.fail(
                    scan_id, "timeout", "扫描在开始前已超时", status="timeout",
                    suggestion="减小扫描范围或在业务低峰重试",
                )
                return
            discoverer = build_discoverer(scfg)
            tables, _total = discoverer.list_tables(
                limit=DEFAULT_SCAN_TABLE_LIMIT, offset=0)
            details = {}
            finalized: list[TableSummary] = []
            table_errors = 0
            timed_out = False
            for summary in tables:
                if time.monotonic() > deadline_mono:
                    timed_out = True
                    break
                try:
                    detail = discoverer.get_table(summary.schema, summary.name)
                    details[(summary.schema, summary.name)] = detail
                    finalized.append(TableSummary(
                        schema=detail.schema,
                        name=detail.name,
                        object_type=detail.object_type,
                        estimated_rows=detail.estimated_rows,
                        primary_key=detail.primary_key,
                        unique_keys=detail.unique_keys,
                        watermark_candidates=detail.watermark_candidates,
                    ))
                except MetadataError as e:
                    table_errors += 1
                    # 连接级 / 权限级错误:整次扫描失败,不伪装 completed
                    if e.code in ("connection_failed", "permission_denied", "timeout"):
                        _SCAN_STORE.fail(
                            scan_id, e.code, e.message, suggestion=e.suggestion)
                        return
                    finalized.append(TableSummary(
                        schema=summary.schema,
                        name=summary.name,
                        object_type=summary.object_type,
                        estimated_rows=None,
                        primary_key=(),
                        unique_keys=(),
                        watermark_candidates=(),
                        error_code=e.code,
                        error_detail=e.message,
                        error_suggestion=e.suggestion,
                    ))
            if timed_out:
                if finalized or details:
                    _SCAN_STORE.complete(
                        scan_id, finalized, details,
                        status="partial",
                        table_errors=table_errors,
                        error_code="timeout",
                        error_detail="扫描超过总时限,结果可能不完整",
                        error_suggestion="减小扫描范围或在业务低峰重试；已返回部分结果可继续使用",
                    )
                else:
                    _SCAN_STORE.fail(
                        scan_id, "timeout", "扫描超过总时限", status="timeout",
                        suggestion="减小扫描范围或在业务低峰重试",
                    )
                return
            status = "partial" if table_errors else "completed"
            _SCAN_STORE.complete(
                scan_id, finalized, details,
                status=status,
                table_errors=table_errors,
                error_code="table_errors" if table_errors else None,
                error_detail=(
                    f"{table_errors} 张表元数据读取失败" if table_errors else None
                ),
            )
        except MetadataDiscoveryUnsupported as e:
            log.warning("metadata scan %s unsupported: %s", scan_id, e)
            _SCAN_STORE.fail(
                scan_id, e.code, str(e),
                suggestion=getattr(e, "suggestion", None),
            )
        except MetadataError as e:
            log.warning("metadata scan %s failed: code=%s detail=%s",
                        scan_id, e.code, e.message)
            status = "timeout" if e.code == "timeout" else "failed"
            _SCAN_STORE.fail(
                scan_id, e.code, e.message, status=status, suggestion=e.suggestion)
        except Exception as e:
            log.error("metadata scan %s error: %s",
                      scan_id, _sanitize_detail(str(e)), exc_info=True)
            _SCAN_STORE.fail(
                scan_id, type(e).__name__, _sanitize_detail(str(e)),
                suggestion="查看管理界面日志中的脱敏错误后重试",
            )
        finally:
            if discoverer is not None:
                try:
                    discoverer.close()
                except Exception:
                    pass

    @api.post("/metadata/scans")
    def start_metadata_scan(body: MetadataScanBody = MetadataScanBody()) -> dict:
        """启动元数据扫描;不要求 tables 已配置。受活动槽位与总时限约束。"""
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        try:
            rec = _SCAN_STORE.try_begin(name)
        except MetadataError as e:
            raise _meta_http(e) from e
        deadline = time.monotonic() + _SCAN_STORE.scan_deadline_seconds
        _SCAN_STORE.submit(_run_scan, rec.scan_id, scfg, deadline)
        return {
            "scan_id": rec.scan_id,
            "source": name,
            "status": rec.status,
            "deadline_seconds": _SCAN_STORE.scan_deadline_seconds,
        }

    @api.get("/metadata/scans/{scan_id}")
    def get_metadata_scan(scan_id: str) -> dict:
        rec = _SCAN_STORE.get(scan_id)
        if rec is None:
            raise http_error(
                404, "scan_id 不存在或已过期",
                "重新发起扫描，或使用最近一次扫描返回的 scan_id",
            )
        out = rec.summary()
        if rec.status in ("completed", "partial"):
            out["tables"] = [
                {
                    "schema": t.schema,
                    "name": t.name,
                    "object_type": t.object_type,
                    "estimated_rows": t.estimated_rows,
                    "primary_key": list(t.primary_key),
                    "unique_keys": [
                        {"name": k.name, "columns": list(k.columns), "kind": k.kind}
                        for k in t.unique_keys
                    ],
                    "watermark_candidates": list(t.watermark_candidates),
                    "error_code": t.error_code,
                    "error_detail": t.error_detail,
                    "error_suggestion": t.error_suggestion,
                    "suggestion": t.error_suggestion,
                }
                for t in rec.tables
            ]
        return out

    @api.get("/metadata/tables")
    def list_metadata_tables(
        source: str | None = None,
        schema: str | None = None,
        q: str | None = None,
        object_type: str | None = None,
        has_pk: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, source)
        rec = _SCAN_STORE.latest_completed_for_source(name)
        if rec is None:
            raise http_error(
                409,
                "尚无可用元数据缓存,请先 POST /api/metadata/scans",
                "在「元数据」页点击扫描，完成后再浏览表列表",
                code="metadata_stale",
            )
        default_schema = _default_schema_for(scfg)
        planned = _planned_keys(scfg)
        rows = []
        for t in rec.tables:
            if schema and t.schema != schema:
                continue
            if object_type and t.object_type != object_type:
                continue
            if q and q.casefold() not in t.name.casefold():
                continue
            if has_pk is True and not t.primary_key:
                continue
            if has_pk is False and t.primary_key:
                continue
            detail = rec.details.get((t.schema, t.name)) if rec.details else None
            rows.append({
                "schema": t.schema,
                "name": t.name,
                "object_type": t.object_type,
                "estimated_rows": t.estimated_rows,
                "primary_key": list(t.primary_key),
                "unique_keys": [
                    {"name": k.name, "columns": list(k.columns), "kind": k.kind}
                    for k in t.unique_keys
                ],
                "watermark_candidates": list(t.watermark_candidates),
                "schema_fingerprint": (
                    detail.schema_fingerprint if detail is not None else None),
                "in_extraction_plan": in_extraction_plan(
                    t.schema, t.name, planned, default_schema=default_schema),
                "error_code": t.error_code,
                "error_detail": t.error_detail,
                "error_suggestion": t.error_suggestion,
                "suggestion": t.error_suggestion,
            })
        total = len(rows)
        page = rows[offset:offset + max(1, min(limit, 500))]
        return {
            "source": name,
            "scan_id": rec.scan_id,
            "scan_status": rec.status,
            "total": total,
            "offset": offset,
            "limit": limit,
            "tables": page,
        }

    @api.get("/metadata/tables/{schema}/{table}")
    def get_metadata_table(schema: str, table: str, source: str | None = None) -> dict:
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, source)
        rec = _SCAN_STORE.latest_completed_for_source(name)
        detail = rec.details.get((schema, table)) if rec else None
        if detail is None:
            discoverer = _open_discoverer(scfg)
            try:
                detail = discoverer.get_table(schema, table)
            except MetadataError as e:
                raise _meta_http(e, table_lookup=True) from e
            finally:
                discoverer.close()
        default_schema = _default_schema_for(scfg)
        planned = _planned_keys(scfg)
        return {
            "source": name,
            "schema": detail.schema,
            "name": detail.name,
            "object_type": detail.object_type,
            "columns": [
                {"name": c.name, "ordinal": c.ordinal,
                 "sql_type": c.sql_type, "nullable": c.nullable}
                for c in detail.columns
            ],
            "primary_key": list(detail.primary_key),
            "unique_keys": [
                {"name": k.name, "columns": list(k.columns), "kind": k.kind}
                for k in detail.unique_keys
            ],
            "foreign_keys": [
                {
                    "name": f.name,
                    "columns": list(f.columns),
                    "referenced_schema": f.referenced_schema,
                    "referenced_table": f.referenced_table,
                    "referenced_columns": list(f.referenced_columns),
                }
                for f in detail.foreign_keys
            ],
            "estimated_rows": detail.estimated_rows,
            "watermark_candidates": list(detail.watermark_candidates),
            "schema_fingerprint": detail.schema_fingerprint,
            "scanned_at": detail.scanned_at,
            "in_extraction_plan": in_extraction_plan(
                detail.schema, detail.name, planned, default_schema=default_schema),
            "key_suggestions": [
                {"source": "primary_key", "columns": list(detail.primary_key)}
            ] + [
                {"source": k.kind, "columns": list(k.columns), "name": k.name}
                for k in detail.unique_keys if k.kind != "primary"
            ],
        }

    @api.post("/metadata/key-check")
    def metadata_key_check(body: KeyCheckBody) -> dict:
        cfg = reload_config()
        _name, scfg = _resolve_source(cfg, body.source)
        discoverer = _open_discoverer(scfg)
        try:
            result = discoverer.check_key(
                body.schema_name, body.table, body.columns,
                timeout_seconds=body.timeout_seconds,
            )
        finally:
            discoverer.close()
        return {
            "ok": result.ok,
            "code": result.code,
            "detail": result.detail,
            "null_count": result.null_count,
            "duplicate_groups": result.duplicate_groups,
            "suggestion": None if result.ok else suggestion_for_check(result.code),
        }

    @api.post("/metadata/watermark-check")
    def metadata_watermark_check(body: WatermarkCheckBody) -> dict:
        cfg = reload_config()
        _name, scfg = _resolve_source(cfg, body.source)
        discoverer = _open_discoverer(scfg)
        try:
            result = discoverer.check_watermark(
                body.schema_name, body.table, body.column)
        finally:
            discoverer.close()
        return {
            "ok": result.ok,
            "code": result.code,
            "detail": result.detail,
            "sql_type": result.sql_type,
            "candidate": result.candidate,
            "suggestion": None if result.ok else suggestion_for_check(result.code),
        }

    @api.post("/actions/trigger")
    def trigger_action(body: TriggerBody) -> dict:
        if body.action == "reconcile":
            raise http_error(
                400, "中间机管理 v1 不支持 reconcile",
                "请使用 sync 动作,或在平台侧处理对账流程",
            )
        if body.action != "sync":
            raise http_error(
                400, f"不支持的动作 '{body.action}'",
                "仅支持 action=sync",
            )
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        tables = _validate_trigger_tables(scfg, body.tables)
        return _start_sync_run(cfg, name, scfg, tables)

    @api.post("/runs/{run_id}/retry-failed")
    def retry_failed_tables(run_id: int) -> dict:
        """定向重试某次运行中失败的表(其余表不重跑)。"""
        cfg = reload_config()
        db = LandingStore(cfg.landing)
        try:
            run = db.con.execute(
                "SELECT * FROM d2a_sync_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise http_error(404, f"运行 #{run_id} 不存在", "确认运行 ID")
            failed = [
                s["target"] for s in db.steps_for_run(run_id)
                if s["kind"] == "table" and s["status"] == "failed"
            ]
            source = run["source"]
        finally:
            db.con.close()
        if not failed:
            raise http_error(
                409, f"运行 #{run_id} 没有失败的表步骤",
                "仅含失败表的运行可定向重试;否则请整体触发同步",
            )
        name, scfg = _resolve_source(cfg, source)
        return _start_sync_run(cfg, name, scfg, failed)

    def _validate_trigger_tables(scfg, tables: list[str] | None) -> list[str] | None:
        """校验 tables 在抽取计划内;None/空 = 全部。"""
        if not tables:
            return None
        configured = set(scfg.tables.keys()) if hasattr(scfg, "tables") else set()
        unknown = [t for t in tables if t not in configured]
        if unknown:
            raise http_error(
                422, f"表不在抽取计划内: {', '.join(unknown)}",
                "仅可指定 connect.yaml tables 中已配置的表",
            )
        return list(dict.fromkeys(tables))

    def _start_sync_run(cfg, name, scfg, tables: list[str] | None) -> dict:

        # 1. 预检:窗口外 / tables 为空立即返回,不创建 run
        preflight = check_sync_preflight(name, scfg)
        if not preflight.executed:
            if preflight.reason == "tables_unconfigured":
                return {
                    "action": "sync", "source": name, "executed": False,
                    "reason": preflight.reason, "overlap_warning": True,
                    "note": preflight.note,
                    "suggestion": preflight.suggestion,
                }
            return {
                "action": "sync", "source": name, "executed": False,
                "reason": preflight.reason,
                "note": preflight.note,
                "suggestion": preflight.suggestion,
            }

        # 2. 跨进程锁:已运行时不排队
        lock = SourceSyncLock.try_acquire(cfg.landing, name)
        if lock is None:
            existing = SourceSyncLock.find_running_run(cfg.landing, name)
            return {
                "action": "sync", "source": name, "executed": False,
                "reason": "already_running", "run_id": existing,
                "note": "已有同步正在运行" if existing else "已有同步正在启动中",
                "suggestion": "等待当前运行完成后再触发",
            }

        # 3. 建 run(锁已持有)→ 后台执行
        landing = LandingStore(cfg.landing)
        try:
            run_id = landing.start_run(name, "sync")
        except Exception as exc:
            landing.con.close()
            lock.release()
            raise http_error(
                500, f"无法创建运行记录: {_sanitize_detail(str(exc))}",
                "重试或检查落地库权限",
            )

        try:
            _TRIGGER_EXECUTOR.submit(
                _run_sync_worker, run_id, name, scfg, cfg.landing, cfg.templates,
                lock, tables)
        except Exception as exc:
            # 线程池无法提交：关闭遗留 run 并释放锁，避免永久 blocking
            try:
                landing.finish_running_run(
                    run_id, status="failed", detail=_sanitize_detail(str(exc)))
            finally:
                landing.con.close()
            lock.release()
            raise http_error(
                500, f"无法提交后台同步任务: {_sanitize_detail(str(exc))}",
                "重启中间机管理进程后重试",
            )

        landing.con.close()
        return {
            "action": "sync", "source": name, "run_id": run_id,
            "executed": True, "status": "started",
            "note": f"同步已后台启动, 运行 ID #{run_id}",
        }

    # ---- 告警静默 ----

    @api.get("/alerts/silences")
    def alert_silences() -> dict:
        cfg = reload_config()
        db = LandingStore(cfg.landing)
        try:
            return {"silences": db.list_alert_silences()}
        finally:
            db.con.close()

    @api.post("/alerts/silences")
    def alert_silence_create(body: SilenceBody) -> dict:
        if not (1 <= body.hours <= 24 * 30):
            raise http_error(422, "hours 须为 1..720", "调整静默时长")
        cfg = reload_config()
        db = LandingStore(cfg.landing)
        try:
            until = db.silence_alert(body.alert_key, hours=body.hours)
            return {"alert_key": body.alert_key, "silenced_until": until}
        finally:
            db.con.close()

    @api.delete("/alerts/silences/{alert_key:path}")
    def alert_silence_delete(alert_key: str) -> dict:
        cfg = reload_config()
        db = LandingStore(cfg.landing)
        try:
            deleted = db.delete_alert_silence(alert_key)
            return {"alert_key": alert_key, "deleted": deleted}
        finally:
            db.con.close()

    # ---- 运行查询 ----

    def _map_run_row(r) -> dict:
        return {
            "id": r["id"], "source": r["source"], "status": r["status"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "tables": r["tables"], "rows": r["rows"],
            "detail": r["detail"],
        }

    def _map_step_row(s) -> dict:
        return {
            "id": s["id"], "ordinal": s["ordinal"], "kind": s["kind"],
            "target": s["target"], "status": s["status"],
            "started_at": s["started_at"], "finished_at": s["finished_at"],
            "batch_id": s["batch_id"],
            "rows_in": s["rows_in"], "rows_out": s["rows_out"],
            "batches": s["batches"], "progressed_at": s["progressed_at"],
            "expected_rows": s["expected_rows"],
            "watermark_before": s["watermark_before"],
            "watermark_after": s["watermark_after"],
            "error": s["error"],
        }

    @api.get("/runs")
    def runs(source: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        if not (1 <= limit <= 50):
            raise http_error(422, "limit 须为 1..50", "调整分页参数")
        if offset < 0:
            raise http_error(422, "offset 须 >= 0", "调整分页参数")
        cfg = reload_config()
        if source is not None:
            name, _scfg = _resolve_source(cfg, source)
        else:
            name = None
        db = LandingStore(cfg.landing)
        try:
            where = "WHERE run_type = 'sync'"
            params: list[Any] = []
            if name is not None:
                where += " AND source = ?"
                params.append(name)
            (total,) = db.con.execute(
                f"SELECT COUNT(*) FROM d2a_sync_run {where}", params).fetchone()
            rows = db.con.execute(
                f"SELECT id, source, status, started_at, finished_at, tables, rows, detail "
                f"FROM d2a_sync_run {where} "
                f"ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset])
            return {
                "runs": [dict(r) for r in rows],
                "total": total, "limit": limit, "offset": offset,
            }
        finally:
            db.con.close()

    @api.get("/runs/{run_id}")
    def run_detail(run_id: int) -> dict:
        cfg = reload_config()
        db = LandingStore(cfg.landing)
        try:
            r = db.con.execute(
                "SELECT * FROM d2a_sync_run WHERE id = ?", (run_id,)).fetchone()
            if r is None:
                raise http_error(404, f"运行 #{run_id} 不存在", "确认运行 ID")
            steps = db.steps_for_run(run_id)
            return {
                "run": _map_run_row(r),
                "steps": [_map_step_row(s) for s in steps],
            }
        finally:
            db.con.close()

    # ---- 推送记录 ----

    def _map_push_log_row(r) -> dict:
        return {
            "id": r["id"], "source": r["source"], "run_id": r["run_id"],
            "step_kind": r["step_kind"], "table_name": r["table_name"],
            "mode": r["mode"], "batch_id": r["batch_id"],
            "rows_count": r["rows_count"], "status": r["status"],
            "error_detail": r["error_detail"], "retry_count": r["retry_count"],
            "duration_ms": r["duration_ms"], "created_at": r["created_at"],
        }

    @api.get("/push-logs")
    def push_logs(source: str | None = None, table: str | None = None,
                  limit: int = 50, offset: int = 0) -> dict:
        if not (1 <= limit <= 100):
            raise http_error(422, "limit 须为 1..100", "调整分页参数")
        if offset < 0:
            raise http_error(422, "offset 须 >= 0", "调整分页参数")
        cfg = reload_config()
        if source is not None:
            _name, _scfg = _resolve_source(cfg, source)
        else:
            source = None
        db = LandingStore(cfg.landing)
        try:
            rows, total = db.list_push_logs(
                source=source, table=table, limit=limit, offset=offset)
            return {
                "push_logs": [_map_push_log_row(r) for r in rows],
                "total": total, "limit": limit, "offset": offset,
            }
        finally:
            db.con.close()

    @api.get("/push-logs/by-table")
    def push_logs_by_table(source: str | None = None) -> dict:
        """按表汇总推送状态:每表最近批次是否推送完成。"""
        cfg = reload_config()
        if source is not None:
            _name, _scfg = _resolve_source(cfg, source)
        else:
            source = None
        db = LandingStore(cfg.landing)
        try:
            return {"tables": db.push_log_table_summaries(source=source)}
        finally:
            db.con.close()

    @api.get("/push-logs/batch/{batch_id}")
    def push_log_batch_detail(batch_id: str) -> dict:
        cfg = reload_config()
        db = LandingStore(cfg.landing)
        try:
            rows = db.con.execute(
                "SELECT * FROM d2a_http_push_log WHERE batch_id = ? "
                "ORDER BY created_at, id", (batch_id,)).fetchall()
            if not rows:
                raise http_error(404, f"批次 {batch_id} 无推送记录", "确认 batch_id")
            # 利用第一条记录算进度
            first = rows[0]
            progress = db.push_log_batch_progress(
                first["source"], first["table_name"], batch_id)
            return {
                "batch_id": batch_id,
                "source": first["source"],
                "table_name": first["table_name"],
                "mode": first["mode"],
                "steps": [_map_push_log_row(r) for r in rows],
                "progress": progress,
            }
        finally:
            db.con.close()

    # ---- 内部 ----

    def _run_sync_worker(
        run_id: int, name: str, scfg, landing_path: str, templates: str, lock,
        tables: list[str] | None = None,
    ) -> None:
        try:
            run_sync_cycle(name, scfg, landing_path, templates,
                           run_id=run_id, acquired_lock=lock, tables=tables)
        except Exception as exc:
            failed_store = LandingStore(landing_path)
            try:
                failed_store.finish_running_run(
                    run_id, status="failed", detail=_sanitize_detail(str(exc)))
            finally:
                failed_store.con.close()
        finally:
            lock.release()

    app.include_router(api)
    # HTML 也走轻量检查:首次配置时放行
    @app.middleware("http")
    async def _html_gate(request: Request, call_next):
        if request.url.path.startswith("/api"):
            return await call_next(request)
        if needs_setup() and request.url.path not in (
            "/config", "/", "/status", "/logs", "/metadata", "/tables"
        ) and not request.url.path.startswith("/static"):
            return RedirectResponse("/config")
        return await call_next(request)

    if _ADMIN_STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=_ADMIN_STATIC), name="static")
    return app
