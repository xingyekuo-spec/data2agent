"""ingest v3 接收侧存储(仅测试用):generation 屏障、批次回执与表完成证据。

中间机状态库(``LandingStore``)只承担发送侧控制状态;这里在其 raw / 快照 /
对账 staging 能力之上补齐接收侧语义,供 ``reference_ingest`` 参考实现与契约测试使用。
快照与对账修复方法留在基类:中间机 ``LocalSink`` 与 ``reconcile`` 也依赖它们,
基类通过 ``_touch_ingest_generation_uncommitted`` 等钩子在同一写事务内回调本类。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from data2agent.shared.store.landing import _SCHEMA_INIT_LOCK, LandingStore, _now
from data2agent.shared.store.table import TableInfo

_RECEIVER_DDL = """
CREATE TABLE IF NOT EXISTS d2a_ingest_batch_receipt (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    schema_name TEXT,
    table_run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    rows INTEGER NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (source, table_name, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_d2a_ingest_receipt_run
    ON d2a_ingest_batch_receipt (source, table_name, table_run_id);
CREATE TABLE IF NOT EXISTS d2a_ingest_generation (
    source TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('open', 'committed', 'failed')
    ),
    expected_tables_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_activity_at TEXT,
    committed_at TEXT,
    PRIMARY KEY (source, generation_id)
);
CREATE INDEX IF NOT EXISTS idx_d2a_ingest_generation_source
    ON d2a_ingest_generation (source, created_at DESC);
CREATE TABLE IF NOT EXISTS d2a_ingest_table_commit (
    source TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    rows INTEGER NOT NULL,
    batches INTEGER NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (source, generation_id, table_name)
);
"""


class IngestReceiverStore(LandingStore):
    def __init__(self, db_path: str | Path):
        super().__init__(db_path)
        with _SCHEMA_INIT_LOCK:
            self.con.executescript(_RECEIVER_DDL)

    def _prune_receiver_history(
        self, receipt_cutoff: str, counts: dict[str, int],
    ) -> None:
        cur = self.con.execute(
            "DELETE FROM d2a_ingest_batch_receipt "
            "WHERE committed_at < ?", (receipt_cutoff,))
        counts["ingest_receipts"] = cur.rowcount

    def _cleanup_receiver_state(self, cutoff: str, counts: dict[str, int]) -> None:
        cur = self.con.execute(
            "UPDATE d2a_ingest_generation SET status = 'failed' "
            "WHERE status = 'open' AND COALESCE(last_activity_at, created_at) < ?",
            (cutoff,),
        )
        counts["generations"] = max(0, cur.rowcount)

    def commit_ingest_batch(
        self, source: str, info: TableInfo, rows: list[dict], batch_id: str,
        table_run_id: str, payload_sha256: str, *,
        generation_id: str | None = None,
    ) -> dict:
        """数据 upsert 与不可变批次回执同事务提交。"""
        try:
            self.con.execute("BEGIN IMMEDIATE")
            if generation_id is not None:
                self._touch_ingest_generation_uncommitted(
                    source, generation_id, info.name)
            self.ensure_raw_table(source, info, commit=False)
            # 在写锁内检查，保证并发重试返回原回执，而不是撞唯一键。
            existing = self.con.execute(
                "SELECT payload_sha256, rows, committed_at "
                "FROM d2a_ingest_batch_receipt "
                "WHERE source = ? AND table_name = ? AND batch_id = ?",
                (source, info.name, batch_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise ValueError(
                        f"{info.name}: batch_id '{batch_id}' 已提交但载荷摘要不同")
                self.con.commit()
                return {
                    "ingested": existing["rows"], "duplicate": True,
                    "payload_sha256": existing["payload_sha256"],
                    "committed_at": existing["committed_at"],
                }
            n = self.upsert_rows(
                source, info, rows, batch_id, commit=False)
            committed_at = _now()
            self.con.execute(
                "INSERT INTO d2a_ingest_batch_receipt "
                "(source, table_name, schema_name, table_run_id, batch_id, "
                "payload_sha256, rows, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source, info.name, info.schema, table_run_id, batch_id,
                 payload_sha256, n, committed_at),
            )
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {
            "ingested": n, "duplicate": False,
            "payload_sha256": payload_sha256, "committed_at": committed_at,
        }

    def verify_ingest_table_run(
        self, source: str, table: str, table_run_id: str,
        expected_rows: int, expected_batches: int,
    ) -> None:
        batch_count, row_sum = self.con.execute(
            "SELECT COUNT(*), COALESCE(SUM(rows), 0) "
            "FROM d2a_ingest_batch_receipt "
            "WHERE source = ? AND table_name = ? AND table_run_id = ?",
            (source, table, table_run_id),
        ).fetchone()
        if int(batch_count) != expected_batches or int(row_sum) != expected_rows:
            raise ValueError(
                f"{table}: 增量完成核验失败:"
                f"committed_batches={batch_count}/{expected_batches},"
                f"committed_rows={row_sum}/{expected_rows}")

    def begin_ingest_generation(
        self, source: str, generation_id: str, tables: list[str], *,
        open_lease_seconds: float = 900.0,
    ) -> dict:
        expected = sorted(tables)
        # 接收侧约定:下游消费者只能读取 status='committed' 的完整 generation。
        # 原平台在已提交但未消费时会拒绝开启新 generation(apply 屏障);该屏障属于
        # 接收方的消费策略而非线协议,平台移出后参考实现不再内置。若接收方在消费
        # 期间不能容忍 raw 被下一轮改写,须自行在 run-begin 上返回 409 串行化。
        try:
            self.con.execute("BEGIN IMMEDIATE")
            existing = self.con.execute(
                "SELECT status, expected_tables_json FROM d2a_ingest_generation "
                "WHERE source = ? AND generation_id = ?",
                (source, generation_id),
            ).fetchone()
            if existing is not None:
                if json.loads(existing["expected_tables_json"]) != expected:
                    raise ValueError("相同 generation_id 的 tables 清单不一致")
                if existing["status"] == "open":
                    self.con.execute(
                        "UPDATE d2a_ingest_generation SET last_activity_at = ? "
                        "WHERE source = ? AND generation_id = ?",
                        (_now(), source, generation_id),
                    )
                self.con.commit()
                return {
                    "generation_id": generation_id,
                    "status": existing["status"], "duplicate": True,
                }
            open_row = self.con.execute(
                "SELECT generation_id, COALESCE(last_activity_at, created_at) AS active_at "
                "FROM d2a_ingest_generation "
                "WHERE source = ? AND status = 'open' "
                "ORDER BY created_at DESC LIMIT 1",
                (source,),
            ).fetchone()
            if open_row is not None:
                cutoff = (
                    datetime.now() - timedelta(seconds=max(60.0, open_lease_seconds))
                ).isoformat(timespec="seconds")
                if open_row["active_at"] >= cutoff:
                    raise ValueError(
                        f"{source}: generation {open_row['generation_id']} 仍在活动，"
                        "已拒绝并发中间机/重复 connector；请停止重复实例后重试"
                    )
                self.con.execute(
                    "UPDATE d2a_ingest_generation SET status = 'failed' "
                    "WHERE source = ? AND generation_id = ? AND status = 'open'",
                    (source, open_row["generation_id"]),
                )
            now = _now()
            self.con.execute(
                "INSERT INTO d2a_ingest_generation "
                "(source, generation_id, status, expected_tables_json, created_at, "
                "last_activity_at) VALUES (?, ?, 'open', ?, ?, ?)",
                (source, generation_id, json.dumps(expected), now, now),
            )
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {
            "generation_id": generation_id,
            "status": "open", "duplicate": False,
        }

    def _touch_ingest_generation_uncommitted(
        self, source: str, generation_id: str, table: str | None = None,
    ) -> None:
        row = self.con.execute(
            "SELECT status, expected_tables_json FROM d2a_ingest_generation "
            "WHERE source = ? AND generation_id = ?",
            (source, generation_id),
        ).fetchone()
        if row is None or row["status"] != "open":
            raise ValueError(
                f"{source}: generation '{generation_id}' 未开启或已关闭"
            )
        if table is not None and table not in json.loads(row["expected_tables_json"]):
            raise ValueError(
                f"{source}: 表 '{table}' 不在 generation 计划内"
            )
        cur = self.con.execute(
            "UPDATE d2a_ingest_generation SET last_activity_at = ? "
            "WHERE source = ? AND generation_id = ? AND status = 'open'",
            (_now(), source, generation_id),
        )
        if cur.rowcount != 1:
            raise ValueError(
                f"{source}: generation '{generation_id}' 已在并发请求中关闭")

    def touch_ingest_generation(
        self, source: str, generation_id: str, table: str | None = None,
    ) -> None:
        self._touch_ingest_generation(source, generation_id, table)

    def begin_incremental_ingest_table(
        self, source: str, generation_id: str, info: TableInfo,
    ) -> None:
        """在 generation 屏障内创建/校验增量 raw 表。"""
        try:
            self.con.execute("BEGIN IMMEDIATE")
            self._touch_ingest_generation_uncommitted(
                source, generation_id, info.name)
            self.ensure_raw_table(source, info, commit=False)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def _record_ingest_table_commit_uncommitted(
        self, source: str, generation_id: str, table: str,
        rows: int, batches: int,
    ) -> None:
        generation = self.con.execute(
            "SELECT status, expected_tables_json FROM d2a_ingest_generation "
            "WHERE source = ? AND generation_id = ?",
            (source, generation_id),
        ).fetchone()
        if generation is None or generation["status"] != "open":
            raise ValueError(
                f"{source}: generation '{generation_id}' 未开启或已关闭")
        expected = json.loads(generation["expected_tables_json"])
        if table not in expected:
            raise ValueError(f"{source}: 表 '{table}' 不在 generation 计划内")
        existing = self.con.execute(
            "SELECT rows, batches FROM d2a_ingest_table_commit "
            "WHERE source = ? AND generation_id = ? AND table_name = ?",
            (source, generation_id, table),
        ).fetchone()
        if existing is not None:
            if (existing["rows"], existing["batches"]) != (rows, batches):
                raise ValueError(f"{table}: generation 完成记录与已提交值不一致")
            return
        self.con.execute(
            "INSERT INTO d2a_ingest_table_commit "
            "(source, generation_id, table_name, rows, batches, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, generation_id, table, rows, batches, _now()),
        )

    def record_ingest_table_commit(
        self, source: str, generation_id: str, table: str,
        rows: int, batches: int,
    ) -> None:
        try:
            self.con.execute("BEGIN IMMEDIATE")
            self._touch_ingest_generation_uncommitted(
                source, generation_id, table)
            self._record_ingest_table_commit_uncommitted(
                source, generation_id, table, rows, batches)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def complete_incremental_ingest_table(
        self, source: str, generation_id: str, info: TableInfo,
        table_run_id: str, rows: int, batches: int,
    ) -> None:
        """原子校验批次回执并登记 generation 的表完成证据。"""
        try:
            self.con.execute("BEGIN IMMEDIATE")
            self._touch_ingest_generation_uncommitted(
                source, generation_id, info.name)
            self.ensure_raw_table(source, info, commit=False)
            self.verify_ingest_table_run(
                source, info.name, table_run_id, rows, batches)
            self._record_ingest_table_commit_uncommitted(
                source, generation_id, info.name, rows, batches)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def complete_ingest_generation(
        self, source: str, generation_id: str,
    ) -> dict:
        generation = self.con.execute(
            "SELECT status, expected_tables_json FROM d2a_ingest_generation "
            "WHERE source = ? AND generation_id = ?",
            (source, generation_id),
        ).fetchone()
        if generation is None:
            raise ValueError(f"{source}: generation '{generation_id}' 不存在")
        if generation["status"] == "committed":
            return {"committed": True, "duplicate": True}
        if generation["status"] != "open":
            raise ValueError(
                f"{source}: generation '{generation_id}' 状态为 "
                f"{generation['status']}")
        expected = set(json.loads(generation["expected_tables_json"]))
        actual = {
            r[0] for r in self.con.execute(
                "SELECT table_name FROM d2a_ingest_table_commit "
                "WHERE source = ? AND generation_id = ?",
                (source, generation_id),
            )
        }
        if actual != expected:
            raise ValueError(
                f"{source}: generation 表完成集合不一致:"
                f"missing={sorted(expected - actual)},extra={sorted(actual - expected)}")
        self.con.execute(
            "UPDATE d2a_ingest_generation "
            "SET status = 'committed', committed_at = ? "
            "WHERE source = ? AND generation_id = ? AND status = 'open'",
            (_now(), source, generation_id),
        )
        self.con.commit()
        return {"committed": True, "duplicate": False}

    def abort_ingest_generation(self, source: str, generation_id: str) -> None:
        self.con.execute(
            "UPDATE d2a_ingest_generation SET status = 'failed' "
            "WHERE source = ? AND generation_id = ? AND status = 'open'",
            (source, generation_id),
        )
        self.con.commit()
