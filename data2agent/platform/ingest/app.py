"""ingest FastAPI 应用:增量 upsert + 全量快照 begin/batch/complete。

安全:可选 Bearer Token(--token / D2A_INGEST_TOKEN);无 Token 为开放(仅内网可信段)。
协议见 data2agent.protocol.ingest:应用版本可独立升级;推送契约由 supported 协议列表声明。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request

from ...shared.store.table import TableInfo
from ...shared.store.landing import LandingStore, raw_table_name
from ...protocol.ingest import (
    BatchBody,
    TableAbortBody,
    TableBeginBody,
    TableCompleteBody,
    health_protocol_fields,
)


def _info_from_body(table: str, columns: list[list[str]], pk: list[str]) -> TableInfo:
    bad = [c for c in columns if len(c) != 2]
    if bad:
        raise HTTPException(422, f"columns 须为 [列名, 类型] 二元组,got {bad[:3]}")
    return TableInfo(name=table, columns=[(c[0], c[1]) for c in columns], pk=list(pk))


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
        return {
            "ok": True,
            "landing": str(landing_path),
            **health_protocol_fields(),
        }

    @app.post("/ingest/table-begin", dependencies=[Depends(auth)])
    def ingest_table_begin(body: TableBeginBody) -> dict:
        info = _info_from_body(body.table, body.columns, body.pk)
        landing = LandingStore(landing_path)
        if body.mode == "incremental":
            landing.ensure_raw_table(body.source, info)
            return {
                "begun": True, "table": body.table, "mode": body.mode,
                "snapshot_id": None,
            }
        assert body.snapshot_id is not None
        result = landing.begin_snapshot(body.source, info, body.snapshot_id)
        return {
            "begun": True, "table": body.table, "mode": body.mode,
            "snapshot_id": result["snapshot_id"],
            "status": result["status"],
            "duplicate": result.get("duplicate", False),
        }

    @app.post("/ingest/batch", dependencies=[Depends(auth)])
    def ingest_batch(body: BatchBody) -> dict:
        info = _info_from_body(body.table, body.columns, body.pk)
        landing = LandingStore(landing_path)
        if body.mode == "full_refresh":
            assert body.snapshot_id is not None
            try:
                result = landing.write_snapshot_batch(
                    body.source, info, body.snapshot_id, body.batch_id, body.rows)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return {
                "ingested": result["ingested"], "table": body.table,
                "batch_id": body.batch_id, "snapshot_id": body.snapshot_id,
                "duplicate": result["duplicate"],
            }

        landing.ensure_raw_table(body.source, info)
        n = landing.upsert_rows(body.source, info, body.rows, body.batch_id)
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
        info = _info_from_body(body.table, body.columns, body.pk)
        landing = LandingStore(landing_path)

        if body.mode == "full_refresh":
            assert body.snapshot_id is not None
            try:
                result = landing.complete_snapshot(
                    body.source, info, body.snapshot_id, body.rows, body.batches)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            # 完成观测(幂等)
            existing = landing.con.execute(
                "SELECT s.id, s.run_id, s.status AS step_status, r.status AS run_status "
                "FROM d2a_run_step s JOIN d2a_sync_run r ON r.id = s.run_id "
                "WHERE r.source = ? AND s.kind = 'table' AND s.target = ? AND s.batch_id = ?",
                (body.source, body.table, body.completion_id),
            ).fetchone()
            if existing is None or not (
                    existing["step_status"] == "ok" and existing["run_status"] == "ok"):
                if existing is None:
                    run_id = landing.start_run(body.source, "ingest")
                    step_id = landing.add_step(
                        run_id, 1, "table", body.table, batch_id=body.completion_id)
                else:
                    run_id, step_id = existing["run_id"], existing["id"]
                landing.log_audit(
                    body.source, "ingest_complete",
                    f"table {body.table} snapshot {body.snapshot_id} complete",
                    body.rows, 0.0, body.completion_id)
                landing.update_step(step_id, status="ok",
                                    rows_in=body.rows, rows_out=body.rows)
                landing.finish_run(run_id, tables=1, rows=body.rows, status="ok")
            return {
                "completed": True, "table": body.table,
                "completion_id": body.completion_id,
                "snapshot_id": body.snapshot_id,
                "duplicate": result.get("duplicate", False),
            }

        # incremental: 零行表也必须建立空 raw
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

    @app.post("/ingest/table-abort", dependencies=[Depends(auth)])
    def ingest_table_abort(body: TableAbortBody) -> dict:
        """丢弃未发布 snapshot staging,保留当前 raw。幂等。"""
        assert body.snapshot_id is not None
        landing = LandingStore(landing_path)
        landing.abort_snapshot(body.source, body.table, body.snapshot_id)
        return {
            "aborted": True,
            "table": body.table,
            "snapshot_id": body.snapshot_id,
            "mode": body.mode,
        }

    return app
