"""平台控制台:Vue Console 静态入口 + JSON API + 运维动作。

安全:
- 只读视图直接查落地库;动作(sync / reconcile / apply / retry)复用 connect
  引擎,错峰窗口 / 白名单 / 只读适配器约束原样生效,控制台不开新的旁路;
- 可选 Bearer Token(--token 或环境变量 D2A_CONSOLE_TOKEN),内网部署建议启用;
- 未加载 --config 时为纯只读模式,动作接口返回 409 并说明原因。
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..admin_common.config_edit import PLATFORM_EDITABLE, merge_whitelist_and_save
from ..admin_common.home_layout import HomeLayout
from ..admin_common.logs import tail_lines
from ..admin_common.secrets_file import apply_secrets_to_environ, save_secrets
from ..admin_common.setup_yaml import build_platform_yaml, write_yaml
from ..connect.config import PlatformConfig, load_platform_config
from ..connect.landing import LandingStore
from ..connect.dataset_publish import (
    PublishedSnapshotError,
    build_dataset,
    publish_dataset,
    published_read_tx,
    resolve_published_snapshot,
    rollback_dataset,
)
from ..connect.mapping_preview import PreviewError, preview_mapping
from ..connect.field_lineage import LineageKeyError, require_lineage_key_token
from ..mapping import parse_field_expr
from ..metamodel.dataset_publish_contract import validate_build_table
from ..metamodel.loader import load_pack
from ..metamodel.versioning import object_layer_fully_published, parse_object_manifest
from ..mcp_server.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceContext,
    EvidenceStore,
    GatewayAuditRecord,
    canonical_json_dumps,
    is_valid_digest,
)
from . import data_browser as br
from . import observability as obs
from .mcp_lab import mcp_lab_error_response
from .contracts import (
    DEFAULT_BREAKER_THRESHOLD,
    AccessAuditPage,
    ActionBody,
    ActionExecutionResult,
    ApplyActionBody,
    ApplyActionResult,
    AuditRecord,
    ConfigPatch,
    ConfigSaveResponse,
    ConfigViewResponse,
    DatasetActionResult,
    DatasetDetail,
    DatasetStatus,
    DatasetSummary,
    HttpError,
    LogsResponse,
    MappingPreviewError,
    MappingPreviewErrorReasonCode,
    MappingPreviewRequest,
    MappingPreviewResponse,
    ObjectLineageError,
    ObjectLineageResponse,
    McpCallBody,
    McpLabError,
    McpMetricsQueryResult,
    McpObjectQueryResult,
    McpQueryMeta,
    McpToolResult,
    ObjectRowsPageResponse,
    ObjectSummary,
    OverviewResponse,
    PipelineResponse,
    ProposalRequest,
    ProposalResponse,
    QueryEvidenceDetailResponse,
    QuarantineDetail,
    QuarantineGroup,
    QuarantineRecord,
    RawDataPageResponse,
    RawTableCatalogResponse,
    RawTablePageResponse,
    RequestError,
    RetryActionError,
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
    TemplateMetric,
    TemplateObject,
    ValidationError,
    ValidationReportResponse,
    ValidationResult,
    ValidationRunRequest,
    ValidationRunStartedResponse,
)
from .validation import build_validation_report

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

_EVIDENCE_SESSION_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_EVIDENCE_SESSION_HEADER = "X-D2A-Session-ID"
_QUERY_ID_RE = re.compile(r"^qry_[0-9a-f]{24}$")
_PROPOSAL_ID_RE = re.compile(r"^prp_[0-9a-f]{24}$")

# PreviewError.reason_code → HTTP(§3.9);鉴权码由 endpoint 直接产出。
_PREVIEW_HTTP_STATUS: dict[str, int] = {
    "unauthorized": 401,
    "token_not_configured": 403,
    "object_not_found": 404,
    "source_not_found": 404,
    "raw_table_not_found": 404,
    "sample_batch_not_found": 404,
    "current_binding_unavailable": 409,
    "raw_unavailable": 409,
    "draft_invalid": 422,
    "sample_invalid": 422,
    "anchor_changed": 422,
    "preview_failed": 500,
}

# ObjectLineageError.reason_code → HTTP(§3.8);鉴权码由 endpoint 直接产出。
_LINEAGE_HTTP_STATUS: dict[str, int] = {
    "unauthorized": 401,
    "token_not_configured": 403,
    "object_not_found": 404,
    "field_not_found": 404,
    "record_not_found": 404,
    "dataset_not_published": 409,
    "snapshot_corrupt": 409,
    "lineage_incomplete": 409,
    "lineage_key_invalid": 422,
    "lineage_query_failed": 500,
}

_SETUP_API_PATHS = frozenset({"/api/setup", "/api/setup/status"})
_AUDIT_SQL_BUDGET = 4096

_PKG = Path(__file__).resolve().parent
_REPO_ROOT = _PKG.parent.parent
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
_VUE_MODULE_MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".wasm": "application/wasm",
}

_VUE_MISSING_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Vue Console 未安装</title></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;line-height:1.5">
<h1>控制台未安装或未构建</h1>
<p>平台首页需要 Vue Console 的构建产物（<code>console-ui/dist</code>）。</p>
<p>开发可用 Vite；源码/便携包/Docker 需先执行 <code>cd console-ui &amp;&amp; npm run build</code>，
或设置环境变量 <code>D2A_VUE_DIST</code> 指向含 <code>index.html</code> 的目录。</p>
<p>平台管理入口已统一为 Vue Console：<a href="/">/</a></p>
</body></html>
"""


