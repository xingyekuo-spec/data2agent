"""控制台应用:FastAPI 单页 + JSON API + 运维动作。

安全:
- 只读视图直接查落地库;动作(sync / reconcile / apply / retry)复用 connect
  引擎,错峰窗口 / 白名单 / 只读适配器约束原样生效,控制台不开新的旁路;
- 可选 Bearer Token(--token 或环境变量 D2A_CONSOLE_TOKEN),内网部署建议启用;
- 未加载 --config 时为纯只读模式,动作接口返回 409 并说明原因。
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..admin_common.config_edit import PLATFORM_EDITABLE, merge_whitelist_and_save
from ..admin_common.logs import tail_lines
from ..connect.config import ConnectConfig, load_config
from ..connect.landing import LandingStore
from ..connect.mapping_apply import MappingCircuitBreaker, apply_object, apply_objects
from ..connect.scheduler import run_reconcile_cycle, run_sync_cycle
from ..metamodel.loader import load_pack
from .ui import UI_HTML

_INGEST_HEALTH = "http://127.0.0.1:8850/ingest/health"
_MCP_URL = "http://127.0.0.1:8848/mcp"
_MCP_HOST, _MCP_PORT = "127.0.0.1", 8848
_APPLY_LOG_STALE_SEC = 30 * 60
_LOG_FILES = {
    "ingest": "d2a-ingest.log",
    "apply": "d2a-apply.log",
    "mcp": "d2a-mcp.log",
    "console": "d2a-console.log",
}
_MCP_TOOLS = frozenset({"query_objects", "query_metrics"})


class ActionBody(BaseModel):
    source: str = "digiwin_e10"
    object: str | None = None
    deep: bool = False


class ConfigPatch(BaseModel):
    templates: str | None = None
    landing: str | None = None


class McpCallBody(BaseModel):
    tool: str
    params: dict[str, Any] = {}


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


def create_app(landing: str, templates: str = "templates",
               config: ConnectConfig | None = None, token: str | None = None,
               config_path: str | Path | None = None,
               log_dir: str | Path | None = None) -> FastAPI:
    if config is not None:  # 配置在场时以其为准,避免两套路径
        landing, templates = config.landing, config.templates
    _config_path = Path(config_path) if config_path else None
    _log_dir = Path(log_dir) if log_dir else None
    pack = load_pack(templates)
    default_source = next(iter(config.sources), "digiwin_e10") if config else "digiwin_e10"

    def auth(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")
        if supplied != token:
            raise HTTPException(401, "需要有效的控制台 Token(Authorization: Bearer <token>)")

    def store() -> LandingStore:
        return LandingStore(landing)

    def require_config() -> ConnectConfig:
        if config is None:
            raise HTTPException(
                409, "控制台以只读模式运行(未加载 --config connect.yaml),动作不可用")
        return config

    def require_config_path() -> Path:
        if _config_path is None:
            raise HTTPException(409, "未配置 config_path(--config),配置 API 不可用")
        return _config_path

    def _mcp_in_process(tool: str, params: dict[str, Any]) -> dict:
        from ..mcp_server.core import QueryService

        svc = QueryService(landing, templates, default_source)
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

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return UI_HTML

    @app.get("/v0", response_class=HTMLResponse)
    def v0() -> str:
        return UI_HTML

    # ---- 只读视图 ----

    @api.get("/overview")
    def overview() -> dict:
        db = store()
        sources = sorted({r[0] for r in db.con.execute(
            "SELECT DISTINCT source FROM d2a_sync_state")}
            | (set(config.sources) if config else set()))
        out_sources = []
        for s in sources:
            state = [dict(r) for r in db.con.execute(
                "SELECT table_name, watermark_col, high_water, last_run_at "
                "FROM d2a_sync_state WHERE source = ? ORDER BY table_name", (s,))]
            (quarantined,) = db.con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine WHERE source = ? AND resolved_at IS NULL",
                (s,)).fetchone()
            out_sources.append({"source": s, "state": state, "quarantined": quarantined})
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
        return {"landing": landing, "readonly": config is None,
                "actions_sync_reconcile": _actions_sync_reconcile(config),
                "sources": out_sources, "objects": objects}

    @api.get("/runs")
    def runs(limit: int = 15) -> list[dict]:
        return [dict(r) for r in store().con.execute(
            "SELECT * FROM d2a_sync_run ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),))]

    @api.get("/quarantine")
    def quarantine(object: str | None = None) -> list[dict]:
        where, params = "resolved_at IS NULL", []
        if object:
            where += " AND object = ?"
            params.append(object)
        return [dict(r) for r in store().con.execute(
            f"SELECT id, source, object, keys_json, reason, created_at "
            f"FROM d2a_quarantine WHERE {where} ORDER BY id DESC LIMIT 200", params)]

    @api.get("/audit")
    def audit(limit: int = 30) -> list[dict]:
        return [dict(r) for r in store().con.execute(
            "SELECT ts, source, action, sql, rows, duration_ms FROM d2a_audit_log "
            "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))]

    # ---- 配置 / 服务 / 日志 / 调试 ----

    @api.get("/config")
    def get_config() -> dict:
        path = require_config_path()
        return _platform_config_subset(load_config(path))

    @api.post("/config")
    def post_config(body: ConfigPatch) -> dict:
        path = require_config_path()
        patch = body.model_dump(exclude_none=True)
        ok, errors = merge_whitelist_and_save(
            path, PLATFORM_EDITABLE, patch, validate=load_config)
        return {"ok": ok, "errors": errors}

    @api.get("/services")
    def services() -> dict:
        ingest_ok, ingest_method = _probe_http(_INGEST_HEALTH)
        mcp_ok, mcp_method = _probe_http(_MCP_URL)
        if not mcp_ok:
            mcp_ok, mcp_method = _probe_tcp(_MCP_HOST, _MCP_PORT)
        apply_ok, apply_method = _probe_apply(_log_dir)
        return {
            "ingest": {"ok": ingest_ok, "method": ingest_method},
            "mcp": {"ok": mcp_ok, "method": mcp_method},
            "apply": {"ok": apply_ok, "method": apply_method},
            "console": {"ok": True, "method": "self"},
        }

    @api.get("/logs")
    def get_logs(service: str, lines: int = 200, level: str | None = None) -> dict:
        if service not in _LOG_FILES:
            raise HTTPException(400, f"未知服务 '{service}',可用:{sorted(_LOG_FILES)}")
        if _log_dir is None:
            return {"ok": False, "text": "未配置日志目录(--log-dir)"}
        capped = max(1, min(lines, 1000))
        ok, text = tail_lines(_log_dir / _LOG_FILES[service], lines=capped, level=level)
        return {"ok": ok, "text": text}

    @api.get("/debug/raw-table")
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

    @api.post("/debug/mcp-call")
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

    @api.post("/actions/sync")
    def action_sync(body: ActionBody) -> dict:
        cfg = require_config()
        executed = run_sync_cycle(body.source, _scfg(cfg, body.source), pack, cfg.landing)
        return {"executed": executed,
                "note": "" if executed else "错峰窗口外,未发起(窗口约束对控制台同样生效)"}

    @api.post("/actions/reconcile")
    def action_reconcile(body: ActionBody) -> dict:
        cfg = require_config()
        executed = run_reconcile_cycle(body.source, _scfg(cfg, body.source), pack,
                                       cfg.landing, deep=body.deep)
        return {"executed": executed,
                "note": "" if executed else "错峰窗口外,未发起"}

    @api.post("/actions/apply")
    def action_apply(body: ActionBody) -> dict:
        report = apply_objects(store(), pack, body.source)
        return {"executed": True, "results": [asdict(r) for r in report.results],
                "aborted": [r.object for r in report.aborted]}

    @api.post("/actions/retry")
    def action_retry(body: ActionBody) -> dict:
        if not body.object:
            raise HTTPException(422, "retry 需要 object 参数")
        tpl = next((o for o in pack.objects if o.object == body.object), None)
        if tpl is None:
            raise HTTPException(404, f"未知对象 '{body.object}'")
        try:
            result = apply_object(store(), tpl, body.source)
        except MappingCircuitBreaker as e:
            raise HTTPException(409, f"重试触发熔断:{e}") from e
        return {"executed": True, **asdict(result)}

    app.include_router(api)
    return app
