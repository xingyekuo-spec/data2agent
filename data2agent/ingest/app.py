"""ingest FastAPI 应用:批次落地与表级完成确认。

安全:可选 Bearer Token(--token / D2A_INGEST_TOKEN);无 Token 为开放(仅内网可信段)。
落地复用 connect.landing.upsert_rows(按业务键 upsert,重推安全)。
每请求自建 LandingStore(sqlite 连接不跨线程复用)。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from ..connect.adapters.base import TableInfo
from ..connect.landing import LandingStore, raw_table_name


class BatchBody(BaseModel):
    source: str
    table: str
    columns: list[list[str]]   # [[列名, 可移植类型], ...]
    pk: list[str]
    batch_id: str
    rows: list[dict]


class TableCompleteBody(BaseModel):
    """中间机确认一张表的全部批次均已被平台接收。

    该事件是 HTTP 推送模式唯一的表级新鲜度证据；即使 rows=0 也必须发送。
    """

    source: str
    table: str
    columns: list[list[str]]
    pk: list[str]
    completion_id: str
    rows: int
    batches: int


def create_app(landing_path: str | Path, token: str | None = None) -> FastAPI:
    def auth(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if supplied != token:
            raise HTTPException(401, "需要有效 Token(Authorization: Bearer <token>)")

    app = FastAPI(title="data2agent ingest")

    @app.get("/ingest/health")
    def health() -> dict:
        return {"ok": True, "landing": str(landing_path)}

    @app.post("/ingest/batch", dependencies=[Depends(auth)])
    def ingest_batch(body: BatchBody) -> dict:
        if not body.pk:
            raise HTTPException(422, f"{body.table}: 缺主键,无法幂等落地")
        bad = [c for c in body.columns if len(c) != 2]
        if bad:
            raise HTTPException(422, f"columns 须为 [列名, 类型] 二元组,got {bad[:3]}")
        info = TableInfo(name=body.table,
                         columns=[(c[0], c[1]) for c in body.columns], pk=list(body.pk))
        landing = LandingStore(landing_path)
        landing.ensure_raw_table(body.source, info)
        n = landing.upsert_rows(body.source, info, body.rows, body.batch_id)
        # 观测记录(M4):同一 source/table/batch 重试关联既有 step;
        # 审计成功后才收口 ok,避免 HTTP 500 却留下 ok Run 的矛盾证据。
        run_id: int | None = None
        step_id: int | None = None
        existing_success = False
        try:
            step_target = raw_table_name(body.source, body.table)
            existing = landing.con.execute(
                "SELECT s.id, s.run_id, s.status AS step_status, r.status AS run_status "
                "FROM d2a_run_step s "
                "JOIN d2a_sync_run r ON r.id = s.run_id "
                "WHERE s.kind = 'batch' AND s.target = ? AND s.batch_id = ?",
                (step_target, body.batch_id)).fetchone()
            if existing is not None:
                run_id = existing["run_id"]
                step_id = existing["id"]
                existing_success = (
                    existing["step_status"] == "ok" and existing["run_status"] == "ok")
            else:
                run_id = landing.start_run(body.source, "ingest")
                step_id = landing.add_step(run_id, 1, "batch", step_target,
                                           batch_id=body.batch_id)
            landing.log_audit(body.source, "ingest",
                              f"batch {body.batch_id} → {raw_table_name(body.source, body.table)}",
                              n, 0.0, body.batch_id)
            if existing_success:
                # 重放旧 batch 仍记录审计，但不能刷新首次成功时间。
                return {"ingested": n, "table": body.table,
                        "batch_id": body.batch_id, "duplicate": True}
            landing.update_step(step_id, status="ok", rows_in=n, rows_out=n)
            landing.finish_run(run_id, tables=1, rows=n, status="ok")
        except Exception as e:
            if step_id is not None and not existing_success:
                try:
                    landing.update_step(step_id, status="failed", error=str(e)[:500])
                except Exception:
                    pass
            if run_id is not None and not existing_success:
                try:
                    row = landing.con.execute(
                        "SELECT status FROM d2a_sync_run WHERE id = ?",
                        (run_id,)).fetchone()
                    if row is None or row["status"] == "running":
                        landing.finish_run(
                            run_id, tables=0, rows=n, status="failed",
                            detail=f"ingest observation failed:{str(e)[:300]}")
                except Exception:
                    pass
            raise HTTPException(
                500, f"批次 {body.batch_id} 数据已写入,但观测记录失败:{e}") from e
        return {"ingested": n, "table": body.table, "batch_id": body.batch_id}

    @app.post("/ingest/table-complete", dependencies=[Depends(auth)])
    def ingest_table_complete(body: TableCompleteBody) -> dict:
        """记录表级完成事件，供平台 Validation 判断新鲜度。"""
        if not body.pk:
            raise HTTPException(422, f"{body.table}: 缺主键,无法创建 raw 表")
        if body.rows < 0 or body.batches < 0:
            raise HTTPException(422, "rows 和 batches 不能为负数")
        bad = [c for c in body.columns if len(c) != 2]
        if bad:
            raise HTTPException(422, f"columns 须为 [列名, 类型] 二元组,got {bad[:3]}")

        info = TableInfo(name=body.table,
                         columns=[(c[0], c[1]) for c in body.columns], pk=list(body.pk))
        landing = LandingStore(landing_path)
        # 零行表不会经过 /ingest/batch；完成事件仍必须建立空 raw 表。
        landing.ensure_raw_table(body.source, info)
        existing = landing.con.execute(
            "SELECT s.id, s.run_id, s.status AS step_status, r.status AS run_status "
            "FROM d2a_run_step s JOIN d2a_sync_run r ON r.id = s.run_id "
            "WHERE r.source = ? AND s.kind = 'table' AND s.target = ? AND s.batch_id = ?",
            (body.source, body.table, body.completion_id),
        ).fetchone()
        if existing is not None and existing["step_status"] == "ok" and existing["run_status"] == "ok":
            return {"completed": True, "table": body.table,
                    "completion_id": body.completion_id, "duplicate": True}

        if existing is None:
            run_id = landing.start_run(body.source, "ingest")
            step_id = landing.add_step(run_id, 1, "table", body.table,
                                       batch_id=body.completion_id)
        else:
            run_id, step_id = existing["run_id"], existing["id"]
        try:
            landing.log_audit(body.source, "ingest_complete",
                              f"table {body.table} complete ({body.completion_id})",
                              body.rows, 0.0, body.completion_id)
            landing.update_step(step_id, status="ok", rows_in=body.rows, rows_out=body.rows)
            landing.finish_run(run_id, tables=1, rows=body.rows, status="ok")
        except Exception as e:
            try:
                landing.update_step(step_id, status="failed", error=str(e)[:500])
                landing.finish_run(run_id, tables=0, rows=body.rows, status="failed",
                                   detail=f"table completion observation failed:{str(e)[:300]}")
            except Exception:
                pass
            raise HTTPException(500, f"表 {body.table} 完成记录失败:{e}") from e
        return {"completed": True, "table": body.table,
                "completion_id": body.completion_id}

    return app
