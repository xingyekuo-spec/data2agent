"""中间机管理 FastAPI 应用:配置读写 + 调度状态 JSON API。"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..admin_common.config_edit import MIDDLE_EDITABLE, merge_whitelist_and_save
from ..connect.config import ConnectConfig, load_config
from .status import build_status

_ADMIN_STATIC = Path(__file__).resolve().parents[1] / "admin_templates" / "static"


class ConfigPatch(BaseModel):
    templates: str | None = None
    landing: str | None = None
    sources: dict[str, Any] | None = None


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

    app.include_router(api)
    if _ADMIN_STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=_ADMIN_STATIC), name="static")
    return app
