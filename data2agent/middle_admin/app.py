"""中间机管理 FastAPI 应用:配置读写 + 调度状态 JSON API。"""

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
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..admin_common.config_edit import MIDDLE_EDITABLE, merge_whitelist_and_save
from ..admin_common.logs import tail_lines
from ..connect.config import ConnectConfig, SourceConfig, load_config
from ..connect.landing import LandingStore
from ..connect.scheduler import build_adapter, run_sync_cycle
from ..metamodel.loader import load_pack
from .status import build_status

_ADMIN_STATIC = Path(__file__).resolve().parents[1] / "admin_templates" / "static"


class ConfigPatch(BaseModel):
    templates: str | None = None
    landing: str | None = None
    sources: dict[str, Any] | None = None


class TriggerBody(BaseModel):
    action: str
    source: str | None = None


class TestConnectionBody(BaseModel):
    source: str | None = None


_DSN_PATTERN = re.compile(
    r"(?i)(password\s*=|pwd\s*=|server\s*=|data\s+source\s*=|"
    r"uid\s*=|user\s+id\s*=|integrated\s+security|"
    r"file:[^\s?]+|\.sqlite\b|\.db\b)"
)


def _env_set(name: str | None) -> bool | None:
    if not name:
        return None
    return os.environ.get(name) is not None


def _config_subset(cfg: ConnectConfig) -> dict:
    """可编辑字段 + 凭据环境变量是否已设置(不暴露值)。"""
    out: dict[str, Any] = {"templates": cfg.templates, "landing": cfg.landing, "sources": {}}
    for name, scfg in cfg.sources.items():
        src: dict[str, Any] = {
            "windows": scfg.windows,
            "rate": {"batch_size": scfg.rate.batch_size,
                     "rows_per_second": scfg.rate.rows_per_second},
            "lookback": scfg.lookback,
            "sync_every": scfg.sync_every,
            "extra_whitelist": scfg.extra_whitelist,
            "sink": {"url": scfg.sink.url,
                     "token_env": scfg.sink.token_env,
                     "token_env_set": _env_set(scfg.sink.token_env)},
            "dsn_env": scfg.dsn_env,
            "dsn_env_set": _env_set(scfg.dsn_env),
        }
        out["sources"][name] = src
    return out


def _patch_to_dict(body: ConfigPatch) -> dict[str, Any]:
    data = body.model_dump(exclude_none=True)
    return data


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


def _probe_connection(name: str, scfg: SourceConfig, pack, landing_path: str) -> list[str]:
    landing = LandingStore(landing_path)
    adapter = build_adapter(name, scfg, pack, landing)
    tables: list[str] = []
    for tbl in sorted(adapter.whitelist):
        adapter.table_info(tbl)
        tables.append(tbl)
    return tables


def _validate_merged(path: Path, patch: dict[str, Any]) -> tuple[bool, list[dict[str, str]]]:
    """在临时副本上合并并 load_config,不写原文件。"""
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy2(path, tmp_path)
    try:
        return merge_whitelist_and_save(tmp_path, MIDDLE_EDITABLE, patch, validate=load_config)
    finally:
        tmp_path.unlink(missing_ok=True)


def create_app(config_path: str | Path, token: str | None = None,
               log_path: str | Path | None = None) -> FastAPI:
    config_path = Path(config_path)
    _log_path = Path(log_path) if log_path else None  # Task 4 使用

    def auth(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")
        if supplied != token:
            raise HTTPException(401, "需要有效的管理 Token(Authorization: Bearer <token>)")

    def reload_config() -> ConnectConfig:
        return load_config(config_path)

    app = FastAPI(title="data2agent 中间机管理")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return "<!DOCTYPE html><html><body><p>中间机管理(占位,Task 5 接 Jinja)</p></body></html>"

    @app.get("/status", response_class=HTMLResponse)
    @app.get("/config", response_class=HTMLResponse)
    @app.get("/logs", response_class=HTMLResponse)
    def html_placeholder() -> str:
        return "<!DOCTYPE html><html><body><p>占位页面(Task 5)</p></body></html>"

    @api.get("/config")
    def get_config() -> dict:
        return _config_subset(reload_config())

    @api.post("/config")
    def post_config(body: ConfigPatch) -> dict:
        patch = _patch_to_dict(body)
        ok, errors = merge_whitelist_and_save(
            config_path, MIDDLE_EDITABLE, patch, validate=load_config)
        return {"ok": ok, "errors": errors}

    @api.post("/config/validate")
    def validate_config(body: ConfigPatch) -> dict:
        ok, errors = _validate_merged(config_path, _patch_to_dict(body))
        return {"ok": ok, "errors": errors}

    @api.get("/status")
    def get_status() -> dict:
        return build_status(reload_config())

    @api.get("/logs")
    def get_logs(lines: int = 200, level: str | None = None) -> dict:
        if _log_path is None:
            return {"ok": False, "text": "未配置日志路径"}
        capped = max(1, min(lines, 1000))
        ok, text = tail_lines(_log_path, lines=capped, level=level)
        return {"ok": ok, "text": text}

    @api.post("/test-connection")
    def test_connection(body: TestConnectionBody = TestConnectionBody()) -> dict:
        cfg = reload_config()
        pack = load_pack(cfg.templates)
        name, scfg = _resolve_source(cfg, body.source)
        started = time.perf_counter()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_probe_connection, name, scfg, pack, cfg.landing)
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
        pack = load_pack(cfg.templates)
        name, scfg = _resolve_source(cfg, body.source)
        executed = run_sync_cycle(name, scfg, pack, cfg.landing)
        return {"action": "sync", "source": name, "executed": executed,
                "overlap_warning": True,
                "note": "" if executed else "错峰窗口外,未发起(窗口约束同样生效)"}

    app.include_router(api)
    if _ADMIN_STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=_ADMIN_STATIC), name="static")
    return app