def resolve_vue_dist() -> Path | None:
    """定位 Vue dist;优先 D2A_VUE_DIST,其次便携 home,再仓库/包内路径。"""
    env = (os.environ.get("D2A_VUE_DIST") or "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    home = (os.environ.get("D2A_HOME") or "").strip()
    if home:
        home_path = Path(home)
        candidates.append(home_path / "app" / "console-ui" / "dist")
        candidates.append(home_path / "console-ui" / "dist")
    candidates.append(_REPO_ROOT / "console-ui" / "dist")
    candidates.append(_PKG / "vue_dist")
    for cand in candidates:
        try:
            if (cand / "index.html").is_file():
                return cand.resolve()
        except OSError:
            continue
    return None


def resolve_build_version() -> str | None:
    """Read the immutable portable build label when the application has one."""
    home = (os.environ.get("D2A_HOME") or "").strip()
    if not home:
        return None
    try:
        raw = json.loads((Path(home) / "BUILD-INFO.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    value = raw.get("release_version") if isinstance(raw, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


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


def _parse_version_time(
    text: str | None, *, field: str, required: bool,
) -> datetime | None:
    """严格解析版本时间。

    - DB null/空串 + required=False → None(合法尚未发布)
    - 非空但无法解析 → 500(不得伪装成尚未发布);detail 含 error_id
    """
    if text is None or (isinstance(text, str) and not text.strip()):
        if required:
            error_id = uuid.uuid4().hex[:12]
            raise HTTPException(
                500, f"版本元数据 {field} 缺失或无法解析(error_id={error_id})")
        return None
    value = obs.aware(text if isinstance(text, str) else None)
    if value is None:
        error_id = uuid.uuid4().hex[:12]
        raise HTTPException(
            500, f"版本元数据 {field} 无法解析(error_id={error_id})")
    return value


def _dataset_error_fields(raw: str | None) -> tuple[str | None, str | None]:
    """数据集内部错误 → 固定安全摘要 + 稳定 error_id;原文永不出口。"""
    if not raw or not isinstance(raw, str) or not raw.strip():
        return None, None
    error_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"数据集构建失败(error_id={error_id})", error_id


def _published_at_required(status: str) -> bool:
    return status in ("published", "retired")


def _map_dataset_summary(record) -> dict:
    error, error_id = _dataset_error_fields(record.error)
    return {
        "dataset_version": record.dataset_version,
        "source": record.source,
        "template_version": record.template_version,
        "status": record.status,
        "built_at": _parse_version_time(record.built_at, field="built_at", required=True),
        "published_at": _parse_version_time(
            record.published_at,
            field="published_at",
            required=_published_at_required(record.status),
        ),
        "previous_dataset_version": record.previous_dataset_version,
        "error": error,
        "error_id": error_id,
        "object_manifest": parse_object_manifest(record.object_manifest),
    }


def _map_object_version(record) -> dict:
    return {
        "object": record.object,
        "object_version": record.object_version,
        "binding_hash": record.binding_hash,
        "row_count": record.row_count,
        "batch_id": record.batch_id,
        "build_table": record.build_table,
        "status": record.status,
        "built_at": _parse_version_time(record.built_at, field="built_at", required=True),
        "published_at": _parse_version_time(
            record.published_at,
            field="published_at",
            required=_published_at_required(record.status),
        ),
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
     "semantics": "当前 source published 快照物理表行数合计;与 raw 因隔离/软删有差",
     "source": "published snapshot 物理表 COUNT(*)"},
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


def _platform_config_subset(cfg: PlatformConfig) -> dict[str, Any]:
    return {"templates": cfg.templates, "landing": cfg.landing}


def _validate_merged(path: Path, patch: dict[str, Any]) -> tuple[bool, list[dict[str, str]]]:
    """在临时副本上合并并 load_platform_config,不写原文件。"""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy2(path, tmp_path)
    try:
        return merge_whitelist_and_save(tmp_path, PLATFORM_EDITABLE, patch, validate=load_platform_config)
    finally:
        tmp_path.unlink(missing_ok=True)


def _compute_rate_state(rate: float | None, threshold: float) -> str:
    """隔离率状态:无证据 → unknown;0% → ok;低于阈值 → warning;达到阈值 → tripped。"""
    if rate is None:
        return "unknown"
    if rate == 0.0:
        return "ok"
    if rate >= threshold:
        return "tripped"
    return "warning"


def _compute_serving_state(
    db: LandingStore,
    table_exists: bool,
    table_ok: bool,
    object_rows: int | None,
    mapped_at: datetime | None,
    source: str,
    latest_apply_run_id: int | None,
    step_aborted: bool,
    binding_tables: list[str] | None = None,
) -> str:
    """对象数据新鲜度状态(M5 §6.3 决策矩阵)。

    优先级:not_materialized > unavailable > stale > fresh > unknown。

    binding_tables=None 表示无法确定 binding 表集合(未知对象/未加载 pack),
    此时无法核实 raw 新鲜度证据,不判 fresh。
    """
    if not table_exists:
        return "not_materialized"
    if not table_ok or object_rows is None:
        return "unavailable"
    # stale: step 被熔断中止 或 raw 明显新于 mapped_at
    if step_aborted:
        return "stale"
    # 按 binding 限定 raw 表,避免无关表的抽取时间污染新鲜度判断
    # 逐表跟踪证据:任一表缺失/不可读 → 不能判 fresh;全表完整且 ≤ mapped_at → fresh
    tables_with_evidence = 0
    tables_missing = 0
    if mapped_at is not None and binding_tables:
        try:
            raw_latest: datetime | None = None
            for bt in binding_tables:
                table_name = f"raw_{source}__{bt}"
                try:
                    rr = db.con.execute(
                        f'SELECT MAX("_d2a_extracted_at") AS m FROM "{table_name}"'
                    ).fetchone()
                    at = obs.aware(rr["m"])
                    if at is not None:
                        tables_with_evidence += 1
                        if raw_latest is None or at > raw_latest:
                            raw_latest = at
                    else:
                        tables_missing += 1
                except sqlite3.Error:
                    tables_missing += 1  # 单表读取失败视为缺失证据
            # 任一表缺失证据 → 不能判 fresh
            # fresh 仅在所有表都有证据且 raw 不晚于 mapped_at 时成立
            if tables_missing == 0 and raw_latest is not None:
                has_raw_evidence = True
                if raw_latest > mapped_at:
                    return "stale"
            elif tables_with_evidence > 0 and raw_latest is not None:
                # 部分表有证据、部分缺失:最多判 stale(如果有证据显示过期)
                has_raw_evidence = True
                if raw_latest > mapped_at:
                    return "stale"
            else:
                has_raw_evidence = False
        except sqlite3.Error:
            has_raw_evidence = False
    else:
        has_raw_evidence = False
    # fresh: 最近 apply 成功 + mapped_at 存在 + 所有 binding 表证据完整 + raw 不晚于 mapped_at
    if (latest_apply_run_id is not None and mapped_at is not None
            and binding_tables and has_raw_evidence and tables_missing == 0):
        run = db.con.execute(
            "SELECT status FROM d2a_sync_run WHERE id = ?",
            (latest_apply_run_id,)).fetchone()
        if run and run["status"] == "ok":
            return "fresh"
    return "unknown"


def create_app(landing: str | None = None, templates: str = "templates",
               config: PlatformConfig | None = None, token: str | None = None,
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
            config = load_platform_config(home_layout.platform_yaml)
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
        # M6:进程内长生命周期 QueryService(与 landing/templates/source/max_tier 签名绑定)
        "query_service": None,
        "query_service_sig": None,
        "_query_service_lock": threading.Lock(),
        "_validation_lock": threading.Lock(),
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

    def _invalidate_query_service() -> None:
        with state["_query_service_lock"]:
            state["query_service"] = None
            state["query_service_sig"] = None

    def hydrate_from_disk() -> None:
        path = state["config_path"]
        if path is None or not Path(path).is_file():
            return
        cfg = load_platform_config(path)
        state["config"] = cfg
        state["landing"] = cfg.landing
        state["templates"] = cfg.templates
        state["pack"] = load_pack(cfg.templates)
        _invalidate_query_service()

    def _is_quarantine_detail(path: str) -> bool:
        # /api/quarantine/{id} where id is numeric; NOT /api/quarantine or /api/quarantine/groups
        if not path.startswith("/api/quarantine/"):
            return False
        id_part = path[len("/api/quarantine/"):]
        return id_part.isdigit()

    def _is_mapping_preview(path: str) -> bool:
        # /api/mappings/{object}/preview — Bearer 由端点自行强制,不接受 ?token=
        if not path.startswith("/api/mappings/"):
            return False
        return path.endswith("/preview")

    def _is_object_lineage(path: str) -> bool:
        # /api/objects/{object}/{key}/lineage — Bearer 由端点自行强制
        parts = path.strip("/").split("/")
        return (
            len(parts) == 5
            and parts[0] == "api"
            and parts[1] == "objects"
            and parts[4] == "lineage"
        )

    def auth(request: Request) -> None:
        path = request.url.path
        if path == "/api/data/raw" or path.startswith("/api/data/raw/"):
            return
        if _is_quarantine_detail(path):
            return  # 隔离详情自行做强制 Bearer + 审计
        if _is_mapping_preview(path):
            return  # mapping preview 自行做强制 Bearer + 审计(MappingPreviewError)
        if _is_object_lineage(path):
            return  # field lineage 自行做强制 Bearer + 审计(ObjectLineageError)
        if needs_setup():
            if path in ("/api/setup", "/api/setup/status") or path.startswith("/api/setup"):
                if _client_host(request) not in _LOOPBACK:
                    raise HTTPException(403, "首次配置仅允许本机访问")
                return
            if path == "/api/config" and request.method == "GET":
                return
            raise HTTPException(409, "尚未完成首次配置,请打开 /setup")

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

    def require_config() -> PlatformConfig:
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
        """仅接受 `Authorization: Bearer <token>`(scheme 大小写不敏感)。

        裸 token / 其它 scheme 视为未认证,供 raw 浏览、隔离详情与 mapping preview
        的强制 Bearer 门禁使用;不得用 removeprefix 把裸 token 当成合法 Bearer。
        """
        header = request.headers.get("authorization", "").strip()
        if len(header) < 8 or header[:7].lower() != "bearer ":
            return ""
        return header[7:].strip()

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

    def _requires_bearer_only(path: str) -> bool:
        """需要强制 Bearer 的 API 路径:raw 浏览 + 隔离详情 + mapping preview + lineage。"""
        return (
            _is_raw_api_path(path)
            or path == "/api/quarantine/{id}"
            or path == "/api/mappings/{object}/preview"
            or path == "/api/objects/{object}/{key}/lineage"
        )

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

    def _observed_sources() -> list[str]:
        """从落地库 raw 表和 sync_state 推导实际观测到的来源（优先）。"""
        sources: set[str] = set()
        try:
            if state["landing"]:
                db = LandingStore(state["landing"])
                rows = db.con.execute(
                    "SELECT DISTINCT source FROM d2a_sync_state").fetchall()
                sources.update(r[0] for r in rows)
                for row in db.con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'raw_%__%'"
                ).fetchall():
                    name = row[0]
                    parts = name[4:].split("__", 1)
                    if len(parts) == 2:
                        sources.add(parts[0])
        except Exception:
            pass
        return sorted(sources)

    def _allowed_sources() -> list[str]:
        """从模板 binding 推导允许的来源（仅用于授权/映射校验）。"""
        sources: set[str] = set()
        if state["pack"] is not None:
            for o in state["pack"].objects:
                for b in o.bindings:
                    if b.source and b.enabled:
                        sources.add(b.source)
        return sorted(sources)

    def _known_sources() -> list[str]:
        """返回所有已知来源：实际观测优先，模板允许其次。"""
        observed = set(_observed_sources())
        allowed = set(_allowed_sources())
        return sorted(observed) + sorted(allowed - observed)

    def default_source() -> str:
        observed = _observed_sources()
        if observed:
            return observed[0]
        allowed = _allowed_sources()
        if allowed:
            return allowed[0]
        return "digiwin_e10"

    def _query_service_max_tier() -> str:
        return os.environ.get("D2A_MCP_MAX_TIER", "说")

    def _query_service_signature() -> tuple[str, str, str, str]:
        if needs_setup() or not state["landing"]:
            raise HTTPException(409, "尚未完成首次配置或落地库不可用")
        landing = str(Path(state["landing"]).resolve())
        templates_root = str(Path(state["templates"]).resolve())
        return (landing, templates_root, default_source(), _query_service_max_tier())

    def get_query_service():
        """返回与当前配置签名绑定的进程内 QueryService;签名变化时原子替换。"""
        from ..mcp_server.core import QueryService

        sig = _query_service_signature()
        with state["_query_service_lock"]:
            svc = state["query_service"]
            if svc is None or state["query_service_sig"] != sig:
                svc = QueryService(
                    sig[0], sig[1], source=sig[2], max_tier=sig[3],
                )
                state["query_service"] = svc
                state["query_service_sig"] = sig
            return svc

    def _mcp_in_process(
        tool: str,
        params: dict[str, Any],
        *,
        context: EvidenceContext | None = None,
    ) -> dict:
        svc = get_query_service()
        if tool == "query_objects":
            allowed = {"object", "filters", "order_by", "desc", "limit"}
            unknown = sorted(set(params) - allowed)
            if unknown:
                raise ValueError(f"未知参数 {unknown}")
            return svc.query_objects(**params, context=context)
        allowed = {"metric", "group_by", "limit"}
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(f"未知参数 {unknown}")
        return svc.query_metrics(**params, context=context)

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

    app = FastAPI(title="data2agent 运维控制台", version=__version__)
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError,
    ) -> JSONResponse:
        path = request.url.path
        if path.endswith("/gateway/proposals") or path.endswith("/debug/mcp-call"):
            tool = "propose_action" if path.endswith("/gateway/proposals") else None
            return JSONResponse(
                status_code=422,
                content=McpLabError(
                    detail="查询或建议卡参数无效",
                    reason_code="invalid_params",
                    tool=tool,
                    retryable=False,
                    error_id=None,
                ).model_dump(),
            )
        if not _is_raw_api_path(path):
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

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """MCP Lab 路径上的 409(未配置等)统一为 McpLabError。"""
        path = request.url.path
        if exc.status_code == 409 and (
            path.endswith("/debug/mcp-call") or path.endswith("/gateway/proposals")
        ):
            tool = "propose_action" if path.endswith("/gateway/proposals") else "query_objects"
            # 请求体 tool 字段优先(mcp-call),解析失败则回退默认
            if path.endswith("/debug/mcp-call"):
                try:
                    body = await request.json()
                    if isinstance(body, dict) and body.get("tool") in (
                        "query_objects", "query_metrics",
                    ):
                        tool = body["tool"]
                except Exception:
                    pass
            detail = exc.detail if isinstance(exc.detail, str) else "尚未完成首次配置或落地库不可用"
            return JSONResponse(
                status_code=409,
                content=McpLabError(
                    detail=detail,
                    reason_code="mcp_unavailable",
                    tool=tool,
                    retryable=False,
                    error_id=None,
                ).model_dump(),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    def _invalid_session_response() -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=McpLabError(
                detail="缺少或非法的 MCP evidence session",
                reason_code="invalid_session",
                tool=None,
                retryable=False,
                error_id=None,
            ).model_dump(),
        )

    def _require_evidence_session(session_id: str | None) -> str | JSONResponse:
        if not session_id or not _EVIDENCE_SESSION_RE.fullmatch(session_id):
            return _invalid_session_response()
        return session_id

    def _evidence_store_unavailable(*, tool: str | None) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=McpLabError(
                detail="MCP 会话证据存储尚未接通",
                reason_code="evidence_store_unavailable",
                tool=tool,
                retryable=False,
                error_id=uuid.uuid4().hex[:12],
            ).model_dump(),
        )

    def _console_evidence_context(session_id: str) -> EvidenceContext:
        return EvidenceContext(
            principal="console:configured",
            session_id=session_id,
            channel="console",
        )

    def _parse_evidence_json(raw: str, *, field: str) -> object:
        try:
            return json.loads(raw)
        except Exception as exc:
            raise ValueError(
                f"evidence_integrity_failed: {field} JSON 无法解析"
            ) from exc

    def _require_query_id(query_id: str) -> None:
        if not _QUERY_ID_RE.fullmatch(query_id):
            raise ValueError("invalid_params: query_id 格式非法")

    def _require_proposal_id(proposal_id: str) -> None:
        if not _PROPOSAL_ID_RE.fullmatch(proposal_id):
            raise ValueError("invalid_params: proposal_id 格式非法")

    def _write_gateway_audit(
        *,
        context: EvidenceContext,
        operation: str,
        target: str,
        outcome: str,
        reason_code: str,
        query_id: str | None = None,
        proposal_id: str | None = None,
        dataset_version: str | None = None,
        result_digest: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        store = LandingStore(state["landing"])
        evidence = EvidenceStore(store)
        try:
            evidence.insert_audit(
                GatewayAuditRecord(
                    event_id=uuid.uuid4().hex[:24],
                    created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    principal=context.principal,
                    session_id=context.session_id,
                    channel=context.channel,
                    source=default_source(),
                    operation=operation,
                    target=target,
                    outcome=outcome,
                    reason_code=reason_code,
                    query_id=query_id,
                    proposal_id=proposal_id,
                    dataset_version=dataset_version,
                    result_digest=result_digest,
                    detail_json=canonical_json_dumps(detail or {}),
                ),
                commit=True,
            )
        except sqlite3.Error as exc:
            raise ValueError("evidence_store_unavailable: gateway audit persist failed") from exc
        finally:
            store.con.close()

    def _load_query_detail(
        query_id: str,
        *,
        context: EvidenceContext,
    ) -> QueryEvidenceDetailResponse:
        _require_query_id(query_id)
        store = LandingStore(state["landing"])
        evidence = EvidenceStore(store)
        try:
            record = evidence.get_query(query_id)
        except sqlite3.Error as exc:
            raise ValueError("evidence_store_unavailable: query evidence read failed") from exc
        finally:
            store.con.close()
        if record is None:
            raise ValueError("evidence_not_found: query evidence 不存在")
        if record.principal != context.principal:
            raise ValueError("evidence_principal_mismatch: query evidence 属于其他主体")
        if record.session_id != context.session_id:
            raise ValueError("evidence_session_mismatch: query evidence 不属于当前会话")
        try:
            expires_at = datetime.fromisoformat(record.expires_at)
        except ValueError as exc:
            raise ValueError("evidence_integrity_failed: expires_at 非法") from exc
        if expires_at <= datetime.now(expires_at.tzinfo):
            raise ValueError("query_expired: query evidence 已过期")
        if record.evidence_schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("evidence_integrity_failed: unsupported evidence schema version")
        if not is_valid_digest(record.result_digest):
            raise ValueError("evidence_integrity_failed: query evidence digest 非法")
        detail = QueryEvidenceDetailResponse.model_validate(
            {
                "query_id": record.query_id,
                "source": record.source,
                "tool": record.tool,
                "target": record.target,
                "session_id": record.session_id,
                "evidence_scope": "principal_session",
                "normalized_query": _parse_evidence_json(
                    record.normalized_query_json, field="normalized_query"
                ),
                "dataset_version": record.dataset_version,
                "template_version": record.template_version,
                "binding_hashes": _parse_evidence_json(
                    record.binding_hashes_json, field="binding_hashes"
                ),
                "result_digest": record.result_digest,
                "result_summary": _parse_evidence_json(
                    record.result_summary_json, field="result_summary"
                ),
                "warnings": _parse_evidence_json(record.warnings_json, field="warnings"),
                "row_count": record.row_count,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
            }
        )
        _write_gateway_audit(
            context=context,
            operation="get_query_evidence",
            target=query_id,
            outcome="ok",
            reason_code="ok",
            query_id=query_id,
            dataset_version=record.dataset_version,
            result_digest=record.result_digest,
        )
        return detail

    def _load_proposal_detail(
        proposal_id: str,
        *,
        context: EvidenceContext,
    ) -> ProposalResponse:
        _require_proposal_id(proposal_id)
        store = LandingStore(state["landing"])
        evidence = EvidenceStore(store)
        try:
            proposal = evidence.get_proposal(proposal_id)
            snapshots = evidence.list_proposal_evidence(proposal_id)
        except sqlite3.Error as exc:
            raise ValueError("evidence_store_unavailable: proposal evidence read failed") from exc
        finally:
            store.con.close()
        if proposal is None:
            raise ValueError("evidence_not_found: proposal evidence 不存在")
        if proposal.principal != context.principal:
            raise ValueError("evidence_principal_mismatch: proposal evidence 属于其他主体")
        if proposal.session_id != context.session_id:
            raise ValueError("evidence_session_mismatch: proposal evidence 不属于当前会话")
        if proposal.evidence_schema_version != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("evidence_integrity_failed: unsupported proposal schema version")
        if not snapshots:
            raise ValueError("evidence_integrity_failed: proposal 缺少 evidence snapshot")

        caveats: list[str] = []
        dataset_versions: set[str] = set()
        rendered_evidence: list[dict[str, Any]] = []
        for row in snapshots:
            if not is_valid_digest(row.result_digest):
                raise ValueError("evidence_integrity_failed: proposal snapshot digest 非法")
            normalized_query = _parse_evidence_json(
                row.normalized_query_json, field="proposal.normalized_query"
            )
            binding_hashes = _parse_evidence_json(
                row.binding_hashes_json, field="proposal.binding_hashes"
            )
            result_summary = _parse_evidence_json(
                row.result_summary_json, field="proposal.result_summary"
            )
            warnings = _parse_evidence_json(row.warnings_json, field="proposal.warnings")
            if row.dataset_version:
                dataset_versions.add(row.dataset_version)
            caveats.extend(str(w) for w in warnings if w)
            rendered_evidence.append(
                {
                    "claim": row.claim,
                    "query": {
                        "query_id": row.query_id,
                        "source": proposal.source,
                        "tool": row.query_tool,
                        "target": row.query_target,
                        "normalized_query": normalized_query,
                        "dataset_version": row.dataset_version,
                        "template_version": row.template_version,
                        "binding_hashes": binding_hashes,
                        "result_digest": row.result_digest,
                        "result_summary": result_summary,
                        "warnings": warnings,
                        "created_at": row.query_created_at,
                        "expires_at": None,
                    },
                }
            )
        if len(dataset_versions) > 1:
            raise ValueError("dataset_version_mismatch: proposal evidence 混用多个数据集版本")
        if dataset_versions and proposal.dataset_version not in dataset_versions:
            raise ValueError("dataset_version_mismatch: proposal dataset_version 与 snapshot 不一致")

        detail = ProposalResponse.model_validate(
            {
                "proposal_id": proposal.proposal_id,
                "at": proposal.created_at,
                "session_id": proposal.session_id,
                "source": proposal.source,
                "dataset_version": proposal.dataset_version,
                "object": proposal.object,
                "action": proposal.action,
                "action_desc": proposal.action_desc,
                "tier": proposal.tier,
                "conclusion": proposal.conclusion,
                "evidence": rendered_evidence,
                "caveats": sorted({c for c in caveats if c}),
                "governance": proposal.governance,
            }
        )
        _write_gateway_audit(
            context=context,
            operation="get_proposal_evidence",
            target=proposal_id,
            outcome="ok",
            reason_code="ok",
            proposal_id=proposal_id,
            dataset_version=proposal.dataset_version,
            detail={"evidence_count": len(rendered_evidence)},
        )
        return detail

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
                # 普通管理 API 的 Token 可按部署关闭;raw 原始数据浏览与隔离详情始终
                # 要求显式 Bearer,与运行时强门禁保持一致。
                op["security"] = (
                    [{"HTTPBearer": []}]
                    if _requires_bearer_only(path)
                    else [{"HTTPBearer": []}, {}]
                )
                if path in _SETUP_API_PATHS:
                    op["description"] = (
                        (op.get("description") or "")
                        + ("\n\n" if op.get("description") else "")
                        + "Auth: skipped only while needs_setup=true (first-time bootstrap). "
                        "After configuration, Bearer is required when D2A_CONSOLE_TOKEN is set."
                    ).strip()
        # M4/M5/M1:列表总数响应头显式声明(类型层必须可见)
        for path in ("/api/runs", "/api/audit", "/api/quarantine", "/api/datasets"):
            get_op = schema.get("paths", {}).get(path, {}).get("get")
            if get_op is not None:
                get_op.setdefault("responses", {}).setdefault("200", {}) \
                    .setdefault("headers", {})["X-Total-Count"] = {
                        "schema": {"type": "integer"},
                        "description": "当前筛选条件下的总数(分页用)",
                    }
        # M6-T01:冻结查询 meta / Lab 错误 / 数据结果形状(运行时映射在后续任务)
        schemas = components.setdefault("schemas", {})
        for model in (
            McpQueryMeta, McpLabError, McpObjectQueryResult, McpMetricsQueryResult,
            QueryEvidenceDetailResponse, ProposalResponse,
        ):
            name = model.__name__
            if name in schemas:
                continue
            model_schema = model.model_json_schema(
                ref_template="#/components/schemas/{model}")
            for def_name, def_schema in model_schema.pop("$defs", {}).items():
                schemas.setdefault(def_name, def_schema)
            schemas[name] = model_schema
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    @app.get("/")
    def index():
        if vue_dist is None:
            return _vue_missing()
        if needs_setup():
            return RedirectResponse("/setup", status_code=302)
        return FileResponse(vue_dist / "index.html")

    @app.get("/config", include_in_schema=False)
    def legacy_config_page():
        return RedirectResponse("/setup" if needs_setup() else "/settings", status_code=302)

    @app.get("/debug", include_in_schema=False)
    def legacy_debug_page():
        return RedirectResponse("/mcp", status_code=302)

    @app.get("/v0", include_in_schema=False)
    def legacy_v0():
        return RedirectResponse("/", status_code=302)

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
            load_platform_config(cfg_path)
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
        sources = _observed_sources()
        out_sources = []
        for s in sources:
            sync_state = db.list_sync_watermarks(s)
            (quarantined,) = db.con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine WHERE source = ? AND resolved_at IS NULL",
                (s,)).fetchone()
            out_sources.append({"source": s, "state": sync_state, "quarantined": quarantined})
        # 与 Pipeline / MCP / QueryService 共用配置顺序首源。
        default_src = default_source()
        with published_read_tx(db):
            obj_stats = obs.object_stats(db, pack, default_src)
            objects = []
            for o in pack.objects:
                st = obj_stats.get(o.object, {})
                mapped = st.get("mapped_at")
                objects.append({
                    "object": o.object, "display_name": o.display_name,
                    "rows": st.get("rows"),
                    "mapped_at": mapped.isoformat() if mapped is not None else None,
                    "quarantined": st.get("quarantined") or 0,
                })

            # ---- M3 观测聚合(observability;查询失败按字段降级为 null + 告警)----
            query_failures: list[str] = []
            # raw 行数:任一源查询失败则整体为 null,部分合计不得冒充总数
            raw_rows_total = 0
            raw_failed = False
            agg_sources = sorted({s["source"] for s in out_sources})
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
            published = db.get_published_dataset(default_src)
            dataset_version = published.dataset_version if published else None
            # 原子发布:以数据集冻结 object_manifest 为分母;清单不全/有额外/非 published → 未发布。
            object_version = None
            if published is not None:
                obj_rows = db.list_object_versions(published.dataset_version)
                if object_layer_fully_published(published, obj_rows):
                    object_version = published.dataset_version
            # Pipeline 节点与 object_stats / versions 共用同一读快照,避免并发 publish 混版。
            nodes = obs.compute_nodes(
                db, pack, cfg, default_src, component_version=app_version,
            )
        recent = obs.recent_runs(db)
        if recent is None:
            query_failures.append("最近运行查询失败(d2a_sync_run)")
        trend = obs.sync_trend(db)
        if trend is None:
            query_failures.append("抽取趋势查询失败(d2a_sync_run)")
        alerts = obs.build_alerts(nodes, quarantine=qp, drafts=bs["draft"],
                                  query_failures=query_failures)

        return {"landing": state["landing"], "readonly": cfg is None,
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
                    "dataset": dataset_version, "object": object_version,
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

    def _validation_error(status: int, detail: str, reason_code: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=ValidationError(
                detail=detail, reason_code=reason_code,
                retryable=reason_code == "validation_in_progress",
            ).model_dump(),
        )

    @api.post(
        "/validation/run",
        response_model=ValidationRunStartedResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            409: {"model": ValidationError},
            422: _RESP_HTTP_ERROR[422],
            500: {"model": ValidationError},
        },
        tags=["v0.3"],
    )
    def validation_run(
        body: ValidationRunRequest = ValidationRunRequest(),
    ) -> ValidationRunStartedResponse | JSONResponse:
        """运行一次只读验收并原子持久化不可变报告。

        不接受 source、SQL、路径、会话或跳过失败参数；当前配置和已发布快照是
        唯一事实来源。运行本身即使总体 fail 也记为已完成的 validation run，
        不将“发现验收问题”伪装成执行失败。
        """
        lock: threading.Lock = state["_validation_lock"]
        if not lock.acquire(blocking=False):
            return _validation_error(409, "已有验收运行正在执行。", "validation_in_progress")
        db: LandingStore | None = None
        try:
            db = store()
            if db.has_running_validation():
                return _validation_error(409, "已有验收运行正在执行。", "validation_in_progress")
            pack = require_pack()
            source = default_source()
            run_id = db.start_run(source, "validation", commit=False)
            report = build_validation_report(
                db, run_id=run_id, pack=pack, source=source,
                config=state["config"], include_mcp_probe=body.include_mcp_probe,
                mcp_probe=lambda object_name: get_query_service().probe_objects(
                    object_name, limit=1,
                ),
            )
            # 先以公开契约校验，再落同一 JSON；详情与下载绝不各自重算。
            validated = ValidationReportResponse.model_validate(report)
            report_json = validated.model_dump(mode="json")
            db.finish_run(
                run_id, tables=0, rows=len(report_json["checks"]), status="ok",
                detail=f"validation:{report_json['overall_status']}", commit=False,
            )
            db.insert_validation_report(report_json, report_json["checks"], commit=False)
            db.con.commit()
            return ValidationRunStartedResponse(
                run_id=run_id,
                overall_status=report_json["overall_status"],
                report_path=f"/api/validation/runs/{run_id}",
            )
        except Exception:
            if db is not None:
                db.con.rollback()
            return _validation_error(500, "验收报告暂不可用。", "validation_unavailable")
        finally:
            if db is not None:
                db.con.close()
            lock.release()

    @api.get(
        "/validation/runs/{run_id}",
        response_model=ValidationReportResponse,
        responses={401: _RESP_HTTP_ERROR[401], 404: {"model": ValidationError}},
        tags=["v0.3"],
    )
    def validation_report(run_id: int) -> ValidationReportResponse | JSONResponse:
        db = store()
        try:
            run = db.con.execute(
                "SELECT run_type FROM d2a_sync_run WHERE id = ?", (run_id,),
            ).fetchone()
            report = db.get_validation_report(run_id) if run and run["run_type"] == "validation" else None
            if report is None:
                return _validation_error(404, "验收报告不存在。", "validation_not_found")
            return ValidationReportResponse.model_validate(report)
        finally:
            db.con.close()

    @api.get(
        "/validation/runs/{run_id}/report.json",
        response_model=ValidationReportResponse,
        responses={401: _RESP_HTTP_ERROR[401], 404: {"model": ValidationError}},
        tags=["v0.3"],
    )
    def validation_report_download(run_id: int) -> JSONResponse:
        db = store()
        try:
            run = db.con.execute(
                "SELECT run_type FROM d2a_sync_run WHERE id = ?", (run_id,),
            ).fetchone()
            report = db.get_validation_report(run_id) if run and run["run_type"] == "validation" else None
            if report is None:
                return _validation_error(404, "验收报告不存在。", "validation_not_found")
            safe_report = ValidationReportResponse.model_validate(report).model_dump(mode="json")
            return JSONResponse(
                content=safe_report,
                headers={"Content-Disposition": f'attachment; filename="data2agent-validation-{run_id}.json"'},
            )
        finally:
            db.con.close()

    @api.get(
        "/quarantine",
        response_model=list[QuarantineRecord],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409],
                   422: _RESP_HTTP_ERROR[422]},
    )
    def quarantine(
        response: Response,
        source: str | None = None,
        object: str | None = None,
        reason: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """隔离列表:数组 wire shape + X-Total-Count 响应头(M5)。

        支持 source/object 精确匹配、reason 子串搜索;分页(默认 50,上限 100);
        排序固定 id DESC;不含 raw_json;created_at 规范化为带时区 datetime。
        """
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "limit 须为 1..100,offset 须 >= 0")
        where = ["resolved_at IS NULL"]
        params: list[Any] = []
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if object is not None:
            where.append("object = ?")
            params.append(object)
        if reason is not None:
            reason_clean = str(reason).strip()[:200]
            where.append("reason LIKE ?")
            params.append(f"%{reason_clean}%")
        wsql = " WHERE " + " AND ".join(where)
        db = store()
        (total,) = db.con.execute(
            f"SELECT COUNT(*) FROM d2a_quarantine{wsql}", params).fetchone()
        response.headers["X-Total-Count"] = str(total)
        rows = db.con.execute(
            f"SELECT id, source, object, keys_json, reason, batch_id, created_at "
            f"FROM d2a_quarantine{wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset])
        result: list[dict] = []
        now = obs.now_aware()
        pack = state.get("pack")
        for r in rows:
            warnings: list[str] = []
            keys: dict[str, Any] | None = None
            keys_json_out: str | None = None  # 默认 null:解析失败/非 dict 不回退到原始敏感串
            if r["keys_json"]:
                try:
                    parsed = json.loads(r["keys_json"])
                    if isinstance(parsed, dict):
                        # 对 keys 做服务端脱敏,与 detail 端点一致
                        keys, _key_trunc = br._sanitize_object_keys(
                            pack, r["source"], r["object"], parsed)
                        keys_json_out = json.dumps(keys, ensure_ascii=False, default=str)
                    else:
                        keys = None
                        keys_json_out = None
                        warnings.append("keys_json 解析值不是 JSON 对象")
                except (json.JSONDecodeError, TypeError):
                    keys = None
                    keys_json_out = None
                    warnings.append("keys_json 解析失败")
            created = obs.aware(r["created_at"])
            age = None
            if created is not None:
                age = int((now - created).total_seconds())
            else:
                warnings.append(f"created_at 解析失败: {r['created_at']!r}")
                created = now  # 保底值,不影响 wire shape 校验
            result.append({
                "id": r["id"],
                "source": r["source"],
                "object": r["object"],
                "keys_json": keys_json_out,
                "keys": keys,
                "reason": br._sanitize_quarantine_reason(
                    r["reason"], pack, r["source"], r["object"]) or "",
                "batch_id": r["batch_id"],
                "created_at": created,
                "age_seconds": age,
                "warnings": warnings,
            })
        return result

    @api.get(
        "/quarantine/groups",
        response_model=list[QuarantineGroup],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def quarantine_groups(source: str | None = None) -> list[dict]:
        """隔离分组摘要:按 (source, object) 聚合未处理隔离(M5)。

        包含模板显示名、隔离率、熔断/数据新鲜度状态;未知 source/object 不省略,
        以 display_name=null + 警告保留为事实。
        """
        db = store()
        pack = state.get("pack")
        where = "WHERE resolved_at IS NULL"
        gparams: list[Any] = []
        if source is not None:
            where += " AND source = ?"
            gparams.append(source)
        groups = db.con.execute(
            f"SELECT source, object, COUNT(*) AS pending, MAX(created_at) AS latest_created_at "
            f"FROM d2a_quarantine {where} "
            f"GROUP BY source, object ORDER BY source, object", gparams).fetchall()
        result: list[dict] = []
        with published_read_tx(db):
            for g in groups:
                src = g["source"]
                obj = g["object"]
                warnings: list[str] = []
                # latest record (batch_id, reason)
                latest = db.con.execute(
                    "SELECT batch_id, reason FROM d2a_quarantine "
                    "WHERE source = ? AND object = ? AND resolved_at IS NULL "
                    "ORDER BY id DESC LIMIT 1", (src, obj)).fetchone()
                # display_name from template pack
                display_name = None
                if pack is not None:
                    tpl = next((o for o in pack.objects if o.object == obj), None)
                    if tpl is not None:
                        display_name = tpl.display_name
                # quarantine rate from most recent apply object step (按源隔离)
                quarantine_rate = None
                latest_apply_run_id = None
                step_aborted = False
                step = db.con.execute(
                    "SELECT s.id, s.run_id, s.status, s.quarantined, s.rows_in "
                    "FROM d2a_run_step s "
                    "JOIN d2a_sync_run r ON s.run_id = r.id "
                    "WHERE s.kind = 'object' AND s.target = ? "
                    "AND r.source = ? AND r.run_type = 'apply' "
                    "ORDER BY s.id DESC LIMIT 1", (obj, src)).fetchone()
                if step is not None:
                    latest_apply_run_id = step["run_id"]
                    step_aborted = step["status"] == "aborted"
                    if step["rows_in"] and step["rows_in"] > 0:
                        quarantine_rate = (step["quarantined"] or 0) / step["rows_in"]
                rate_state = _compute_rate_state(quarantine_rate, DEFAULT_BREAKER_THRESHOLD)
                # published 快照物理表(不回退遗留 obj_*)
                object_rows = None
                mapped_at = None
                table_exists = False
                table_ok = False
                try:
                    physical, _ov = br.resolve_published_object(db, src, obj)
                    table_exists = True
                    try:
                        cols = {r[1] for r in db.con.execute(
                            f'PRAGMA table_info("{physical}")')}
                        if "_d2a_mapped_at" in cols:
                            table_ok = True
                    except sqlite3.Error:
                        pass
                    if table_ok:
                        try:
                            row = db.con.execute(
                                f'SELECT COUNT(*) AS n, MAX("_d2a_mapped_at") AS m '
                                f'FROM "{physical}"'
                            ).fetchone()
                            object_rows = row["n"]
                            mapped_at = obs.aware(row["m"])
                        except sqlite3.Error:
                            warnings.append("对象数据查询失败")
                except br.BrowseError:
                    pass
                # 收集该对象在此源的 binding 表集合(supply serving_state 用)
                binding_tables: list[str] | None = None
                if pack is not None:
                    tpl = next((o for o in pack.objects if o.object == obj), None)
                    if tpl is not None:
                        bt_list: list[str] = []
                        for binding in tpl.bindings:
                            if binding.enabled and binding.source == src:
                                bt_list.extend(binding.tables)
                        if bt_list:
                            binding_tables = bt_list
                serving_state = _compute_serving_state(
                    db, table_exists, table_ok, object_rows, mapped_at,
                    src, latest_apply_run_id, step_aborted, binding_tables)
                if serving_state == "unavailable":
                    warnings.append("对象表存在但结构不完整或无法读取")
                # ---- retry_allowed 条件 ----
                retry_allowed: bool = True
                retry_disabled_reason: str | None = None
                cfg = state.get("config")
                if cfg is None:
                    retry_allowed = False
                    retry_disabled_reason = "只读模式"
                elif src not in _known_sources():
                    retry_allowed = False
                    retry_disabled_reason = "未知数据源"
                else:
                    tpl_b = next((o for o in pack.objects if o.object == obj), None) if pack else None
                    if tpl_b is None:
                        retry_allowed = False
                        retry_disabled_reason = "模板未识别此对象，无法重试"
                    elif not any(
                        b.enabled and b.source == src for b in tpl_b.bindings
                    ):
                        retry_allowed = False
                        retry_disabled_reason = "无启用的映射绑定"
                result.append({
                    "source": src,
                    "object": obj,
                    "display_name": display_name,
                    "pending": g["pending"],
                    "latest_created_at": obs.aware(g["latest_created_at"]),
                    "latest_batch_id": latest["batch_id"] if latest else None,
                    "latest_reason": (
                        br._sanitize_quarantine_reason(
                            latest["reason"], pack, src, obj)
                        if latest and latest["reason"] else None
                    ),
                    "quarantine_rate": quarantine_rate,
                    "breaker_threshold": DEFAULT_BREAKER_THRESHOLD,
                    "rate_state": rate_state,
                    "serving_state": serving_state,
                    "latest_apply_run_id": latest_apply_run_id,
                    "object_rows": object_rows,
                    "mapped_at": mapped_at,
                    "retry_allowed": retry_allowed,
                    "retry_disabled_reason": retry_disabled_reason,
                    "warnings": warnings,
                })
        return result

    @api.get(
        "/quarantine/{id}",
        response_model=QuarantineDetail,
        responses={
            401: _RESP_HTTP_ERROR[401],
            403: _RESP_HTTP_ERROR[403],
            404: {"model": HttpError},
            409: _RESP_HTTP_ERROR[409],
            500: _RESP_HTTP_ERROR[500],
        },
    )
    def quarantine_detail(id: int, request: Request) -> dict:
        """隔离详情(M5-T03):强制 Bearer auth + raw 脱敏预览。

        列表/分组端点从不返回 raw;这是查看隔离原始数据的唯一入口。
        每次请求(允许/拒绝)均写入不泄密访问审计;审计失败 → 请求失败关闭。
        已处理记录(resolved_at 非空)返回 404,与不存在记录同语义。
        """
        request_id = str(uuid.uuid4())
        db = store()

        # ---- 强制 Bearer 认证 ----
        tok = state["token"]
        if not tok:
            db.log_access(
                subject="anonymous", resource_type="quarantine_raw",
                source=None, resource=str(id), allowed=False,
                reason_code="token_not_configured", request_id=request_id)
            raise HTTPException(403, "隔离详情需配置控制台 Token 并显式认证")

        supplied = _auth_supplied(request)
        if supplied != tok:
            db.log_access(
                subject="anonymous", resource_type="quarantine_raw",
                source=None, resource=str(id), allowed=False,
                reason_code="unauthorized", request_id=request_id)
            raise HTTPException(401, "需要有效的管理界面登录密码")

        # ---- 查询(只取未处理记录) ----
        row = db.con.execute(
            "SELECT * FROM d2a_quarantine WHERE id = ? AND resolved_at IS NULL",
            (id,)).fetchone()

        if row is None:
            exists = db.con.execute(
                "SELECT 1 FROM d2a_quarantine WHERE id = ?", (id,)).fetchone()
            reason_code = "resolved" if exists else "not_found"
            db.log_access(
                subject="console-admin", resource_type="quarantine_raw",
                source=None, resource=str(id), allowed=False,
                reason_code=reason_code, request_id=request_id)
            raise HTTPException(404, (
                f"隔离记录 #{id} 已处理" if exists
                else f"隔离记录 #{id} 不存在"))

        source = row["source"]
        object_name = row["object"]
        pack = state.get("pack")

        # ---- 解析并脱敏 keys ----
        keys: dict[str, Any] | None = None
        keys_warnings: list[str] = []
        keys_json_out: str | None = None  # 默认 null:解析失败/非 dict 不回退到原始敏感串
        if row["keys_json"]:
            try:
                parsed = json.loads(row["keys_json"])
                if isinstance(parsed, dict):
                    keys, _key_trunc = br._sanitize_object_keys(
                        pack, source, object_name, parsed)
                    keys_json_out = json.dumps(keys, ensure_ascii=False, default=str)
                else:
                    keys = None
                    keys_json_out = None
                    keys_warnings.append("keys_json 解析值不是 JSON 对象")
            except (json.JSONDecodeError, TypeError):
                keys = None
                keys_json_out = None
                keys_warnings.append("keys_json 解析失败")

        # ---- 脱敏 raw_json ----
        raw_sanitized: dict[str, Any] | None = None
        raw_truncations: list[dict[str, Any]] = []
        if row["raw_json"]:
            try:
                raw_dict = json.loads(row["raw_json"])
                raw_sanitized, raw_truncations = br.sanitize_quarantine_raw(
                    raw_dict, pack, source, object_name)
                if raw_sanitized is None:
                    keys_warnings.append("raw_json 解析值不是 JSON 对象")
            except (json.JSONDecodeError, TypeError):
                keys_warnings.append("raw_json 解析失败")

        # ---- 时间与年龄 ----
        now = obs.now_aware()
        created = obs.aware(row["created_at"])
        age: int | None = None
        if created is not None:
            age = int((now - created).total_seconds())
        else:
            keys_warnings.append(f"created_at 解析失败: {row['created_at']!r}")
            created = now  # 保底值,不影响 wire shape 校验

        # ---- 写允许访问审计(失败) → fail-close ----
        try:
            db.log_access(
                subject="console-admin", resource_type="quarantine_raw",
                source=source, resource=str(id), allowed=True,
                reason_code="ok", request_id=request_id,
                returned_rows=1)
        except Exception as audit_error:
            raise HTTPException(500, "隔离详情访问审计写入失败") from audit_error

        return {
            "id": row["id"],
            "source": source,
            "object": object_name,
            "keys_json": keys_json_out,
            "keys": keys,
            "reason": br._sanitize_quarantine_reason(
                row["reason"], pack, source, object_name) or "",
            "batch_id": row["batch_id"],
            "created_at": created,
            "age_seconds": age,
            "warnings": keys_warnings,
            "raw": raw_sanitized,
            "truncations": raw_truncations,
            "request_id": request_id,
        }

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
        if resource_type is not None and resource_type not in (
            "raw", "object", "quarantine_raw"):
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
        version = {"app_version": __version__, "build_version": resolve_build_version()}
        if needs_setup():
            return {**version, "needs_setup": True, "templates": "", "landing": ""}
        if state["config"] is not None:
            cfg = state["config"]
        else:
            cfg = load_platform_config(require_config_path())
        out = _platform_config_subset(cfg)
        out.update({**version, "needs_setup": False})
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
            path, PLATFORM_EDITABLE, patch, validate=load_platform_config)
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
            403: {"model": McpLabError, "description": "档位禁止或其他治理拒绝"},
            404: {"model": McpLabError, "description": "未知对象/指标"},
            409: {"model": McpLabError, "description": "未配置/冲突/query 过期等"},
            422: {"model": McpLabError, "description": "参数无效"},
            429: {"model": McpLabError, "description": "限流"},
            500: {"model": McpLabError, "description": "执行失败"},
            502: {"model": McpLabError, "description": "上游 MCP 失败"},
            503: {"model": McpLabError, "description": "MCP 不可用"},
        },
    )
    def debug_mcp_call(
        body: McpCallBody,
        x_d2a_session_id: Annotated[
            str | None,
            Header(
                alias=_EVIDENCE_SESSION_HEADER,
                description="M5 MCP evidence session header; required on gateway query/proposal APIs",
            ),
        ] = None,
    ) -> dict | JSONResponse:
        validated_session = _require_evidence_session(x_d2a_session_id)
        if isinstance(validated_session, JSONResponse):
            return validated_session
        _ = validated_session
        if body.tool not in _MCP_TOOLS:
            raise HTTPException(400, f"工具 '{body.tool}' 不在白名单,可用:{sorted(_MCP_TOOLS)}")
        # model_dump 解开 JsonValue RootModel,避免 object=root='Customer' 一类参数污染
        params = body.model_dump().get("params") or {}
        try:
            return _mcp_in_process(
                body.tool,
                params,
                context=EvidenceContext(
                    principal="console:configured",
                    session_id=validated_session,
                    channel="console",
                ),
            )
        except (ValueError, TypeError) as e:
            # 业务/参数错误直接映射;不回落到远端 HTTP(远端 query ID 无法被本进程 proposal 引用)
            return mcp_lab_error_response(e, tool=body.tool)
        except HTTPException:
            raise
        except Exception:
            return JSONResponse(
                status_code=503,
                content=McpLabError(
                    detail="MCP 进程内查询不可用",
                    reason_code="mcp_unavailable",
                    tool=body.tool,
                    retryable=True,
                    error_id=None,
                ).model_dump(),
            )

    # ---- 动作(复用 connect 引擎,窗口 / 白名单原样生效)----

    @api.post(
        "/actions/apply",
        response_model=ApplyActionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            409: _RESP_HTTP_ERROR[409],
        },
    )
    def action_apply(body: ApplyActionBody) -> dict:
        result = build_dataset(
            store(), require_pack(), body.source, auto_publish=body.publish,
        )
        if result.outcome == "conflict":
            raise HTTPException(
                409, result.error or result.reason_code or "数据集构建冲突",
            )
        return {
            "executed": True,
            "results": [asdict(r) for r in result.results],
            "aborted": [
                r.object for r in result.results
                if r.status in ("aborted", "failed")
            ],
            "dataset_version": result.dataset_version,
            "published": result.published,
            "previous_dataset_version": result.previous_dataset_version,
        }

    @api.post(
        "/actions/retry",
        response_model=RetryActionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError},
            409: {
                "model": RetryActionError,
                "description": "熔断(隔离率超阈值)或前置校验冲突(绑定不存在/已禁用)",
            },
            422: _RESP_HTTP_ERROR[422],
            500: {
                "model": RetryActionError,
                "description": "执行失败或观测写入失败",
            },
        },
    )
    def action_retry(body: ActionBody) -> dict:
        if not body.object:
            raise HTTPException(422, "retry 需要 object 参数")
        pack = require_pack()
        tpl = next((o for o in pack.objects if o.object == body.object), None)
        if tpl is None:
            raise HTTPException(404, f"未知对象 '{body.object}'")

        # ---- 前置校验(before creating Run)----
        cfg = state["config"]
        if cfg is None:
            return JSONResponse(
                status_code=409,
                content=RetryActionError(
                    detail="只读模式，重试不可用",
                    reason_code="preflight_failed",
                    executed=False,
                    object=body.object,
                    status="aborted",
                ).model_dump())
        if body.source not in _known_sources():
            raise HTTPException(
                404, f"未知来源 '{body.source}',可用:{_known_sources()}")

        bindings = [b for b in tpl.bindings if b.source == body.source]
        if not bindings:
            return JSONResponse(
                status_code=409,
                content=RetryActionError(
                    detail=f"对象 '{body.object}' 在源 '{body.source}' 没有绑定",
                    reason_code="preflight_failed",
                    executed=False,
                    object=body.object,
                    status="aborted",
                ).model_dump())
        if not any(b.enabled for b in bindings):
            return JSONResponse(
                status_code=409,
                content=RetryActionError(
                    detail=f"对象 '{body.object}' 在源 '{body.source}' 的绑定已禁用",
                    reason_code="preflight_failed",
                    executed=False,
                    object=body.object,
                    status="aborted",
                ).model_dump())

        # ---- 完整数据集重建 + 自动发布(object 仅定位/审计/结果聚焦)----
        db = store()
        try:
            result = build_dataset(db, pack, body.source, auto_publish=True)
        except Exception:
            error_id = str(uuid.uuid4())
            return JSONResponse(
                status_code=500,
                content=RetryActionError(
                    detail=f"重试执行失败: {body.object}",
                    reason_code="execution_failed",
                    executed=True,
                    object=body.object,
                    status="failed",
                    error_id=error_id,
                ).model_dump())

        focus = next((r for r in result.results if r.object == body.object), None)
        run_id = result.run_id
        step_id = result.step_ids.get(body.object) if result.step_ids else None
        detail_path = f"/api/runs/{run_id}" if run_id is not None else None

        if result.outcome == "conflict":
            conflict_code = result.reason_code or "preflight_failed"
            if conflict_code not in (
                "preflight_failed", "active_build",
                "empty_manifest", "empty_field_map",
            ):
                conflict_code = "preflight_failed"
            return JSONResponse(
                status_code=409,
                content=RetryActionError(
                    detail=result.error or result.reason_code or "数据集构建冲突",
                    reason_code=conflict_code,  # type: ignore[arg-type]
                    executed=False,
                    object=body.object,
                    status="aborted",
                    run_id=run_id,
                    step_id=step_id,
                    detail_path=detail_path,
                    error_id=result.error_id,
                ).model_dump())

        aborted = focus is not None and focus.status == "aborted"
        failed_focus = focus is not None and focus.status == "failed"
        if result.outcome != "ok" or not result.published or aborted or failed_focus:
            if aborted:
                reason = "circuit_broken"
                status_code = 409
                err_status = "aborted"
            else:
                reason = "execution_failed"
                status_code = 500
                err_status = "failed"
            return JSONResponse(
                status_code=status_code,
                content=RetryActionError(
                    detail=(
                        f"重试触发熔断: {body.object} 隔离率 "
                        f"{focus.quarantined}/{focus.total}"
                        if aborted and focus is not None
                        else (result.error or f"重试执行失败: {body.object}")
                    ),
                    reason_code=reason,  # type: ignore[arg-type]
                    executed=True,
                    object=body.object,
                    total=focus.total if focus else None,
                    mapped=focus.mapped if focus else None,
                    quarantined=focus.quarantined if focus else None,
                    status=err_status,  # type: ignore[arg-type]
                    run_id=run_id,
                    step_id=step_id,
                    detail_path=detail_path,
                    error_id=result.error_id,
                ).model_dump())

        if focus is None or run_id is None or step_id is None:
            error_id = str(uuid.uuid4())
            return JSONResponse(
                status_code=500,
                content=RetryActionError(
                    detail="写入 run 观测记录失败",
                    reason_code="observation_failed",
                    executed=True,
                    object=body.object,
                    status="failed",
                    run_id=run_id,
                    step_id=step_id,
                    detail_path=detail_path,
                    error_id=error_id,
                ).model_dump())

        return JSONResponse(
            status_code=200,
            content=RetryActionResult(
                executed=True,
                object=body.object,
                total=focus.total,
                mapped=focus.mapped,
                quarantined=focus.quarantined,
                status="ok",
                run_id=run_id,
                step_id=step_id,
                detail_path=f"/api/runs/{run_id}",
                dataset_version=result.dataset_version,
            ).model_dump())

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

    # ---- v0.3 datasets(M1 只读;M2-T06 publish/rollback 原子引擎)----

    def _map_dataset_mutation(result) -> DatasetActionResult:
        if result.outcome in ("ok", "idempotent"):
            return DatasetActionResult(
                executed=result.executed,
                dataset_version=result.dataset_version,
                note=result.note or "",
            )
        if result.outcome == "not_found":
            raise HTTPException(404, f"数据集版本 {result.dataset_version} 不存在")
        if result.outcome == "conflict":
            detail = result.reason_code or result.note or "冲突"
            raise HTTPException(409, detail)
        error_id = result.error_id or uuid.uuid4().hex[:12]
        raise HTTPException(
            500, f"数据集发布操作失败(error_id={error_id})",
        )

    @api.get(
        "/datasets",
        response_model=list[DatasetSummary],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409],
                   422: _RESP_HTTP_ERROR[422], 500: _RESP_HTTP_ERROR[500]},
        tags=["v0.3"],
    )
    def datasets_list(
        response: Response,
        source: str | None = None,
        status: DatasetStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """数据集版本列表:数组 wire shape + X-Total-Count;空库返回 [] 不伪造版本。"""
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "limit 须为 1..100,offset 须 >= 0")
        rows, total = store().list_dataset_versions(
            source=source, status=status, limit=limit, offset=offset)
        response.headers["X-Total-Count"] = str(total)
        return [_map_dataset_summary(r) for r in rows]

    @api.get(
        "/datasets/{version}",
        response_model=DatasetDetail,
        responses={401: _RESP_HTTP_ERROR[401], 404: {"model": HttpError},
                   409: _RESP_HTTP_ERROR[409], 500: _RESP_HTTP_ERROR[500]},
        tags=["v0.3"],
    )
    def datasets_detail(version: str) -> dict:
        """数据集版本详情(含对象版本);不存在返回 404。"""
        record = store().get_dataset_version(version)
        if record is None:
            raise HTTPException(404, f"数据集版本 {version} 不存在")
        objects = store().list_object_versions(version)
        return {
            **_map_dataset_summary(record),
            "objects": [_map_object_version(o) for o in objects],
        }

    @api.post(
        "/datasets/{version}/publish",
        response_model=DatasetActionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError, "description": "候选版本不存在"},
            409: _RESP_HTTP_ERROR[409],
            500: _RESP_HTTP_ERROR[500],
        },
        tags=["v0.3"],
    )
    def datasets_publish(version: str) -> DatasetActionResult:
        """原子发布候选数据集版本。"""
        return _map_dataset_mutation(publish_dataset(store(), version))

    @api.post(
        "/datasets/{version}/rollback",
        response_model=DatasetActionResult,
        responses={
            401: _RESP_HTTP_ERROR[401],
            404: {"model": HttpError, "description": "目标版本不存在"},
            409: _RESP_HTTP_ERROR[409],
            500: _RESP_HTTP_ERROR[500],
        },
        tags=["v0.3"],
    )
    def datasets_rollback(version: str) -> DatasetActionResult:
        """回滚到直接上一稳定版本。"""
        return _map_dataset_mutation(rollback_dataset(store(), version))

    # ---- v0.3 M3-T05: mapping preview API(强制 Bearer + 审计 + 只读求值)----

    def _preview_error_response(
        status: int,
        reason_code: MappingPreviewErrorReasonCode | str,
        detail: str,
        *,
        error_id: str | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=MappingPreviewError(
                status=status,
                reason_code=reason_code,  # type: ignore[arg-type]
                detail=detail,
                error_id=error_id,
            ).model_dump(),
        )

    def _preview_audit_resource(
        object_name: str, body: MappingPreviewRequest, pack,
    ) -> str:
        """审计 resource=`mapping_preview:{object}:{anchor}`;锚未知时用 __unknown__。"""
        anchor = "__unknown__"
        if body.draft_binding is not None and body.draft_binding.tables:
            anchor = body.draft_binding.tables[0]
        elif pack is not None:
            tpl = next((o for o in pack.objects if o.object == object_name), None)
            if tpl is not None:
                binding = next(
                    (b for b in tpl.bindings if b.source == body.source), None)
                if binding is not None and binding.tables:
                    anchor = binding.tables[0]
        return f"mapping_preview:{object_name}:{anchor}"

    def _preview_safe_detail(reason_code: str) -> str:
        """对外固定安全摘要;不回传异常原文/SQL/traceback。"""
        return {
            "unauthorized": "需要有效的管理界面登录密码",
            "token_not_configured": "mapping preview 需配置控制台 Token 并显式认证",
            "object_not_found": "对象不存在",
            "source_not_found": "数据源不存在或不允许",
            "raw_table_not_found": "锚表尚未落地",
            "sample_batch_not_found": "指定批次不存在",
            "current_binding_unavailable": "无当前 binding 且未提供草稿",
            "raw_unavailable": "raw 数据暂不可用",
            "draft_invalid": "草稿不合法",
            "sample_invalid": "样本参数不合法",
            "anchor_changed": "样本锚已变化,请重试",
            "preview_failed": "映射预览失败",
        }.get(reason_code, "映射预览失败")

    @api.post(
        "/mappings/{object}/preview",
        response_model=MappingPreviewResponse,
        responses={
            401: {
                "model": MappingPreviewError,
                "description": "Bearer 错误或缺失(unauthorized)",
            },
            403: {
                "model": MappingPreviewError,
                "description": "未配置 Token(token_not_configured)",
            },
            404: {
                "model": MappingPreviewError,
                "description": (
                    "object_not_found / source_not_found / "
                    "raw_table_not_found / sample_batch_not_found"
                ),
            },
            409: {
                "model": MappingPreviewError,
                "description": "current_binding_unavailable / raw_unavailable",
            },
            422: {
                "model": RequestError | MappingPreviewError,
                "description": (
                    "Pydantic 请求校验(RequestError),或语义错误 "
                    "draft_invalid / sample_invalid / anchor_changed"
                    "(MappingPreviewError.reason_code)"
                ),
            },
            500: {
                "model": MappingPreviewError,
                "description": "preview_failed + error_id",
            },
        },
        tags=["v0.3"],
    )
    def mappings_preview(
        object: str,
        body: MappingPreviewRequest,
        request: Request,
    ) -> MappingPreviewResponse | JSONResponse:
        """映射 Preview:只读样本试算,不写业务表。

        鉴权/审计走可写 store(log_access);求值走 LandingStore.open_readonly()
        快照连接,避免 preview 路径持有写连接或触发 DDL。
        """
        request_id = uuid.uuid4().hex
        db = store()
        pack = require_pack()
        resource = _preview_audit_resource(object, body, pack)
        offset = body.sample.offset
        limit = body.sample.limit

        # 强制 Bearer(与 raw browse 同纪律;不接受 ?token=)
        tok = state["token"]
        if not tok:
            db.log_access(
                subject="anonymous", resource_type="raw", source=body.source,
                resource=resource, allowed=False,
                reason_code="token_not_configured",
                page_offset=offset, page_limit=limit, request_id=request_id)
            return _preview_error_response(
                403, "token_not_configured",
                _preview_safe_detail("token_not_configured"))
        if _auth_supplied(request) != tok:
            db.log_access(
                subject="anonymous", resource_type="raw", source=body.source,
                resource=resource, allowed=False, reason_code="unauthorized",
                page_offset=offset, page_limit=limit, request_id=request_id)
            return _preview_error_response(
                401, "unauthorized", _preview_safe_detail("unauthorized"))

        cfg = state["config"]
        allowed = (
            br.allowed_sources(pack, _known_sources())
            if cfg is not None
            else None
        )
        draft = (
            body.draft_binding.model_dump() if body.draft_binding is not None else None
        )

        # 只读求值连接与审计写连接分离:preview 不借主 store 写事务。
        ro = LandingStore.open_readonly(db.db_path)
        try:
            try:
                result = preview_mapping(
                    ro,
                    pack,
                    object_name=object,
                    source=body.source,
                    offset=offset,
                    limit=limit,
                    batch_id=body.sample.batch_id,
                    draft_binding=draft,
                    allowed_sources=allowed,
                )
            except PreviewError as e:
                reason = str(e.reason_code)
                status = _PREVIEW_HTTP_STATUS.get(reason, 500)
                if status == 500:
                    reason = "preview_failed"
                try:
                    db.log_access(
                        subject="console-admin", resource_type="raw",
                        source=body.source, resource=resource, allowed=False,
                        reason_code=reason,
                        page_offset=offset, page_limit=limit,
                        request_id=request_id)
                except Exception as audit_error:
                    raise HTTPException(
                        500, "mapping preview 失败且访问审计写入失败",
                    ) from audit_error
                error_id = None
                if status == 500:
                    error_id = hashlib.sha256(
                        f"{reason}:{e.detail}".encode()).hexdigest()[:12]
                return _preview_error_response(
                    status, reason, _preview_safe_detail(reason),
                    error_id=error_id)
            except Exception as e:
                error_id = hashlib.sha256(
                    f"preview_failed:{type(e).__name__}".encode()
                ).hexdigest()[:12]
                try:
                    db.log_access(
                        subject="console-admin", resource_type="raw",
                        source=body.source, resource=resource, allowed=False,
                        reason_code="preview_failed",
                        page_offset=offset, page_limit=limit,
                        request_id=request_id)
                except Exception as audit_error:
                    raise HTTPException(
                        500, "mapping preview 失败且访问审计写入失败",
                    ) from audit_error
                return _preview_error_response(
                    500, "preview_failed",
                    _preview_safe_detail("preview_failed"),
                    error_id=error_id)
        finally:
            ro.con.close()

        # 成功审计用结果中的真实锚表刷新 resource
        resource = f"mapping_preview:{result.object}:{result.sample.anchor_table}"
        try:
            db.log_access(
                subject="console-admin", resource_type="raw", source=result.source,
                resource=resource, allowed=True, reason_code="preview_allowed",
                page_offset=result.sample.offset, page_limit=result.sample.limit,
                returned_rows=result.sample.sampled_rows, request_id=request_id)
        except Exception as audit_error:
            raise HTTPException(
                500, "mapping preview 访问审计写入失败",
            ) from audit_error

        return MappingPreviewResponse.model_validate(asdict(result))


    # ---- v0.2 数据浏览与模板(已实现)----
    # 历史注释曾把下列端点标为契约桩;M4–M6 已落地,publish/rollback 已由 T06 接通。

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
                db, pack, br.allowed_sources(pack, _known_sources()))
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
                br.allowed_sources(pack, _known_sources()), db, source)
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
        """对象目录:模板 ∩ published 快照;未发布为 rows=null + warning,不伪装 0。"""
        db = store()
        pack = require_pack()
        src = default_source()
        with published_read_tx(db):
            stats = obs.object_stats(db, pack, src)
            out = []
            for tpl in pack.objects:
                st = stats.get(tpl.object, {})
                rows = st.get("rows")
                mapped_at = st.get("mapped_at")
                warning = None
                if st.get("error"):
                    warning = st["error"]
                elif rows is None:
                    warning = "尚未发布"
                out.append({
                    "object": tpl.object,
                    "display_name": tpl.display_name,
                    "domain": tpl.domain,
                    "rows": rows,
                    "mapped_at": mapped_at,
                    "quarantined": st.get("quarantined") or 0,
                    "version": st.get("version"),
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
            409: {"model": HttpError, "description": "模板存在但尚未发布"},
            422: _RESP_HTTP_ERROR[422],
        },
    )
    def object_rows(object: str, offset: int = 0,
                    limit: int = 50, q: str = "") -> dict:
        """对象分页浏览(敏感属性服务端永久脱敏);读 published 快照物理表。"""
        db = store()
        with published_read_tx(db):
            try:
                snap = resolve_published_snapshot(db, default_source())
            except PublishedSnapshotError as e:
                if e.reason_code == "not_published":
                    raise HTTPException(409, "对象尚未发布") from e
                raise HTTPException(409, e.detail) from e
            tpl = next(
                (o for o in snap.template_pack.objects if o.object == object), None,
            )
            entry = snap.objects.get(object)
            if tpl is None or entry is None:
                raise HTTPException(404, f"未知对象 '{object}'")
            try:
                physical = validate_build_table(entry.physical_table)
            except ValueError as e:
                raise HTTPException(409, "数据集快照不可用") from e
            cols = br.object_column_meta(db, tpl, physical)
            try:
                page = br.browse_table(
                    db, physical, cols,
                    limit=limit, offset=offset, q=q)
            except br.BrowseError as e:
                raise HTTPException(e.status, e.detail) from e
            warnings = [f"列 {c['name']} 分类未知,按未确认处理展示"
                        for c in cols if c["classification"] == "unknown"]

            # M4-T09: lineage_refs 与 rows 对齐
            lineage_refs: list[dict] = []
            obj_vers = db.list_object_versions(snap.dataset_version)
            obj_ver = next(
                (o for o in obj_vers if o.object == object), None,
            )
            if (
                obj_ver is not None
                and obj_ver.lineage_schema_version is not None
            ):
                from ..connect.field_lineage import (
                    canonical_object_key_json,
                    object_key_token,
                )
                for idx, row in enumerate(page["rows"]):
                    try:
                        key_vals = {k: row.get(k) for k in tpl.keys}
                        token = object_key_token(tpl.keys, key_vals)
                        key_json = canonical_object_key_json(
                            tpl.keys, key_vals,
                        )
                        lineage_refs.append({
                            "row_index": idx,
                            "key_token": token,
                            "object_key": json.loads(key_json),
                        })
                    except Exception:
                        lineage_refs.append({
                            "row_index": idx,
                            "key_token": "",
                            "object_key": [],
                        })

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
                "lineage_refs": lineage_refs,
                "generated_at": datetime.now().astimezone(),
            }

    # ---- v0.3 M4: field lineage API ----

    class _TxnExit(Exception):
        """正常退出 published_read_tx;不是错误。"""

    def _lineage_error_response(
        status: int,
        reason_code: str,
        detail: str,
        *,
        error_id: str | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content=ObjectLineageError(
                status=status,
                reason_code=reason_code,  # type: ignore[arg-type]
                detail=detail,
                error_id=error_id,
            ).model_dump(),
        )

    def _lineage_safe_detail(reason_code: str) -> str:
        return {
            "unauthorized": "需要有效的管理界面登录密码",
            "token_not_configured": "字段血缘需配置控制台 Token 并显式认证",
            "object_not_found": "对象不存在",
            "field_not_found": "字段不存在",
            "record_not_found": "记录不存在",
            "dataset_not_published": "数据集尚未发布",
            "snapshot_corrupt": "数据集快照不可用",
            "lineage_incomplete": "字段血缘不完整",
            "lineage_key_invalid": "key token 必须是规范 64 位小写十六进制 SHA-256",
            "lineage_query_failed": "字段血缘查询失败",
        }.get(reason_code, "字段血缘查询失败")

    def _lineage_audit_resource(object_name: str, key_token: str) -> str:
        prefix = key_token[:12] if key_token else "__invalid__"
        return f"field_lineage:{object_name}:{prefix}"

    @api.get(
        "/objects/{object}/{key}/lineage",
        response_model=ObjectLineageResponse,
        responses={
            401: {
                "model": ObjectLineageError,
                "description": "Bearer 错误或缺失(unauthorized)",
            },
            403: {
                "model": ObjectLineageError,
                "description": "未配置 Token(token_not_configured)",
            },
            404: {
                "model": ObjectLineageError,
                "description": (
                    "object_not_found / field_not_found / record_not_found"
                ),
            },
            409: {
                "model": ObjectLineageError,
                "description": (
                    "dataset_not_published / snapshot_corrupt / lineage_incomplete"
                ),
            },
            422: {
                "model": RequestError | ObjectLineageError,
                "description": (
                    "Pydantic 校验(RequestError),或 lineage_key_invalid"
                    "(ObjectLineageError.reason_code)"
                ),
            },
            500: {
                "model": ObjectLineageError,
                "description": "lineage_query_failed + error_id",
            },
        },
        tags=["v0.3"],
    )
    def object_field_lineage(
        object: str,
        key: str,
        request: Request,
        property_name: str | None = Query(
            default=None,
            alias="property",
            description="可选:只返回单个模板属性;不传则返回全部字段",
        ),
    ) -> ObjectLineageResponse | JSONResponse:
        """对象字段血缘:published snapshot 只读查询。

        T01 冻结契约与 key token 校验;完整查询在 T07 接入。未实现前对合法
        key 返回 501 fail-closed,不得伪装成功体或空字段。
        """
        request_id = uuid.uuid4().hex
        db = store()
        resource = _lineage_audit_resource(object, key)

        tok = state["token"]
        if not tok:
            db.log_access(
                subject="anonymous", resource_type="object", source=None,
                resource=resource, allowed=False,
                reason_code="token_not_configured", request_id=request_id)
            return _lineage_error_response(
                403, "token_not_configured",
                _lineage_safe_detail("token_not_configured"))
        if _auth_supplied(request) != tok:
            db.log_access(
                subject="anonymous", resource_type="object", source=None,
                resource=resource, allowed=False, reason_code="unauthorized",
                request_id=request_id)
            return _lineage_error_response(
                401, "unauthorized", _lineage_safe_detail("unauthorized"))

        try:
            require_lineage_key_token(key)
        except LineageKeyError as e:
            db.log_access(
                subject="console-admin", resource_type="object", source=None,
                resource=resource, allowed=False,
                reason_code=e.reason_code, request_id=request_id)
            return _lineage_error_response(
                _LINEAGE_HTTP_STATUS.get(e.reason_code, 422),
                e.reason_code,
                _lineage_safe_detail(e.reason_code))

        # ---- T07: published snapshot 只读查询 ----
        # P1-4: 对象/属性/脱敏全部使用 published 快照冻结模板,不用 live template
        # 审计在事务外写入(log_access 会 commit,不能在 read_tx 内调用)
        src = default_source()

        # 在只读事务内完成所有查询;事务外做审计和响应构建
        _audit_allowed = True
        _audit_rc = "preview_allowed"
        _error_resp: JSONResponse | None = None
        _unavailable_resp: ObjectLineageResponse | None = None
        nodes: list = []
        inputs_rows: list = []
        tpl = None  # 从 published 快照取得

        try:
            with published_read_tx(db):
                try:
                    snap = resolve_published_snapshot(db, src)
                except PublishedSnapshotError as e:
                    rc = (
                        "dataset_not_published"
                        if e.reason_code == "not_published"
                        else "snapshot_corrupt"
                    )
                    _audit_allowed = False
                    _audit_rc = rc
                    _error_resp = _lineage_error_response(
                        _LINEAGE_HTTP_STATUS.get(rc, 409),
                        rc, _lineage_safe_detail(rc))
                    raise _TxnExit()

                # P1-4: 从 published 快照取模板
                tpl = next(
                    (t for t in snap.template_pack.objects
                     if t.object == object),
                    None,
                )
                if tpl is None:
                    _audit_allowed = False
                    _audit_rc = "object_not_found"
                    _error_resp = _lineage_error_response(
                        404, "object_not_found",
                        _lineage_safe_detail("object_not_found"))
                    raise _TxnExit()

                if property_name is not None:
                    prop_names = {p.name for p in tpl.properties}
                    if property_name not in prop_names:
                        _audit_allowed = False
                        _audit_rc = "field_not_found"
                        _error_resp = _lineage_error_response(
                            404, "field_not_found",
                            _lineage_safe_detail("field_not_found"))
                        raise _TxnExit()

                entry = snap.objects.get(object)
                if entry is None:
                    _audit_allowed = False
                    _audit_rc = "object_not_found"
                    _error_resp = _lineage_error_response(
                        404, "object_not_found",
                        _lineage_safe_detail("object_not_found"))
                    raise _TxnExit()

                obj_versions = db.list_object_versions(snap.dataset_version)
                obj_ver = next(
                    (o for o in obj_versions if o.object == object), None,
                )

                if (
                    obj_ver is None
                    or obj_ver.lineage_schema_version is None
                ):
                    _unavailable_resp = ObjectLineageResponse(
                        state="unavailable",
                        reason_code="lineage_not_recorded",
                        source=src,
                        object=object,
                        display_name=tpl.display_name,
                        object_key=[],
                        key_token=key,
                        dataset_version=snap.dataset_version,
                        object_version=entry.object_version,
                        template_version=snap.template_version,
                        binding_hash=entry.binding_hash,
                        binding_status=None,
                        map_batch_id=None,
                        fields=[],
                        warnings=[
                            "该数据集版本未记录字段血缘"
                            "（发布于字段血缘功能上线前）",
                        ],
                        generated_at=datetime.now().astimezone(),
                    )
                    raise _TxnExit()

                # 先查全部节点校验完整性,再按 property 过滤
                all_nodes = db.get_field_lineage_by_key_hash(
                    snap.dataset_version, object, key,
                )
                if not all_nodes:
                    _audit_allowed = False
                    _audit_rc = "record_not_found"
                    _error_resp = _lineage_error_response(
                        404, "record_not_found",
                        _lineage_safe_detail("record_not_found"))
                    raise _TxnExit()

                # 校验字段集合与冻结模板完全一致(不仅比较数量)
                expected_set = {p.name for p in tpl.properties}
                actual_set = {n["property"] for n in all_nodes}
                if actual_set != expected_set:
                    _audit_allowed = False
                    _audit_rc = "lineage_incomplete"
                    _error_resp = _lineage_error_response(
                        409, "lineage_incomplete",
                        _lineage_safe_detail("lineage_incomplete"))
                    raise _TxnExit()

                if property_name is not None:
                    nodes = [
                        n for n in all_nodes
                        if n["property"] == property_name
                    ]
                else:
                    nodes = all_nodes

                inputs_rows = db.get_field_lineage_inputs_by_key_hash(
                    snap.dataset_version, object, key,
                    property_name=property_name,
                )
        except _TxnExit:
            pass  # 正常退出:审计和响应在事务外处理
        except (sqlite3.Error, OSError) as exc:
            error_id = hashlib.sha256(
                str(exc).encode("utf-8"),
            ).hexdigest()[:12]
            db.log_access(
                subject="console-admin", resource_type="object",
                source=src, resource=resource, allowed=False,
                reason_code="lineage_query_failed",
                request_id=request_id)
            return _lineage_error_response(
                500, "lineage_query_failed",
                _lineage_safe_detail("lineage_query_failed"),
                error_id=error_id)

        # 事务外审计
        db.log_access(
            subject="console-admin", resource_type="object",
            source=src, resource=resource, allowed=_audit_allowed,
            reason_code=_audit_rc, request_id=request_id)

        if _error_resp is not None:
            return _error_resp
        if _unavailable_resp is not None:
            return _unavailable_resp

        # ---- 构建响应(脱敏在出口统一处理) ----
        sensitive_props = {
            p.name for p in tpl.properties if p.sensitive
        }
        props_by_name = {p.name: p for p in tpl.properties}
        _MASK = "•••"

        def _mask_evidence(
            ev_json: str | None, prop_name: str,
        ) -> dict | None:
            if ev_json is None:
                return None
            try:
                ev = json.loads(ev_json)
            except (json.JSONDecodeError, TypeError):
                return None
            if prop_name in sensitive_props:
                return {
                    "kind": ev.get("kind", "scalar"),
                    "value": _MASK if ev.get("value") is not None else None,
                    "preview": (
                        _MASK if ev.get("preview") is not None else None
                    ),
                    "sha256": ev.get("sha256"),
                    "length": ev.get("length"),
                }
            return ev

        # 按 property 分组输入边
        inputs_by_prop: dict[str, list] = {}
        for ir in inputs_rows:
            inputs_by_prop.setdefault(ir["property"], []).append(ir)

        # 取第一个节点的公共版本信息
        first = nodes[0]
        key_json = first["object_key_json"]
        try:
            object_key_pairs = json.loads(key_json)
        except (json.JSONDecodeError, TypeError):
            object_key_pairs = []

        fields_out: list[dict] = []
        for node in nodes:
            prop_name = node["property"]
            prop = props_by_name.get(prop_name)
            display = prop.desc if prop else prop_name

            # 解析 steps
            steps_out: list[dict] = []
            try:
                raw_steps = json.loads(node["transform_steps_json"])
            except (json.JSONDecodeError, TypeError):
                raw_steps = []
            for s in raw_steps:
                step: dict = {"kind": s.get("kind", "read")}
                if s.get("before") is not None:
                    step["before"] = _mask_evidence(
                        json.dumps(
                            {"kind": "scalar", "value": s["before"]},
                            ensure_ascii=False,
                        ),
                        prop_name,
                    )
                if s.get("after") is not None:
                    step["after"] = _mask_evidence(
                        json.dumps(
                            {"kind": "scalar", "value": s["after"]},
                            ensure_ascii=False,
                        ),
                        prop_name,
                    )
                if s.get("map_hit") is not None:
                    step["map_hit"] = s["map_hit"]
                if s.get("coerce_type"):
                    step["coerce_type"] = s["coerce_type"]
                if s.get("derived_rule_index") is not None:
                    step["derived_rule_index"] = s["derived_rule_index"]
                if s.get("derived_when") is not None:
                    step["derived_when"] = s["derived_when"]
                steps_out.append(step)

            # 解析 inputs
            prop_inputs = inputs_by_prop.get(prop_name, [])
            inputs_out: list[dict] = []
            for ir in prop_inputs:
                inp: dict = {"role": ir["role"]}
                if ir["source_table"]:
                    inp["source_table"] = ir["source_table"]
                if ir["source_column"]:
                    inp["source_column"] = ir["source_column"]
                if ir["source_pk_json"]:
                    try:
                        pk = json.loads(ir["source_pk_json"])
                        inp["source_pk"] = (
                            [[k, v] for k, v in pk.items()]
                            if isinstance(pk, dict) else pk
                        )
                    except (json.JSONDecodeError, TypeError):
                        pass
                if ir["source_value_json"]:
                    inp["source_value"] = _mask_evidence(
                        ir["source_value_json"], prop_name,
                    )
                if ir["extract_batch_id"]:
                    inp["extract_batch_id"] = ir["extract_batch_id"]
                if ir["join_json"]:
                    try:
                        inp["join"] = json.loads(ir["join_json"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                inputs_out.append(inp)

            fields_out.append({
                "property": prop_name,
                "display_name": display,
                "final_value": _mask_evidence(
                    node["result_value_json"], prop_name,
                ),
                "state": node["trace_status"],
                "reason_code": node["unavailable_reason"],
                "steps": steps_out,
                "inputs": inputs_out,
            })

        return ObjectLineageResponse(
            state="available",
            reason_code=None,
            source=src,
            object=object,
            display_name=tpl.display_name,
            object_key=object_key_pairs,
            key_token=key,
            dataset_version=first["dataset_version"],
            object_version=first["object_version"],
            template_version=first["template_version"],
            binding_hash=first["binding_hash"],
            binding_status=first["binding_status"],
            map_batch_id=first["map_batch_id"],
            fields=fields_out,
            warnings=[],
            generated_at=datetime.now().astimezone(),
        )

    @api.get(
        "/templates",
        response_model=list[TemplateObject],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def templates_view() -> list[dict]:
        """模板只读展示:对象模板、属性、绑定、物化状态与隔离计数。

        枚举映射从 binding field_map 表达式中解析;派生决策表原样透出。
        物化状态按 published 快照物理表判定;无 published 不回退遗留 obj_*;
        查询失败返回 state=unknown + 警告,不伪装为未物化。
        """
        db = store()
        pack = require_pack()
        src = default_source()
        with published_read_tx(db):
            stats = obs.object_stats(db, pack, src)
            result: list[dict] = []
            for tpl in pack.objects:
                warnings: list[str] = []

                # -- properties --
                properties = []
                for p in tpl.properties:
                    properties.append({
                        "name": p.name,
                        "type": p.type,
                        "desc": p.desc,
                        "sensitive": p.sensitive,
                        "ref": p.ref,
                        "enum_values": p.enum_values,
                    })

                # -- bindings --
                bindings = []
                for b in tpl.bindings:
                    # Parse enum_map from field_map expressions
                    enum_map: dict[str, dict[str, str]] = {}
                    for prop_name, expr_str in b.field_map.items():
                        try:
                            fexpr = parse_field_expr(expr_str)
                            if fexpr.value_map:
                                enum_map[prop_name] = fexpr.value_map
                        except ValueError:
                            pass  # expression parse failure: no enum_map

                    # Convert derived decision tables
                    derived: dict[str, dict] = {}
                    for prop_name, df in b.derived.items():
                        rules = []
                        for rule in df.rules:
                            rules.append({
                                "when": rule.when,
                                "value": rule.value,
                            })
                        derived[prop_name] = {
                            "rules": rules,
                            "default": df.default,
                        }

                    bindings.append({
                        "source": b.source,
                        "tables": b.tables,
                        "status": b.status,
                        "key_map": b.key_map,
                        "field_map": b.field_map,
                        "watermark": b.watermark,
                        "notes": b.notes,
                        "enabled": b.enabled,
                        "enum_map": enum_map,
                        "derived": derived,
                    })

                relations = [{
                    "name": r.name,
                    "target": r.target,
                    "cardinality": r.cardinality,
                    "desc": r.desc,
                } for r in tpl.relations]

                # -- materialized lookup (published snapshot only) --
                st = stats.get(tpl.object, {})
                if st.get("error"):
                    materialized = {
                        "state": "unknown",
                        "source": None,
                        "rows": None,
                        "mapped_at": None,
                        "batch_id": None,
                        "warnings": [st["error"]],
                    }
                elif st.get("rows") is None:
                    materialized = {
                        "state": "not_materialized",
                        "source": None,
                        "rows": None,
                        "mapped_at": None,
                        "batch_id": None,
                        "warnings": [],
                    }
                else:
                    batch_id = None
                    try:
                        physical, _ov = br.resolve_published_object(db, src, tpl.object)
                        batch_row = db.con.execute(
                            f'SELECT DISTINCT "_d2a_batch_id" AS b '
                            f'FROM "{physical}" WHERE "_d2a_batch_id" IS NOT NULL '
                            f"LIMIT 2"
                        ).fetchall()
                        if len(batch_row) == 1 and batch_row[0]["b"]:
                            batch_id = batch_row[0]["b"]
                        elif len(batch_row) > 1:
                            warnings.append("对象表存在多个批次，无法确定物化来源")
                    except (br.BrowseError, sqlite3.Error):
                        pass
                    materialized = {
                        "state": "materialized",
                        "source": src,
                        "rows": st["rows"],
                        "mapped_at": st.get("mapped_at"),
                        "batch_id": batch_id,
                        "warnings": list(warnings),
                    }

                # -- quarantine_pending --
                qp = st.get("quarantined")
                if qp is None:
                    (qp,) = db.con.execute(
                        "SELECT COUNT(*) FROM d2a_quarantine "
                        "WHERE object = ? AND resolved_at IS NULL",
                        (tpl.object,)).fetchone()

                result.append({
                    "object": tpl.object,
                    "display_name": tpl.display_name,
                    "description": tpl.description,
                    "domain": tpl.domain,
                    "keys": tpl.keys,
                    "properties": properties,
                    "relations": relations,
                    "bindings": bindings,
                    "source_of_truth": tpl.source_of_truth,
                    "knowledge_refs": tpl.knowledge_refs,
                    "materialized": materialized,
                    "quarantine_pending": qp,
                    "warnings": warnings,
                })
            return result

    @api.get(
        "/templates/metrics",
        response_model=list[TemplateMetric],
        responses={401: _RESP_HTTP_ERROR[401], 409: _RESP_HTTP_ERROR[409]},
    )
    def templates_metrics() -> list[dict]:
        """指标只读展示:模板包内所有指标定义。

        calibration_state 按 status 映射:certified→calibrated, draft→uncalibrated,
        deprecated→deprecated。draft 指标表示"模板未声明完成现场校准"。
        """
        pack = require_pack()
        result: list[dict] = []
        for m in pack.metrics:
            calibration_state = {
                "certified": "calibrated",
                "draft": "uncalibrated",
                "deprecated": "deprecated",
            }.get(m.status, "uncalibrated")

            result.append({
                "metric": m.metric,
                "display_name": m.display_name,
                "status": m.status,
                "calibration_state": calibration_state,
                "formula": m.formula,
                "grain": m.grain,
                "dimensions": m.dimensions,
                "caveats": m.caveats,
                "freshness_sla": m.freshness_sla,
            })
        return result

    @api.post(
        "/gateway/proposals",
        response_model=ProposalResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            403: {"model": McpLabError, "description": "档位禁止"},
            404: {"model": McpLabError, "description": "未知对象/动作"},
            409: {"model": McpLabError, "description": "query 过期/配置冲突"},
            422: {"model": McpLabError, "description": "参数无效"},
            500: {"model": McpLabError, "description": "执行失败"},
        },
        tags=["v0.2"],
    )
    def gateway_proposals(
        body: ProposalRequest,
        x_d2a_session_id: Annotated[
            str | None,
            Header(
                alias=_EVIDENCE_SESSION_HEADER,
                description="M5 MCP evidence session header; required on gateway query/proposal APIs",
            ),
        ] = None,
    ) -> ProposalResponse | JSONResponse:
        """M5-T06:按 principal/session 校验已持久 query evidence,原子写 proposal snapshot。"""
        validated_session = _require_evidence_session(x_d2a_session_id)
        if isinstance(validated_session, JSONResponse):
            return validated_session
        try:
            svc = get_query_service()
        except HTTPException as e:
            if e.status_code == 409:
                return JSONResponse(
                    status_code=409,
                    content=McpLabError(
                        detail="尚未完成首次配置或落地库不可用",
                        reason_code="mcp_unavailable",
                        tool="propose_action",
                        retryable=False,
                        error_id=None,
                    ).model_dump(),
                )
            raise
        evidence = [
            {
                "claim": item.claim,
                "query_id": item.query_id,
                "result_digest": item.result_digest,
            }
            for item in body.evidence
        ]
        try:
            card = svc.propose_action(
                body.object,
                body.action,
                body.conclusion,
                evidence,
                context=_console_evidence_context(validated_session),
            )
            return ProposalResponse.model_validate(card)
        except ValueError as e:
            return mcp_lab_error_response(e, tool="propose_action")
        except Exception:
            return mcp_lab_error_response(
                RuntimeError("proposal failed"), tool="propose_action",
            )

    @api.get(
        "/gateway/queries/{query_id}",
        response_model=QueryEvidenceDetailResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            403: {"model": McpLabError, "description": "query evidence 属于其他主体"},
            404: {"model": McpLabError, "description": "query evidence 不存在"},
            409: {"model": McpLabError, "description": "query 过期或证据冲突"},
            422: {"model": McpLabError, "description": "session 或 query_id 非法"},
            500: {"model": McpLabError, "description": "证据存储不可用"},
        },
        tags=["v0.3"],
    )
    def gateway_query_detail(
        query_id: str,
        x_d2a_session_id: Annotated[
            str | None,
            Header(
                alias=_EVIDENCE_SESSION_HEADER,
                description="M5 MCP evidence session header; required on gateway query/proposal APIs",
            ),
        ] = None,
    ) -> QueryEvidenceDetailResponse | JSONResponse:
        validated_session = _require_evidence_session(x_d2a_session_id)
        if isinstance(validated_session, JSONResponse):
            return validated_session
        try:
            return _load_query_detail(
                query_id,
                context=_console_evidence_context(validated_session),
            )
        except ValueError as e:
            return mcp_lab_error_response(e, tool=None)
        except Exception:
            return mcp_lab_error_response(
                RuntimeError("query detail failed"), tool=None,
            )

    @api.get(
        "/gateway/proposals/{proposal_id}",
        response_model=ProposalResponse,
        responses={
            401: _RESP_HTTP_ERROR[401],
            403: {"model": McpLabError, "description": "proposal evidence 属于其他主体"},
            404: {"model": McpLabError, "description": "proposal evidence 不存在"},
            409: {"model": McpLabError, "description": "proposal evidence 冲突或完整性失败"},
            422: {"model": McpLabError, "description": "session 或 proposal_id 非法"},
            500: {"model": McpLabError, "description": "证据存储不可用"},
        },
        tags=["v0.3"],
    )
    def gateway_proposal_detail(
        proposal_id: str,
        x_d2a_session_id: Annotated[
            str | None,
            Header(
                alias=_EVIDENCE_SESSION_HEADER,
                description="M5 MCP evidence session header; required on gateway query/proposal APIs",
            ),
        ] = None,
    ) -> ProposalResponse | JSONResponse:
        validated_session = _require_evidence_session(x_d2a_session_id)
        if isinstance(validated_session, JSONResponse):
            return validated_session
        try:
            return _load_proposal_detail(
                proposal_id,
                context=_console_evidence_context(validated_session),
            )
        except ValueError as e:
            return mcp_lab_error_response(e, tool=None)
        except Exception:
            return mcp_lab_error_response(
                RuntimeError("proposal detail failed"), tool=None,
            )

    app.include_router(api)

    @app.middleware("http")
    async def _html_gate(request: Request, call_next):
        if request.url.path.startswith("/api"):
            return await call_next(request)
        response = await call_next(request)
        # Portable Windows environments may inherit a registry MIME mapping that
        # labels .js as text/plain.  Browsers then reject Vue ES modules before
        # the application can load.  Keep module asset types deterministic.
        if request.url.path.startswith("/assets/"):
            media_type = _VUE_MODULE_MEDIA_TYPES.get(
                Path(request.url.path).suffix.lower())
            if media_type and response.status_code == 200:
                response.headers["content-type"] = media_type
        return response

    # ---- Vue Console 根路径静态挂载与 SPA fallback ----
    vue_dist = resolve_vue_dist()
    app.state.vue_dist = vue_dist

    def _vue_missing() -> HTMLResponse:
        return HTMLResponse(_VUE_MISSING_HTML, status_code=503)

    if vue_dist is not None:
        assets_dir = vue_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="vue-assets")

    @app.get("/v1", include_in_schema=False)
    @app.get("/v1/", include_in_schema=False)
    def legacy_v1_index():
        return RedirectResponse("/setup" if needs_setup() else "/", status_code=302)

    @app.get("/v1/{full_path:path}", include_in_schema=False)
    def legacy_v1_spa(full_path: str, request: Request):
        target = f"/{full_path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=302)

    @app.get("/{full_path:path}", include_in_schema=False)
    def vue_console_spa(full_path: str):
        if vue_dist is None:
            return _vue_missing()
        # 已由 StaticFiles 处理 /assets/*;其余文件或 SPA 回退
        candidate = (vue_dist / full_path).resolve()
        try:
            candidate.relative_to(vue_dist.resolve())
        except ValueError:
            return _vue_missing()
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(vue_dist / "index.html")

    app.state.d2a_state = state
    return app
