"""中间机管理 FastAPI:配置 / 首次浏览器配置 / 状态 / 日志 / 调试。"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
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
from ..connect.config import ConnectConfig, SourceConfig, config_revision, load_config
from ..connect import discoverers as _discoverers  # noqa: F401  — 注册 MetadataDiscoverer
from ..connect.metadata import (
    DEFAULT_SCAN_TABLE_LIMIT,
    MetadataDiscoveryUnsupported,
    MetadataError,
    ScanStore,
    TableSummary,
    build_discoverer,
    discoverer_default_schema,
    extraction_plan_keys,
    in_extraction_plan,
)
from ..connect.scheduler import run_sync_cycle

from .status import build_status

_SCAN_STORE = ScanStore()

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
    revision: str | None = None


class TriggerBody(BaseModel):
    action: str
    source: str | None = None


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
                tbl: {
                    "mode": spec.mode,
                    "watermark": spec.watermark,
                    "schema": spec.schema or "dbo",
                    "key_columns": spec.key_columns,
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
            raise HTTPException(404, f"配置中没有源 '{source}',可用:{sorted(cfg.sources)}")
        return source, scfg
    name = next(iter(cfg.sources))
    return name, cfg.sources[name]


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
        if "timeout" in msg.lower():
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
        return False, [{"field": "", "message": str(e)}]
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
                raise HTTPException(409, "尚未完成首次配置")
            current = config_revision(cfg_path)
            if body.revision is None:
                raise HTTPException(
                    409, {"detail": "已有配置的更新必须提交 revision,请刷新页面获取最新修订号。",
                          "hint": "GET /api/config 返回 revision 字段",
                          "current_revision": current})
            if body.revision != current:
                raise HTTPException(409, {"detail": "配置已被其他会话修改,请刷新后重新提交。",
                                         "current_revision": current})
            try:
                ok, errors = merge_whitelist_and_save(
                    cfg_path, MIDDLE_EDITABLE, patch, validate=load_config)
            except Exception as e:
                return {"ok": False, "errors": [{"field": "", "message": str(e)}],
                        "restart_required": False, "revision": current}
            revision = config_revision(cfg_path) if ok else current
        return {"ok": ok, "errors": errors, "restart_required": ok, "revision": revision}

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

    @api.post("/connection/test")
    def test_connection(body: TestConnectionBody = TestConnectionBody()) -> dict:
        """纯连接测试:只验证 DSN 可连、数据库可访问、元数据权限。不读取 tables。"""
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        if scfg.adapter != "mssql_readonly":
            return {"status": "failed", "error": "unsupported",
                    "detail": "连接测试仅支持 mssql_readonly 适配器"}
        dsn = os.environ.get(scfg.dsn_env or "", "")
        if not dsn:
            return {"status": "failed", "error": "missing_dsn",
                    "detail": f"环境变量 {scfg.dsn_env} 未设置"}
        started = time.perf_counter()
        result = _probe_connection_with_timeout(dsn, timeout=10)
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        return result

    def _open_discoverer(scfg: SourceConfig):
        try:
            return build_discoverer(scfg)
        except MetadataDiscoveryUnsupported as e:
            raise HTTPException(400, {"code": e.code, "detail": str(e)}) from e
        except MetadataError as e:
            status = 503 if e.code in ("connection_failed", "timeout") else 400
            if e.code == "permission_denied":
                status = 403
            raise HTTPException(status, {"code": e.code, "detail": e.message}) from e

    def _default_schema_for(scfg: SourceConfig) -> str:
        try:
            return discoverer_default_schema(scfg.adapter)
        except MetadataDiscoveryUnsupported as e:
            raise HTTPException(400, {"code": e.code, "detail": str(e)}) from e

    def _planned_keys(scfg: SourceConfig) -> set[tuple[str, str]]:
        return extraction_plan_keys(
            scfg.tables, default_schema=_default_schema_for(scfg))

    def _run_scan(scan_id: str, scfg: SourceConfig, deadline_mono: float) -> None:
        discoverer = None
        try:
            if time.monotonic() > deadline_mono:
                _SCAN_STORE.fail(scan_id, "timeout", "扫描在开始前已超时", status="timeout")
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
                        _SCAN_STORE.fail(scan_id, e.code, e.message)
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
                    ))
            if timed_out:
                if finalized or details:
                    _SCAN_STORE.complete(
                        scan_id, finalized, details,
                        status="partial",
                        table_errors=table_errors,
                        error_code="timeout",
                        error_detail="扫描超过总时限,结果可能不完整",
                    )
                else:
                    _SCAN_STORE.fail(scan_id, "timeout", "扫描超过总时限", status="timeout")
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
            _SCAN_STORE.fail(scan_id, e.code, str(e))
        except MetadataError as e:
            status = "timeout" if e.code == "timeout" else "failed"
            _SCAN_STORE.fail(scan_id, e.code, e.message, status=status)
        except Exception as e:
            _SCAN_STORE.fail(scan_id, type(e).__name__, _sanitize_detail(str(e)))
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
            raise HTTPException(409, {"code": e.code, "detail": e.message}) from e
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
            raise HTTPException(404, "scan_id 不存在或已过期")
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
            raise HTTPException(
                409,
                {"code": "metadata_stale",
                 "detail": "尚无可用元数据缓存,请先 POST /api/metadata/scans"},
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
                "in_extraction_plan": in_extraction_plan(
                    t.schema, t.name, planned, default_schema=default_schema),
                "error_code": t.error_code,
                "error_detail": t.error_detail,
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
                status = 404 if e.code == "table_missing" else 400
                if e.code == "permission_denied":
                    status = 403
                elif e.code in ("connection_failed", "timeout"):
                    status = 503
                raise HTTPException(status, {"code": e.code, "detail": e.message}) from e
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
        }

    @api.post("/actions/trigger")
    def trigger_action(body: TriggerBody) -> dict:
        if body.action == "reconcile":
            raise HTTPException(400, "中间机管理 v1 不支持 reconcile")
        if body.action != "sync":
            raise HTTPException(400, f"不支持的动作 '{body.action}'")
        cfg = reload_config()
        name, scfg = _resolve_source(cfg, body.source)
        if not scfg.table_whitelist():
            return {"action": "sync", "source": name, "executed": False,
                    "reason": "tables_unconfigured", "overlap_warning": True,
                    "note": "尚未配置抽取表，未发起同步"}
        executed = run_sync_cycle(name, scfg, cfg.landing, cfg.templates)
        return {"action": "sync", "source": name, "executed": executed,
                "overlap_warning": True,
                "reason": "executed" if executed else "outside_window",
                "note": "" if executed else "错峰窗口外，未发起（窗口约束同样生效）"}

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
