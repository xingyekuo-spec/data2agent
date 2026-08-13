"""中间机管理 FastAPI:配置 / 首次浏览器配置 / 状态 / 日志 / 调试。"""

from __future__ import annotations

import logging
import os
import re
import shutil
import ssl
import tempfile
import threading
import time
import json as _json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request as _UrlRequest, urlopen as _urlopen

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PrefixLoader, select_autoescape
from pydantic import BaseModel, Field, ValidationError
import yaml

from ... import __version__
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
from ..extract.scheduler import (
    check_sync_preflight,
    run_reconcile_cycle,
    run_sync_cycle,
)
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
# source 锁负责同源互斥；允许不同 source 并行，避免一个长任务让其它源
# 的已创建 run 长时间排队并占锁。上限固定，防止管理端制造线程风暴。
_TRIGGER_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="d2a-middle-action")
_PROBE_STATE_LOCK = threading.Lock()
log = logging.getLogger("data2agent.middle.admin")

_PKG = Path(__file__).resolve().parent
_ADMIN_TEMPLATES = _PKG.parents[1] / "shared" / "admin_templates"
_MIDDLE_TEMPLATES = _PKG / "templates"
_ADMIN_STATIC = _ADMIN_TEMPLATES / "static"
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _record_readiness_probe(
    cfg: ConnectConfig, source: str, kind: str, result: dict[str, Any],
) -> None:
    """原子记录脱敏连通探测，供 readiness 聚合；不写 DSN/Token/URL。"""
    path = Path(cfg.state_db).parent / "run" / "readiness-probes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PROBE_STATE_LOCK:
        try:
            state = _json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError, TypeError):
            state = {}
        sources = state.setdefault("sources", {})
        source_state = sources.setdefault(source, {})
        source_state[kind] = {
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            **result,
        }
        fd, temp_name = tempfile.mkstemp(
            prefix=".readiness-probes-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                _json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temp_name).unlink(missing_ok=True)
            raise


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
    state_db: str | None = None
    # 旧客户端兼容；保存时转换为 state_db，响应不再使用 landing。
    landing: str | None = None
    sources: dict[str, Any] | None = None
    revision: str | None = None


class TriggerBody(BaseModel):
    action: str
    source: str | None = None
    tables: list[str] | None = None  # 限定只同步这些表(失败表定向重试)


class SilenceBody(BaseModel):
    alert_key: str
    hours: int = Field(24, ge=1, le=168)


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
    source: str | None = None
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


def _http_get_json(
    url: str, token: str | None, timeout: float, ca_bundle: str | None = None,
) -> dict:
    """中间机管理端最小 GET JSON(连通检查用;与 sink 同为 urllib,不引新依赖)。"""
    req = _UrlRequest(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None
    with _urlopen(req, timeout=timeout, context=context) as resp:
        return _json.loads(resp.read().decode("utf-8"))


_DSN_PATTERN = re.compile(
    r"(?i)(password\s*[=:]|pwd\s*[=:]|token\s*[=:]|bearer\s+|"
    r"authorization\s*[:=]|server\s*=|data\s+source\s*=|"
    r"uid\s*=|user\s+id\s*=|integrated\s+security|dsn\s*=|"
    r"connection\s+string|connect\s+string|"
    r"file:[^\s?]+|\.sqlite\b|\.db\b|"
    r"\b(select|insert|update|delete|create|alter|drop|truncate|"
    r"exec|execute|merge|grant|revoke)\b)"
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
    out: dict[str, Any] = {
        "templates": cfg.templates,
        "deployment_mode": cfg.deployment_mode,
        "state_db": cfg.state_db,
        "state_db_kind": "middle_state",
        "sources": {},
    }
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
            "sink": {
                "type": scfg.sink.type,
                "url": scfg.sink.url,
                "token_env": scfg.sink.token_env,
                "token_env_set": _env_set(scfg.sink.token_env),
                "timeout_seconds": scfg.sink.timeout_seconds,
                "retries": scfg.sink.retries,
                "ca_bundle": scfg.sink.ca_bundle,
                "ca_bundle_configured": bool(scfg.sink.ca_bundle),
                "allow_insecure_http": scfg.sink.allow_insecure_http,
            },
            "spool": {
                "policy": scfg.spool.policy,
                "directory": scfg.spool.directory,
                "encrypted_at_rest": scfg.spool.encrypted_at_rest,
            },
            "dsn_env": scfg.dsn_env,
            "dsn_env_set": _env_set(scfg.dsn_env),
        }
        out["sources"][name] = src
    return out


def _patch_to_dict(body: ConfigPatch) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    legacy = patch.pop("landing", None)
    if legacy is not None and "state_db" not in patch:
        patch["state_db"] = legacy
    return patch


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
        return "执行失败(响应中已省略凭据/连接串/Token/SQL 细节)"
    return message[:500]


def _error_category(message: str | None) -> str:
    """服务端稳定错误分类；页面不得再按中英文文案正则自行猜测。"""
    text = (message or "").lower()
    if any(token in text for token in ("401", "403", "auth", "权限", "denied", "token")):
        return "auth"
    if any(token in text for token in ("timeout", "timed out", "超时")):
        return "timeout"
    if any(token in text for token in ("connect", "network", "unreachable", "连接", "网络", "odbc")):
        return "network"
    if any(token in text for token in ("schema", "column", "watermark", "duplicate", "结构", "水位", "唯一")):
        return "schema"
    if any(token in text for token in ("generation", "lease", "屏障", "租约")):
        return "generation"
    return "runtime"


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
    # merge 会生成同目录备份；专用临时目录可确保校验请求不在系统
    # 临时目录长期遗留 .bak 文件。
    with tempfile.TemporaryDirectory(prefix="d2a-config-validate-") as temp_dir:
        tmp_path = Path(temp_dir) / "connect.yaml"
        shutil.copy2(path, tmp_path)
        try:
            return merge_whitelist_and_save(
                tmp_path, MIDDLE_EDITABLE, patch, validate=load_config)
        except Exception as e:
            return False, [field_error(
                "", str(e),
                "根据报错修正配置字段后重新校验",
            )]


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
        "maintenance": "d2a-maintenance.log",  # 状态库备份 / 清理
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

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # 页面脚本均为本地固定版本静态资源；样式仍含模板内联 CSS，故仅
        # style-src 暂时保留 unsafe-inline，script/object/frame/connect 从严。
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; object-src 'none'; "
            "frame-src 'none'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/static/"):
            # 无 Cache-Control 时浏览器启发式缓存会让旧 JS 长期"新鲜",
            # 升级后出现 新页面脚本 + 旧缓存 admin.js 的错配(如 runAction
            # is not defined)。no-cache 配合 ETag/Last-Modified 每次再验证,
            # 未变化时仅 304 开销;跨版本再由模板 ?v= 指纹强制失效。
            response.headers["Cache-Control"] = "no-cache"
        return response

    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])
    templates = _make_templates()

    def page_ctx(request: Request) -> dict[str, Any]:
        return {
            "static_url": "/static",
            # 静态脚本 URL 指纹:版本升级后 ?v= 变化,浏览器缓存自动失效
            "static_ver": __version__,
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

    @app.get("/recovery", response_class=HTMLResponse)
    def recovery_page(request: Request) -> HTMLResponse:
        """只提供说明，不暴露在线覆盖状态库的危险操作。"""
        return templates.TemplateResponse(request, "recovery.html", page_ctx(request))

    @api.get("/setup/status")
    def setup_status() -> dict:
        return {
            "needs_setup": needs_setup(),
            "config_path": str(cfg_path),
            "home": str(home_layout.root) if home_layout else None,
        }

    @api.get("/auth/check")
    def auth_check() -> dict:
        """登录框使用的轻量校验；通过 Depends(auth) 即表示 Token 有效。"""
        return {"ok": True, "authenticated": True}

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
        try:
            raw_config = yaml.safe_load(
                cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError, TypeError):
            raw_config = {}
        out["legacy_landing_key"] = (
            "landing" in raw_config and "state_db" not in raw_config)
        secrets_path = home_layout.secrets_env if home_layout is not None else None
        secrets_updated_at = None
        if secrets_path is not None and secrets_path.is_file():
            try:
                secrets_updated_at = datetime.fromtimestamp(
                    secrets_path.stat().st_mtime).astimezone().isoformat(
                        timespec="seconds")
            except OSError:
                secrets_updated_at = None
        out["sensitive_config"] = {
            "secrets_file_configured": bool(
                secrets_path is not None and secrets_path.is_file()),
            "updated_at": secrets_updated_at,
        }
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
            name, scfg = _resolve_source(cfg, body.source)
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
    def connection_check(source: str | None = None) -> dict:
        """平台连通 + 协议兼容检查:GET {sink.url}/ingest/health(不回显 Token)。"""
        if needs_setup():
            raise http_error(
                409, "尚未完成首次配置",
                "打开 /config 完成首次配置后再测试连通",
            )
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, source)
        from ...protocol.ingest import INGEST_PROTOCOL_VERSION
        base: dict[str, Any] = {
            "source": name,
            "platform_url": scfg.sink.url,
            "local_protocol": INGEST_PROTOCOL_VERSION,
            "sink_type": scfg.sink.type,
        }
        def finish(result: dict[str, Any]) -> dict[str, Any]:
            _record_readiness_probe(cfg, name, "platform", {
                "ok": bool(result.get("ok")),
                "compatible": bool(result.get("compatible")),
                "error_code": result.get("error_code"),
                "local_protocol": result.get("local_protocol"),
                "platform_supported": result.get("platform_supported") or [],
            })
            return result
        if scfg.sink.type != "http" or not scfg.sink.url:
            return finish({
                **base, "ok": False, "compatible": False,
                "error_code": "sink_not_http",
                "detail": "local 落地模式,无平台接收端点(仅开发/参考链)",
            })
        env_name = scfg.sink.token_env or "D2A_INGEST_TOKEN"
        token = os.environ.get(env_name) or None
        try:
            health = _http_get_json(
                scfg.sink.url.rstrip("/") + "/ingest/health", token,
                timeout=min(30, scfg.sink.timeout_seconds),
                ca_bundle=scfg.sink.ca_bundle)
        except Exception as e:
            code = getattr(e, "code", None)
            if code == 401:
                detail = "平台拒绝(401):推送口令无效,请在平台重新签发后更新"
            elif code == 403:
                detail = "平台拒绝(403):数据源未登记或已停用,请先在平台数据源管理中登记"
            else:
                detail = f"连接失败:{_sanitize_detail(str(e))}"
            return finish({
                **base, "ok": False, "compatible": False,
                "token_configured": token is not None,
                "error_code": (
                    "auth" if code in (401, 403) else "network"),
                "detail": detail,
            })
        supported = health.get("supported_ingest_protocol_versions")
        if isinstance(supported, list) and supported:
            supported = [str(v) for v in supported]
        else:
            remote = (health.get("active_ingest_protocol_version")
                      or health.get("ingest_protocol_version"))
            supported = [str(remote)] if remote else []
        compatible = INGEST_PROTOCOL_VERSION in supported
        return finish({
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
            "error_code": None if compatible else "protocol_incompatible",
        })

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
        return build_status(reload_config(), config_path=cfg_path)

    @api.get("/readiness")
    def get_readiness() -> dict:
        status = build_status(reload_config(), config_path=cfg_path)
        return {
            "observed_at": status["observed_at"],
            **status["readiness"],
        }

    @api.get("/logs")
    def get_logs(service: str = "connector", lines: int = 200,
                 level: str | None = None) -> dict:
        capped = max(1, min(lines, 1000))
        if service not in _LOG_FILES:
            return {
                "ok": False,
                "text": f"未知服务 '{service}',可用:{sorted(_LOG_FILES)}",
                "suggestion": "选择 connector / maintenance / admin / launcher 之一",
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
        def finish(result: dict) -> dict:
            output = _with_conn_suggestion(result)
            _record_readiness_probe(cfg, name, "erp", {
                "ok": output.get("status") in ("connected", "connected_limited"),
                "status": output.get("status", "unknown"),
                "error_code": output.get("error"),
            })
            return output
        if scfg.adapter != "mssql_readonly":
            return finish({
                "status": "failed", "error": "unsupported",
                "detail": "连接测试仅支持 mssql_readonly 适配器",
            })
        dsn = os.environ.get(scfg.dsn_env or "", "")
        if not dsn:
            return finish({
                "status": "failed", "error": "missing_dsn",
                "detail": f"环境变量 {scfg.dsn_env} 未设置",
            })
        started = time.perf_counter()
        result = _probe_connection_with_timeout(dsn, timeout=10)
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return finish(result)

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
        if body.action not in ("sync", "reconcile", "reconcile_deep"):
            raise http_error(
                400, f"不支持的动作 '{body.action}'",
                "支持 action=sync / reconcile / reconcile_deep",
            )
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        if body.action in ("reconcile", "reconcile_deep"):
            if body.tables:
                raise http_error(
                    422, "对账动作不支持限定 tables",
                    "对账按已配置抽取计划执行；如需定向修复请从对账结果进入",
                )
            return _start_reconcile_run(
                cfg, name, scfg, deep=body.action == "reconcile_deep")
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

        violations = cfg.production_violations()
        if violations:
            raise http_error(
                409, "生产配置未就绪，已拒绝启动同步",
                "修正数据驻留阻断项后重新执行",
                code="production_config_not_ready",
                violations=violations,
            )

        # 1. 预检:窗口外 / tables 为空立即返回,不创建 run
        preflight = check_sync_preflight(name, scfg)
        if not preflight.executed:
            if preflight.reason == "tables_unconfigured":
                return {
                    "action": "sync", "source": name, "executed": False,
                    "run_id": None, "status": "not_started",
                    "reason": preflight.reason, "overlap_warning": True,
                    "note": preflight.note,
                    "suggestion": preflight.suggestion,
                    "follow_up_url": "/metadata",
                }
            return {
                "action": "sync", "source": name, "executed": False,
                "run_id": None, "status": "not_started",
                "reason": preflight.reason,
                "note": preflight.note,
                "suggestion": preflight.suggestion,
                "follow_up_url": "/status",
            }

        # 2. 跨进程锁:已运行时不排队
        lock = SourceSyncLock.try_acquire(cfg.landing, name)
        if lock is None:
            existing = SourceSyncLock.find_running_run(cfg.landing, name)
            return {
                "action": "sync", "source": name, "executed": False,
                "reason": "already_running", "run_id": existing,
                "status": "running",
                "note": "已有同步正在运行" if existing else "已有同步正在启动中",
                "suggestion": "等待当前运行完成后再触发",
                "follow_up_url": f"/runs?watch={existing}" if existing else "/runs",
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
            "follow_up_url": f"/runs?watch={run_id}",
        }

    def _start_reconcile_run(cfg, name, scfg, *, deep: bool) -> dict:
        violations = cfg.production_violations()
        if violations:
            raise http_error(
                409, "生产配置未就绪，已拒绝启动对账",
                "修正数据驻留阻断项后重新执行",
                code="production_config_not_ready",
                violations=violations,
            )
        if not scfg.table_whitelist():
            return {
                "action": "reconcile_deep" if deep else "reconcile",
                "source": name, "executed": False,
                "run_id": None, "status": "not_started",
                "reason": "tables_unconfigured",
                "note": "尚未配置抽取表，对账不会访问 ERP",
                "suggestion": "先完成元数据选表和抽取计划",
                "follow_up_url": "/metadata",
            }
        from ...shared.config import in_window
        if not in_window(datetime.now().time(), scfg.windows):
            return {
                "action": "reconcile_deep" if deep else "reconcile",
                "source": name, "executed": False,
                "run_id": None, "status": "not_started",
                "reason": "outside_window",
                "note": "当前不在允许的抽取窗口内",
                "suggestion": "等待窗口开启或调整 windows 配置",
                "follow_up_url": "/status",
            }
        lock = SourceSyncLock.try_acquire(cfg.landing, name)
        if lock is None:
            existing = SourceSyncLock.find_running_run(cfg.landing, name)
            return {
                "action": "reconcile_deep" if deep else "reconcile",
                "source": name, "executed": False,
                "reason": "already_running", "run_id": existing,
                "status": "running",
                "note": "已有同步或对账正在运行",
                "suggestion": "等待当前运行完成后再触发",
                "follow_up_url": f"/runs?watch={existing}" if existing else "/runs",
            }
        store = LandingStore(cfg.landing)
        try:
            run_id = store.start_run(name, "reconcile")
            _TRIGGER_EXECUTOR.submit(
                _run_reconcile_worker,
                run_id, name, scfg, cfg.landing, deep, lock)
        except Exception as exc:
            try:
                if "run_id" in locals():
                    store.finish_running_run(
                        run_id, status="failed", detail=_sanitize_detail(str(exc)))
            finally:
                store.con.close()
                lock.release()
            raise http_error(
                500, f"无法提交后台对账任务: {_sanitize_detail(str(exc))}",
                "重启中间机管理进程后重试",
            )
        store.con.close()
        return {
            "action": "reconcile_deep" if deep else "reconcile",
            "source": name, "run_id": run_id,
            "executed": True, "status": "started",
            "note": f"{'深度' if deep else 'L1'}对账已后台启动, 运行 ID #{run_id}",
            "follow_up_url": f"/runs?watch={run_id}",
        }

    # ---- 告警静默 ----

    @api.get("/alerts")
    def alerts() -> dict:
        """聚合进程、维护、抽取和推送告警，返回稳定分类与生命周期。"""
        cfg = reload_config()
        status = build_status(cfg, config_path=cfg_path)
        db = LandingStore(cfg.landing)
        try:
            silences = {
                row["alert_key"]: row["silenced_until"]
                for row in db.list_alert_silences()
            }
            items: list[dict[str, Any]] = []
            for alert in status.get("alerts", []):
                key = str(alert["key"])
                items.append({
                    **alert,
                    "status": "active",
                    "first_seen_at": status["observed_at"],
                    "last_seen_at": status["observed_at"],
                    "occurrences": 1,
                    "source": None,
                    "table": None,
                    "retryable": False,
                    "silence_allowed": False,
                    "silenced_until": silences.get(key),
                })

            failed_steps = db.con.execute(
                "SELECT r.id AS run_id, r.source, r.run_type, r.started_at, "
                "r.finished_at, s.target, s.error "
                "FROM d2a_run_step s JOIN d2a_sync_run r ON r.id = s.run_id "
                "WHERE s.status = 'failed' AND r.run_type IN ('sync','reconcile') "
                "ORDER BY r.started_at DESC, s.id DESC LIMIT 200"
            ).fetchall()
            grouped: dict[str, dict] = {}
            for row in failed_steps:
                category = _error_category(row["error"])
                key = f"run:{row['source']}:{row['target']}:{category}"
                item = grouped.setdefault(key, {
                    "key": key,
                    "severity": "error",
                    "category": category,
                    "title": f"{row['target']} {row['run_type']} 失败",
                    "source": row["source"], "table": row["target"],
                    "detail": _sanitize_detail(row["error"] or "运行步骤失败"),
                    "suggestion": "查看运行步骤和日志，修正后执行定向重试",
                    "first_seen_at": row["started_at"],
                    "last_seen_at": row["finished_at"] or row["started_at"],
                    "occurrences": 0, "run_id": row["run_id"],
                    "retryable": True, "silence_allowed": True,
                    "links": [
                        f"/runs?watch={row['run_id']}",
                        "/logs?service=connector",
                    ],
                })
                item["occurrences"] += 1
                if row["started_at"] < item["first_seen_at"]:
                    item["first_seen_at"] = row["started_at"]
            for key, item in grouped.items():
                latest = db.con.execute(
                    "SELECT s.status, r.started_at FROM d2a_run_step s "
                    "JOIN d2a_sync_run r ON r.id = s.run_id "
                    "WHERE r.source = ? AND s.target = ? "
                    "ORDER BY r.started_at DESC, s.id DESC LIMIT 1",
                    (item["source"], item["table"]),
                ).fetchone()
                item["status"] = "recovered" if latest and latest["status"] == "ok" else "active"
                item["recovered_at"] = latest["started_at"] if item["status"] == "recovered" else None
                item["silenced_until"] = silences.get(key)
                items.append(item)

            failed_pushes = db.con.execute(
                "SELECT source, table_name, error_detail, error_category, retryable, "
                "created_at, run_id FROM d2a_http_push_log "
                "WHERE status = 'failed' ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            push_grouped: dict[str, dict] = {}
            latest_push = {
                (item["source"], item["table_name"]): item
                for item in db.push_log_table_summaries()
            }
            for row in failed_pushes:
                category = row["error_category"] or _error_category(row["error_detail"])
                key = f"push:{row['source']}:{row['table_name']}:{category}"
                item = push_grouped.setdefault(key, {
                    "key": key, "severity": "error", "category": category,
                    "title": f"{row['table_name']} 推送失败",
                    "source": row["source"], "table": row["table_name"],
                    "detail": _sanitize_detail(row["error_detail"] or "推送失败"),
                    "suggestion": "确认平台、网络和 Token 后定向重推",
                    "first_seen_at": row["created_at"], "last_seen_at": row["created_at"],
                    "occurrences": 0, "run_id": row["run_id"],
                    "retryable": bool(row["retryable"] if row["retryable"] is not None else True),
                    "silence_allowed": True,
                    "links": ["/push-logs", "/logs?service=connector"],
                })
                item["occurrences"] += 1
                if row["created_at"] < item["first_seen_at"]:
                    item["first_seen_at"] = row["created_at"]
            for key, item in push_grouped.items():
                latest = latest_push.get((item["source"], item["table"]))
                item["status"] = "recovered" if latest and latest["status"] == "completed" else "active"
                item["recovered_at"] = latest.get("last_at") if item["status"] == "recovered" else None
                item["silenced_until"] = silences.get(key)
                items.append(item)

            # 持久化根因生命周期。轮询不会增加 occurrences；只有首次出现、
            # 历史失败计数增长或 recovered→active 再次发生才更新计数。
            observed_at = status["observed_at"]
            existing_rows = db.con.execute(
                "SELECT * FROM d2a_alert_event").fetchall()
            existing = {row["alert_key"]: row for row in existing_rows}
            active_items = {
                str(item["key"]): item for item in items
                if item.get("status") == "active"
            }
            for key, item in active_items.items():
                row = existing.get(key)
                payload_json = _json.dumps(
                    item, ensure_ascii=False, sort_keys=True)
                first_seen = item.get("first_seen_at") or observed_at
                last_seen = item.get("last_seen_at") or observed_at
                observed_occurrences = max(1, int(item.get("occurrences") or 1))
                if row is None:
                    db.con.execute(
                        "INSERT INTO d2a_alert_event "
                        "(alert_key, status, first_seen_at, last_seen_at, "
                        "recovered_at, occurrences, payload_json) "
                        "VALUES (?, 'active', ?, ?, NULL, ?, ?)",
                        (key, first_seen, last_seen,
                         observed_occurrences, payload_json),
                    )
                elif row["status"] == "recovered":
                    db.con.execute(
                        "UPDATE d2a_alert_event SET status = 'active', "
                        "last_seen_at = ?, recovered_at = NULL, "
                        "occurrences = occurrences + 1, payload_json = ? "
                        "WHERE alert_key = ?",
                        (last_seen, payload_json, key),
                    )
                else:
                    db.con.execute(
                        "UPDATE d2a_alert_event SET last_seen_at = ?, "
                        "occurrences = MAX(occurrences, ?), payload_json = ? "
                        "WHERE alert_key = ?",
                        (last_seen, observed_occurrences, payload_json, key),
                    )
            for key, row in existing.items():
                if row["status"] == "active" and key not in active_items:
                    db.con.execute(
                        "UPDATE d2a_alert_event SET status = 'recovered', "
                        "recovered_at = ? WHERE alert_key = ?",
                        (observed_at, key),
                    )
            db.con.commit()

            event_rows = db.con.execute(
                "SELECT * FROM d2a_alert_event "
                "ORDER BY last_seen_at DESC").fetchall()
            event_by_key = {row["alert_key"]: row for row in event_rows}
            current_keys = {str(item["key"]) for item in items}
            for item in items:
                event = event_by_key.get(str(item["key"]))
                if event is None:
                    continue
                item["status"] = event["status"]
                item["first_seen_at"] = event["first_seen_at"]
                item["last_seen_at"] = event["last_seen_at"]
                item["recovered_at"] = event["recovered_at"]
                item["occurrences"] = event["occurrences"]
            for event in event_rows:
                if event["status"] != "recovered" or event["alert_key"] in current_keys:
                    continue
                try:
                    recovered_item = _json.loads(event["payload_json"])
                except (TypeError, ValueError, _json.JSONDecodeError):
                    continue
                recovered_item.update({
                    "status": "recovered",
                    "first_seen_at": event["first_seen_at"],
                    "last_seen_at": event["last_seen_at"],
                    "recovered_at": event["recovered_at"],
                    "occurrences": event["occurrences"],
                    "silenced_until": None,
                })
                items.append(recovered_item)

            return {
                "observed_at": status["observed_at"],
                "alerts": items,
                "active": sum(1 for item in items if item["status"] == "active"),
                "recovered": sum(1 for item in items if item["status"] == "recovered"),
            }
        finally:
            db.con.close()

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
        if body.alert_key.startswith("readiness:"):
            raise http_error(
                409, "生产阻断告警不可静默",
                "修复进程、备份、磁盘或数据驻留问题后告警会自动恢复",
            )
        if not (1 <= body.hours <= 24 * 7):
            raise http_error(422, "hours 须为 1..168", "调整静默时长")
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
            "run_type": r["run_type"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "tables": r["tables"], "rows": r["rows"],
            "detail": _sanitize_detail(r["detail"] or ""),
            "generation_id": r["generation_id"],
        }

    def _map_step_row(s) -> dict:
        return {
            "id": s["id"], "ordinal": s["ordinal"], "kind": s["kind"],
            "target": s["target"], "status": s["status"],
            "started_at": s["started_at"], "finished_at": s["finished_at"],
            "batch_id": s["batch_id"],
            "rows_in": s["rows_in"], "rows_out": s["rows_out"],
            "quarantined": s["quarantined"],
            "repaired": s["repaired"],
            "soft_deleted": s["soft_deleted"],
            "batches": s["batches"], "progressed_at": s["progressed_at"],
            "expected_rows": s["expected_rows"],
            "watermark_before": s["watermark_before"],
            "watermark_after": s["watermark_after"],
            "error": _sanitize_detail(s["error"] or ""),
            "error_id": s["error_id"],
        }

    @api.get("/runs")
    def runs(source: str | None = None, run_type: str | None = None,
             limit: int = 20, offset: int = 0) -> dict:
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
            if run_type not in (None, "sync", "reconcile"):
                raise http_error(
                    422, "run_type 仅支持 sync / reconcile",
                    "调整运行类型过滤条件")
            where = "WHERE run_type IN ('sync', 'reconcile')"
            params: list[Any] = []
            if run_type is not None:
                where += " AND run_type = ?"
                params.append(run_type)
            if name is not None:
                where += " AND source = ?"
                params.append(name)
            (total,) = db.con.execute(
                f"SELECT COUNT(*) FROM d2a_sync_run {where}", params).fetchone()
            rows = db.con.execute(
                f"SELECT id, source, run_type, status, started_at, finished_at, "
                f"tables, rows, detail, generation_id "
                f"FROM d2a_sync_run {where} "
                f"ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset])
            return {
                "runs": [_map_run_row(r) for r in rows],
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
            generation_steps = db.con.execute(
                "SELECT step_kind, status, created_at, error_category, retryable, "
                "retry_count, error_detail FROM d2a_http_push_log "
                "WHERE run_id = ? AND generation_id IS NOT NULL "
                "ORDER BY created_at, id", (run_id,)).fetchall()
            return {
                "run": _map_run_row(r),
                "steps": [_map_step_row(s) for s in steps],
                "generation": {
                    "generation_id": r["generation_id"],
                    "events": [
                        {
                            **dict(item),
                            "error_detail": _sanitize_detail(
                                item["error_detail"] or ""),
                        }
                        for item in generation_steps
                    ],
                } if r["generation_id"] else None,
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
            "error_detail": _sanitize_detail(r["error_detail"] or ""),
            "retry_count": r["retry_count"],
            "duration_ms": r["duration_ms"], "created_at": r["created_at"],
            "generation_id": r["generation_id"],
            "error_category": r["error_category"],
            "retryable": (
                None if r["retryable"] is None else bool(r["retryable"])),
            "receipt_received": (
                None if r["receipt_received"] is None
                else bool(r["receipt_received"])),
            "idempotent_replay": (
                None if r["idempotent_replay"] is None
                else bool(r["idempotent_replay"])),
            "receipt_digest": r["receipt_digest"],
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
            result = run_sync_cycle(
                name, scfg, landing_path, templates,
                run_id=run_id, acquired_lock=lock, tables=tables)
            if not result.executed:
                skipped_store = LandingStore(landing_path)
                try:
                    skipped_store.finish_running_run(
                        run_id,
                        status=("paused" if result.reason == "outside_window"
                                else "failed"),
                        detail=result.note or result.reason,
                    )
                finally:
                    skipped_store.con.close()
        except Exception as exc:
            failed_store = LandingStore(landing_path)
            try:
                failed_store.finish_running_run(
                    run_id, status="failed", detail=_sanitize_detail(str(exc)))
            finally:
                failed_store.con.close()
        finally:
            lock.release()

    def _run_reconcile_worker(
        run_id: int, name: str, scfg, landing_path: str, deep: bool, lock,
    ) -> None:
        try:
            executed = run_reconcile_cycle(
                name, scfg, landing_path, deep=deep,
                run_id=run_id, acquired_lock=lock)
            if not executed:
                skipped_store = LandingStore(landing_path)
                try:
                    skipped_store.finish_running_run(
                        run_id, status="paused",
                        detail="对账开始前窗口已关闭，未执行",
                    )
                finally:
                    skipped_store.con.close()
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
