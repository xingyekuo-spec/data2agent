"""ingest FastAPI 应用:增量 upsert + 全量快照 begin/batch/complete。

安全:可选 Bearer Token(--token / D2A_INGEST_TOKEN);无 Token 为开放(仅内网可信段)。
协议见 data2agent.protocol.ingest:应用版本可独立升级;推送契约由 supported 协议列表声明。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request

from ...shared.store.table import TableInfo
from ...shared.store.landing import LandingStore, raw_table_name
from ...protocol.ingest import (
    BatchBody,
    RunBeginBody,
    RunCompleteBody,
    ReconcileRepairBatchBody,
    ReconcileRepairBeginBody,
    ReconcileRepairCompleteBody,
    ReconcileStatsBody,
    TableAbortBody,
    TableBeginBody,
    TableCompleteBody,
    batch_payload_digest,
    decode_transport_rows,
    health_protocol_fields,
)


def _info_from_body(
    table: str, columns: list[list[str]], pk: list[str],
    schema: str | None = None,
) -> TableInfo:
    bad = [c for c in columns if len(c) != 2]
    if bad:
        raise HTTPException(422, f"columns 须为 [列名, 类型] 二元组,got {bad[:3]}")
    return TableInfo(
        name=table, columns=[(c[0], c[1]) for c in columns], pk=list(pk),
        schema=schema)


def create_app(landing_path: str | Path, token: str | None = None) -> FastAPI:
    # 进程启动时只迁移一次；请求连接不得反复跑 DDL。
    initialized = LandingStore(landing_path)
    initialized.con.close()

    def _registry_for(source: str | None) -> tuple[dict | None, bool]:
        """(登记记录, 登记簿是否非空);库不可读时按空登记簿处理(引导期)。"""
        try:
            db = LandingStore.open_existing(landing_path)
        except Exception:
            return None, False
        try:
            reg = db.get_source_registration(source) if source else None
            return reg, db.registry_has_any()
        finally:
            db.con.close()

    async def auth(request: Request) -> None:
        """签发制授权(2026-08):

        - 登记源:Bearer 须为该源签发的 Token(库中只存哈希);已停用源 403;
        - 未登记源:仅全局管理员 Token(D2A_INGEST_TOKEN,迁移期兼容)可推;
        - 空登记簿 + 无全局 Token = 开发引导期,开放;
          首个源登记后未登记推送一律 403(先登记才接收)。
        """
        bearer = (
            request.headers.get("authorization", "").removeprefix("Bearer ").strip())
        # 管理员 Token 最高优先:可推任何源(含未登记),供迁移期与运维逃生
        if token and bearer == token:
            return
        source: str | None = None
        if (
            request.method == "POST"
            and request.headers.get("content-type", "").startswith("application/json")
        ):
            try:
                payload = await request.json()
            except Exception:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("source"), str):
                source = payload["source"]
        reg, has_any = _registry_for(source)
        if reg is not None:
            if reg["status"] != "active":
                raise HTTPException(
                    403, f"数据源 {source} 已停用,请在平台数据源管理中启用")
            digest = hashlib.sha256(bearer.encode()).hexdigest() if bearer else ""
            if bearer and digest == reg["token_sha256"]:
                return
            raise HTTPException(401, f"数据源 {source} 的推送 Token 无效")
        if token:
            raise HTTPException(401, "需要有效 Token(Authorization: Bearer <token>)")
        if has_any:
            raise HTTPException(
                403, f"数据源 {source} 未在平台登记;签发制下请先在数据源管理中登记")
        # 引导期:空登记簿且无全局 Token,保持内网开放

    def open_landing():
        """每个请求独占连接，并在所有返回/异常路径统一释放。"""
        db = LandingStore.open_existing(landing_path)
        try:
            yield db
        finally:
            db.con.close()

    app = FastAPI(title="data2agent ingest")

    @app.get("/ingest/health")
    def health() -> dict:
        return {
            "ok": True,
            "landing": str(landing_path),
            **health_protocol_fields(),
        }

    @app.post("/ingest/table-begin", dependencies=[Depends(auth)])
    def ingest_table_begin(
        body: TableBeginBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        info = _info_from_body(
            body.table, body.columns, body.pk, body.schema_name)
        if body.mode == "incremental":
            try:
                if body.generation_id is not None:
                    landing.begin_incremental_ingest_table(
                        body.source, body.generation_id, info)
                else:
                    landing.ensure_raw_table(body.source, info)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            return {
                "begun": True, "table": body.table, "mode": body.mode,
                "snapshot_id": None,
            }
        assert body.snapshot_id is not None
        try:
            result = landing.begin_snapshot(
                body.source, info, body.snapshot_id,
                generation_id=body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {
            "begun": True, "table": body.table, "mode": body.mode,
            "snapshot_id": result["snapshot_id"],
            "status": result["status"],
            "duplicate": result.get("duplicate", False),
        }

    @app.post("/ingest/run-begin", dependencies=[Depends(auth)])
    def ingest_run_begin(
        body: RunBeginBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        try:
            return landing.begin_ingest_generation(
                body.source, body.generation_id, body.tables)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/ingest/run-complete", dependencies=[Depends(auth)])
    def ingest_run_complete(
        body: RunCompleteBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        try:
            result = landing.complete_ingest_generation(
                body.source, body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"generation_id": body.generation_id, **result}

    @app.post("/ingest/run-abort", dependencies=[Depends(auth)])
    def ingest_run_abort(
        body: RunCompleteBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        landing.abort_ingest_generation(body.source, body.generation_id)
        return {"generation_id": body.generation_id, "aborted": True}

    @app.post("/ingest/run-heartbeat", dependencies=[Depends(auth)])
    def ingest_run_heartbeat(
        body: RunCompleteBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        try:
            landing.touch_ingest_generation(body.source, body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            landing.con.close()
        return {"generation_id": body.generation_id, "alive": True}

    @app.post("/ingest/batch", dependencies=[Depends(auth)])
    def ingest_batch(
        body: BatchBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        info = _info_from_body(
            body.table, body.columns, body.pk, body.schema_name)
        payload_sha256 = batch_payload_digest(
            body.model_dump(by_alias=True, exclude_none=True))
        try:
            decoded_rows = decode_transport_rows(body.rows, body.columns)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if body.mode == "full_refresh":
            assert body.snapshot_id is not None
            try:
                result = landing.write_snapshot_batch(
                    body.source, info, body.snapshot_id, body.batch_id, decoded_rows,
                    payload_sha256=payload_sha256,
                    generation_id=body.generation_id)
            except ValueError as e:
                status = 409 if (
                    "摘要不同" in str(e) or "generation" in str(e)
                ) else 422
                raise HTTPException(status, str(e)) from e
            return {
                "ingested": result["ingested"], "table": body.table,
                "batch_id": body.batch_id, "snapshot_id": body.snapshot_id,
                "duplicate": result["duplicate"],
                "payload_sha256": payload_sha256,
            }

        try:
            if body.ingest_protocol_version == "2":
                # v2 旧中间机一张表的所有分页复用同一 batch_id，不能套用
                # v3 的“同 ID 同摘要”规则；保留旧 upsert 语义直至现场升级。
                landing.ensure_raw_table(body.source, info)
                n_legacy = landing.upsert_rows(
                    body.source, info, decoded_rows, body.batch_id)
                result = {
                    "ingested": n_legacy, "duplicate": False,
                    "payload_sha256": payload_sha256,
                }
            else:
                result = landing.commit_ingest_batch(
                    body.source, info, decoded_rows, body.batch_id,
                    body.table_run_id or body.batch_id, payload_sha256,
                    generation_id=body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        n = int(result["ingested"])
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
                        "batch_id": body.batch_id, "duplicate": True,
                        "payload_sha256": payload_sha256}
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
        return {
            "ingested": n, "table": body.table, "batch_id": body.batch_id,
            "duplicate": bool(result["duplicate"]),
            "payload_sha256": payload_sha256,
        }

    @app.post("/ingest/table-complete", dependencies=[Depends(auth)])
    def ingest_table_complete(
        body: TableCompleteBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        info = _info_from_body(
            body.table, body.columns, body.pk, body.schema_name)
        # 必须在发布 snapshot / 接受增量完成证据之前校验 generation；
        # 否则失败或被抢占的 generation 仍可修改当前 raw。
        if body.mode == "full_refresh":
            assert body.snapshot_id is not None
            try:
                result = landing.complete_snapshot(
                    body.source, info, body.snapshot_id, body.rows, body.batches,
                    generation_id=body.generation_id)
            except ValueError as e:
                status = 409 if "generation" in str(e) else 422
                raise HTTPException(status, str(e)) from e
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
        try:
            if body.generation_id is not None:
                landing.complete_incremental_ingest_table(
                    body.source, body.generation_id, info,
                    body.completion_id, body.rows, body.batches)
            else:
                landing.ensure_raw_table(body.source, info)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
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
    def ingest_table_abort(
        body: TableAbortBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        """丢弃未发布 snapshot staging,保留当前 raw。幂等。"""
        assert body.snapshot_id is not None
        if body.generation_id is not None:
            try:
                landing.touch_ingest_generation(
                    body.source, body.generation_id, body.table)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
        landing.abort_snapshot(body.source, body.table, body.snapshot_id)
        return {
            "aborted": True,
            "table": body.table,
            "snapshot_id": body.snapshot_id,
            "mode": body.mode,
        }

    @app.post("/ingest/reconcile", dependencies=[Depends(auth)])
    def ingest_reconcile_stats(
        body: ReconcileStatsBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        """E6b L1：仅返回平台 raw 的同口径统计。"""
        try:
            landing.touch_ingest_generation(
                body.source, body.generation_id, body.table)
            result = landing.reconcile_stats(
                body.source, body.table, body.watermark_col,
                body.start, body.end)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            landing.con.close()
        return {"table": body.table, **result}

    @app.post("/ingest/reconcile-run-complete", dependencies=[Depends(auth)])
    def ingest_reconcile_run_complete(
        body: RunBeginBody, landing: LandingStore = Depends(open_landing),
    ) -> dict:
        """E6b 整轮完成屏障：所有 raw 修复完成后一次性提交 generation。"""
        try:
            for table in body.tables:
                landing.record_ingest_table_commit(
                    body.source, body.generation_id, table, 0, 0)
            result = landing.complete_ingest_generation(
                body.source, body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            landing.con.close()
        return {"generation_id": body.generation_id, **result}

    @app.post("/ingest/reconcile-repair-begin", dependencies=[Depends(auth)])
    def ingest_reconcile_repair_begin(
        body: ReconcileRepairBeginBody,
        landing: LandingStore = Depends(open_landing),
    ) -> dict:
        info = _info_from_body(
            body.table, body.columns, body.pk, body.schema_name)
        try:
            return landing.begin_reconcile_repair(
                body.source, info, body.repair_id, body.watermark_col,
                body.start, body.end, generation_id=body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            landing.con.close()

    @app.post("/ingest/reconcile-repair-batch", dependencies=[Depends(auth)])
    def ingest_reconcile_repair_batch(
        body: ReconcileRepairBatchBody,
        landing: LandingStore = Depends(open_landing),
    ) -> dict:
        canonical = json.dumps(
            body.model_dump(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
        try:
            decoded_rows = decode_transport_rows(body.rows)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        try:
            result = landing.write_reconcile_repair_batch(
                body.source, body.table, body.repair_id,
                body.batch_id, decoded_rows, digest,
                generation_id=body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            landing.con.close()
        return {
            "table": body.table, "repair_id": body.repair_id,
            "batch_id": body.batch_id, **result,
        }

    @app.post("/ingest/reconcile-repair-complete", dependencies=[Depends(auth)])
    def ingest_reconcile_repair_complete(
        body: ReconcileRepairCompleteBody,
        landing: LandingStore = Depends(open_landing),
    ) -> dict:
        try:
            result = landing.complete_reconcile_repair(
                body.source, body.table, body.repair_id,
                body.rows, body.batches,
                generation_id=body.generation_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            landing.con.close()
        return {
            "table": body.table, "repair_id": body.repair_id, **result,
        }

    @app.post("/ingest/reconcile-repair-abort", dependencies=[Depends(auth)])
    def ingest_reconcile_repair_abort(
        body: ReconcileRepairCompleteBody,
        landing: LandingStore = Depends(open_landing),
    ) -> dict:
        try:
            landing.touch_ingest_generation(
                body.source, body.generation_id, body.table)
            landing.abort_reconcile_repair(
                body.source, body.table, body.repair_id)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        finally:
            landing.con.close()
        return {
            "table": body.table, "repair_id": body.repair_id,
            "aborted": True,
        }

    return app
