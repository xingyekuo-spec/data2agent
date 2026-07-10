"""控制台应用:FastAPI 单页 + JSON API + 运维动作。

安全:
- 只读视图直接查落地库;动作(sync / reconcile / apply / retry)复用 connect
  引擎,错峰窗口 / 白名单 / 只读适配器约束原样生效,控制台不开新的旁路;
- 可选 Bearer Token(--token 或环境变量 D2A_CONSOLE_TOKEN),内网部署建议启用;
- 未加载 --config 时为纯只读模式,动作接口返回 409 并说明原因。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..connect.config import ConnectConfig
from ..connect.landing import LandingStore
from ..connect.mapping_apply import MappingCircuitBreaker, apply_object, apply_objects
from ..connect.scheduler import run_reconcile_cycle, run_sync_cycle
from ..metamodel.loader import load_pack
from .ui import UI_HTML


class ActionBody(BaseModel):
    source: str = "digiwin_e10"
    object: str | None = None
    deep: bool = False


def create_app(landing: str, templates: str = "templates",
               config: ConnectConfig | None = None, token: str | None = None) -> FastAPI:
    if config is not None:  # 配置在场时以其为准,避免两套路径
        landing, templates = config.landing, config.templates
    pack = load_pack(templates)

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

    app = FastAPI(title="data2agent 运维控制台")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
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
