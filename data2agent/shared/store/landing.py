"""原样落地层:raw_{source}__{table},按源主键 upsert,幂等。

落地库为 SQLite(开发 / 参考链 / 首个工厂现场验证);单写者 + 多只读者,
初始化即开 WAL + busy_timeout。PostgreSQL 属后续切片(触发信号见设计 §4)。
系统表:d2a_audit_log(逐条源 SQL)、d2a_sync_run(逐轮汇总)。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .table import TableInfo

_TYPE_SQL = {"int": "INTEGER", "real": "REAL", "text": "TEXT", "blob": "BLOB"}
_SCHEMA_INIT_LOCK = threading.RLock()

_META_COLS = [
    ("_d2a_batch_id", "TEXT"),
    ("_d2a_extracted_at", "TEXT"),
    ("_d2a_row_hash", "TEXT"),
    ("_d2a_deleted_at", "TEXT"),   # 软删标记,由对账(E3)维护,永不物理删
]

_SYSTEM_DDL = """
CREATE TABLE IF NOT EXISTS d2a_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL,
    sql TEXT NOT NULL, rows INTEGER, duration_ms REAL, batch_id TEXT
);
CREATE TABLE IF NOT EXISTS d2a_sync_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    tables INTEGER, rows INTEGER, status TEXT, detail TEXT,
    run_type TEXT, steps_recorded INTEGER,
    dataset_version TEXT,
    generation_id TEXT
);
CREATE TABLE IF NOT EXISTS d2a_sync_state (
    source TEXT NOT NULL, table_name TEXT NOT NULL,
    watermark_col TEXT NOT NULL, high_water TEXT,
    last_run_at TEXT, last_batch_id TEXT,
    key_columns_json TEXT, schema_name TEXT,
    PRIMARY KEY (source, table_name)
);
CREATE TABLE IF NOT EXISTS d2a_run_step (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- table | object | segment | batch
    target TEXT NOT NULL,        -- 表 / 对象 / segment / batch 标识
    status TEXT NOT NULL,        -- running | ok | paused | failed | aborted
    started_at TEXT, finished_at TEXT,
    batch_id TEXT,
    rows_in INTEGER, rows_out INTEGER, quarantined INTEGER,
    repaired INTEGER, soft_deleted INTEGER,
    watermark_before TEXT,       -- JSON
    watermark_after TEXT,        -- JSON
    error TEXT, error_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_d2a_run_step_run ON d2a_run_step (run_id, ordinal, id);
CREATE INDEX IF NOT EXISTS idx_d2a_run_step_batch ON d2a_run_step (batch_id);
"""

_TRANSFER_DDL = """
CREATE TABLE IF NOT EXISTS d2a_snapshot (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'published', 'failed')),
    staging_table TEXT NOT NULL,
    schema_name TEXT,
    expected_rows INTEGER,
    expected_batches INTEGER,
    created_at TEXT NOT NULL,
    last_activity_at TEXT,
    published_at TEXT,
    PRIMARY KEY (source, table_name, snapshot_id)
);
CREATE TABLE IF NOT EXISTS d2a_snapshot_batch (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    rows INTEGER NOT NULL,
    payload_sha256 TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, table_name, snapshot_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_d2a_snapshot_status
    ON d2a_snapshot (source, table_name, status);
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
        status IN ('open', 'committed', 'applying', 'applied', 'failed')
    ),
    expected_tables_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_activity_at TEXT,
    committed_at TEXT,
    applied_at TEXT,
    apply_owner TEXT,
    apply_lease_until TEXT,
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
CREATE TABLE IF NOT EXISTS d2a_runtime_key_validation (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    strategy_fingerprint TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    PRIMARY KEY (source, table_name, strategy_fingerprint)
);
CREATE TABLE IF NOT EXISTS d2a_schema_validation (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    schema_fingerprint TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    PRIMARY KEY (source, table_name, schema_fingerprint)
);
CREATE TABLE IF NOT EXISTS d2a_raw_table_identity (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (source, table_name)
);
CREATE TABLE IF NOT EXISTS d2a_reconcile_repair (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    repair_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'completed', 'failed')),
    staging_table TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    pk_json TEXT NOT NULL,
    watermark_col TEXT,
    segment_start TEXT,
    segment_end TEXT,
    rows INTEGER,
    batches INTEGER,
    soft_deleted INTEGER,
    created_at TEXT NOT NULL,
    last_activity_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (source, table_name, repair_id)
);
CREATE TABLE IF NOT EXISTS d2a_reconcile_repair_batch (
    source TEXT NOT NULL,
    table_name TEXT NOT NULL,
    repair_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    rows INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, table_name, repair_id, batch_id)
);
CREATE TABLE IF NOT EXISTS d2a_http_push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    run_id INTEGER,
    step_kind TEXT NOT NULL,       -- begin_table | write | complete_table | abort_table
    table_name TEXT NOT NULL,
    mode TEXT NOT NULL,            -- incremental | full_refresh
    batch_id TEXT,
    rows_count INTEGER,
    status TEXT NOT NULL,          -- ok | failed
    error_detail TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL,
    generation_id TEXT,
    error_category TEXT,
    retryable INTEGER,
    receipt_received INTEGER,
    idempotent_replay INTEGER,
    receipt_digest TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_d2a_push_log_source
    ON d2a_http_push_log (source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_d2a_push_log_batch
    ON d2a_http_push_log (batch_id);
CREATE TABLE IF NOT EXISTS d2a_aihub_object_version (
    source_application_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    change_key TEXT NOT NULL,
    PRIMARY KEY (source_application_id, object_type, object_id)
);
CREATE TABLE IF NOT EXISTS d2a_aihub_type_watermark (
    source_application_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    next_version INTEGER NOT NULL,
    high_watermark INTEGER NOT NULL,
    PRIMARY KEY (source_application_id, object_type)
);
CREATE TABLE IF NOT EXISTS d2a_aihub_generation (
    source_application_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    table_name TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    external_generation_id TEXT NOT NULL,
    sync_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    next_sequence_no INTEGER NOT NULL,
    high_watermark INTEGER NOT NULL,
    total_rows INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    started_at REAL NOT NULL DEFAULT 0,
    create_request_json TEXT NOT NULL,
    complete_request_json TEXT,
    PRIMARY KEY (source_application_id, object_type)
);
CREATE TABLE IF NOT EXISTS d2a_aihub_batch_receipt (
    source_application_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    external_batch_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    high_watermark INTEGER NOT NULL,
    PRIMARY KEY (source_application_id, object_type, sequence_no)
);
CREATE TABLE IF NOT EXISTS d2a_aihub_pending_batch (
    source_application_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    external_batch_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    spool_path TEXT NOT NULL,
    PRIMARY KEY (source_application_id, object_type, sequence_no)
);
CREATE TABLE IF NOT EXISTS d2a_alert_event (
    alert_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    recovered_at TEXT,
    occurrences INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL
);
"""


def raw_table_name(source: str, table: str) -> str:
    return f"raw_{source}__{table}"


def _safe_snapshot_id(snapshot_id: str) -> str:
    text = (snapshot_id or "").strip()
    if not text or not all(c.isalnum() or c in "-_" for c in text):
        raise ValueError(f"非法 snapshot_id '{snapshot_id}'(仅允许字母数字-_ )")
    if len(text) > 64:
        raise ValueError("snapshot_id 过长(最多 64)")
    return text


def staging_raw_table_name(source: str, table: str, snapshot_id: str) -> str:
    return f"{raw_table_name(source, table)}__snap_{_safe_snapshot_id(snapshot_id)}"


def normalize_value(v):
    """源侧驱动返回的对象类型收敛为可移植类型(设计 §4:int/real/text/blob)。"""
    if isinstance(v, datetime) and v.tzinfo is not None:
        # datetimeoffset 全部规一到 UTC，使字符串水位顺序与时间顺序一致。
        return v.astimezone(timezone.utc).isoformat(sep=" ")
    if isinstance(v, (datetime, date)):
        return str(v)
    if isinstance(v, Decimal):
        return format(v, "f")
    return v


def row_hash(row: dict) -> str:
    """行内容指纹(不含元数据列,基于归一化后的值),对账 L2 的比对依据。"""
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cursor_ahead(new_w, new_k: list | None, old_w, old_k: list | None) -> bool:
    """判断新游标是否严格前进(字典序)。完成态 (w, None) > 同水位任意中途键。"""
    nw, ow = str(new_w), str(old_w)
    if nw > ow:
        return True
    if nw < ow:
        return False
    if old_k is None:
        return False
    if new_k is None:
        return True
    return tuple(str(x) for x in new_k) > tuple(str(x) for x in old_k)


class LandingStore:
    RUN_TYPES = frozenset({
        "sync", "apply", "reconcile", "ingest", "validation", "publish", "rollback",
    })

    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self.con = sqlite3.connect(db_path)
        self.con.row_factory = sqlite3.Row
        # 管理端与抽取线程可能同时为同一 SQLite 文件创建 LandingStore。
        # SQLite 会缓存 schema;若多个连接并发执行 DDL/migrate,容易在 CI 上触发
        # "database schema has changed"。初始化阶段串行化,后续读写仍走 WAL 并发。
        with _SCHEMA_INIT_LOCK:
            # 对账 repair 批次等表依赖外键；SQLite 默认不执行外键约束，
            # 因此每个连接显式开启。
            self.con.execute("PRAGMA foreign_keys=ON")
            # WAL:写批次不阻塞管理端的只读连接(单写者 + 多读者场景);
            # busy_timeout:偶发写锁竞争时等待而非立即抛 "database is locked"。
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA busy_timeout=5000")
            self.con.executescript(_SYSTEM_DDL)
            self._migrate()

    @classmethod
    def open_readonly(cls, db_path: str | Path) -> "LandingStore":
        """打开已有状态库的只读连接(无 DDL/migrate);供备份校验等只读场景。"""
        inst = cls.__new__(cls)
        inst.db_path = str(db_path)
        inst.con = sqlite3.connect(f"file:{inst.db_path}?mode=ro", uri=True)
        inst.con.row_factory = sqlite3.Row
        inst.con.execute("PRAGMA foreign_keys=ON")
        inst.con.execute("PRAGMA busy_timeout=5000")
        return inst

    @classmethod
    def open_existing(cls, db_path: str | Path) -> "LandingStore":
        """打开已初始化落地库的读写连接，不在请求热路径执行 DDL/migrate。"""
        inst = cls.__new__(cls)
        inst.db_path = str(db_path)
        inst.con = sqlite3.connect(inst.db_path)
        inst.con.row_factory = sqlite3.Row
        inst.con.execute("PRAGMA foreign_keys=ON")
        inst.con.execute("PRAGMA busy_timeout=5000")
        return inst

    def backup_to(self, target: str | Path, *, overwrite: bool = False) -> Path:
        """使用 SQLite Online Backup API 生成一致备份并执行 integrity_check。"""
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            raise FileExistsError(f"备份已存在:{path}")
        destination = sqlite3.connect(path)
        try:
            self.con.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite 备份完整性检查失败:{result}")
        finally:
            destination.close()
        return path

    def prune_operational_history(
        self, *, retention_days: int = 90, receipt_days: int = 365,
    ) -> dict[str, int]:
        """清理可再生运行历史；发布版本、血缘、隔离和网关证据不在此删除。"""
        if retention_days < 7 or receipt_days < retention_days:
            raise ValueError("retention_days 至少 7，receipt_days 不得更短")
        operational_cutoff = (
            datetime.now() - timedelta(days=retention_days)).isoformat(
                timespec="seconds")
        receipt_cutoff = (
            datetime.now() - timedelta(days=receipt_days)).isoformat(
                timespec="seconds")
        counts: dict[str, int] = {}
        try:
            self.con.execute("BEGIN IMMEDIATE")
            old_runs = [
                row[0] for row in self.con.execute(
                    "SELECT id FROM d2a_sync_run "
                    "WHERE finished_at IS NOT NULL AND finished_at < ?",
                    (operational_cutoff,))
            ]
            if old_runs:
                marks = ",".join("?" for _ in old_runs)
                cur = self.con.execute(
                    f"DELETE FROM d2a_run_step WHERE run_id IN ({marks})",
                    old_runs)
                counts["run_steps"] = cur.rowcount
                cur = self.con.execute(
                    f"DELETE FROM d2a_sync_run WHERE id IN ({marks})",
                    old_runs)
                counts["runs"] = cur.rowcount
            for table, column, key in (
                ("d2a_audit_log", "ts", "audit"),
                ("d2a_http_push_log", "created_at", "push_log"),
            ):
                cur = self.con.execute(
                    f"DELETE FROM {table} WHERE {column} < ?",
                    (operational_cutoff,))
                counts[key] = cur.rowcount
            cur = self.con.execute(
                "DELETE FROM d2a_ingest_batch_receipt "
                "WHERE committed_at < ?", (receipt_cutoff,))
            counts["ingest_receipts"] = cur.rowcount
            cur = self.con.execute(
                "DELETE FROM d2a_reconcile_repair_batch "
                "WHERE created_at < ?", (receipt_cutoff,))
            counts["reconcile_batches"] = cur.rowcount
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return counts

    def cleanup_abandoned_staging(
        self, *, max_age_hours: float = 24.0,
    ) -> dict[str, int]:
        """回收断电/网络中断后长期 open 的快照和对账 staging。"""
        if max_age_hours < 1:
            raise ValueError("max_age_hours 必须至少为 1")
        cutoff = (
            datetime.now() - timedelta(hours=max_age_hours)
        ).isoformat(timespec="seconds")
        counts = {"snapshots": 0, "reconcile_repairs": 0, "generations": 0}
        try:
            self.con.execute("BEGIN IMMEDIATE")
            for row in self.con.execute(
                "SELECT source, table_name, snapshot_id, staging_table "
                "FROM d2a_snapshot WHERE status = 'open' "
                "AND COALESCE(last_activity_at, created_at) < ?",
                (cutoff,),
            ).fetchall():
                staging = str(row["staging_table"])
                expected = staging_raw_table_name(
                    row["source"], row["table_name"], row["snapshot_id"])
                if staging == expected:
                    quoted = staging.replace('"', '""')
                    self.con.execute(f'DROP TABLE IF EXISTS "{quoted}"')
                self.con.execute(
                    "UPDATE d2a_snapshot SET status = 'failed' "
                    "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                    (row["source"], row["table_name"], row["snapshot_id"]),
                )
                counts["snapshots"] += 1
            for row in self.con.execute(
                "SELECT source, table_name, repair_id, staging_table "
                "FROM d2a_reconcile_repair "
                "WHERE status = 'open' "
                "AND COALESCE(last_activity_at, created_at) < ?",
                (cutoff,),
            ).fetchall():
                staging = str(row["staging_table"])
                # repair staging 名由本库 sha256 生成，只清理符合格式的表。
                if re.fullmatch(r"d2a_reconcile_keys_[0-9a-f]+", staging):
                    self.con.execute(f'DROP TABLE IF EXISTS "{staging}"')
                self.con.execute(
                    "UPDATE d2a_reconcile_repair SET status = 'failed' "
                    "WHERE source = ? AND table_name = ? AND repair_id = ?",
                    (row["source"], row["table_name"], row["repair_id"]),
                )
                counts["reconcile_repairs"] += 1
            cur = self.con.execute(
                "UPDATE d2a_ingest_generation SET status = 'failed' "
                "WHERE status = 'open' AND COALESCE(last_activity_at, created_at) < ?",
                (cutoff,),
            )
            counts["generations"] = max(0, cur.rowcount)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return counts

    def _migrate(self) -> None:
        """幂等兼容升级:运行元数据与 v0.3 版本列/不变量。

        旧记录保持 NULL(类型/快照/版本引用未知),不批量回填猜测;重复初始化无副作用。
        """
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(d2a_sync_run)")}
        if "run_type" not in cols:
            self.con.execute("ALTER TABLE d2a_sync_run ADD COLUMN run_type TEXT")
        if "steps_recorded" not in cols:
            self.con.execute("ALTER TABLE d2a_sync_run ADD COLUMN steps_recorded INTEGER")
        if "dataset_version" not in cols:
            self.con.execute(
                "ALTER TABLE d2a_sync_run ADD COLUMN dataset_version TEXT")
        if "generation_id" not in cols:
            self.con.execute(
                "ALTER TABLE d2a_sync_run ADD COLUMN generation_id TEXT")
        try:
            self.con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_d2a_run_step_ingest_batch "
                "ON d2a_run_step (target, batch_id) "
                "WHERE kind = 'batch' AND batch_id IS NOT NULL")
        except sqlite3.IntegrityError:
            # 旧库若已被早期实现写入重复 batch 观测,不阻断启动;新写入仍按
            # ingest 端 source/table/batch 查询复用既有 step。
            self.con.execute(
                "CREATE INDEX IF NOT EXISTS idx_d2a_run_step_ingest_batch_lookup "
                "ON d2a_run_step (target, batch_id) "
                "WHERE kind = 'batch' AND batch_id IS NOT NULL")
        self.con.executescript(_TRANSFER_DDL)
        # v0.5:run_step 逐批进度字段
        step_cols = {r[1] for r in self.con.execute("PRAGMA table_info(d2a_run_step)")}
        if "batches" not in step_cols:
            self.con.execute(
                "ALTER TABLE d2a_run_step ADD COLUMN batches INTEGER")
        if "progressed_at" not in step_cols:
            self.con.execute(
                "ALTER TABLE d2a_run_step ADD COLUMN progressed_at TEXT")
        if "expected_rows" not in step_cols:
            # 同步前预估行数(COUNT 查询);NULL=旧运行或未预估,页面退化显示行数
            self.con.execute(
                "ALTER TABLE d2a_run_step ADD COLUMN expected_rows INTEGER")
        state_cols = {
            r[1] for r in self.con.execute("PRAGMA table_info(d2a_sync_state)")
        }
        if "key_columns_json" not in state_cols:
            self.con.execute(
                "ALTER TABLE d2a_sync_state ADD COLUMN key_columns_json TEXT")
        if "schema_name" not in state_cols:
            self.con.execute(
                "ALTER TABLE d2a_sync_state ADD COLUMN schema_name TEXT")
        snapshot_batch_cols = {
            r[1] for r in self.con.execute("PRAGMA table_info(d2a_snapshot_batch)")
        }
        snapshot_cols = {
            r[1] for r in self.con.execute("PRAGMA table_info(d2a_snapshot)")
        }
        if "schema_name" not in snapshot_cols:
            self.con.execute(
                "ALTER TABLE d2a_snapshot ADD COLUMN schema_name TEXT")
        if "last_activity_at" not in snapshot_cols:
            self.con.execute(
                "ALTER TABLE d2a_snapshot ADD COLUMN last_activity_at TEXT")
        if "payload_sha256" not in snapshot_batch_cols:
            self.con.execute(
                "ALTER TABLE d2a_snapshot_batch ADD COLUMN payload_sha256 TEXT")
        generation_cols = {
            r[1] for r in self.con.execute(
                "PRAGMA table_info(d2a_ingest_generation)")
        }
        if "apply_owner" not in generation_cols:
            self.con.execute(
                "ALTER TABLE d2a_ingest_generation ADD COLUMN apply_owner TEXT")
        if "apply_lease_until" not in generation_cols:
            self.con.execute(
                "ALTER TABLE d2a_ingest_generation "
                "ADD COLUMN apply_lease_until TEXT")
        if "last_activity_at" not in generation_cols:
            self.con.execute(
                "ALTER TABLE d2a_ingest_generation "
                "ADD COLUMN last_activity_at TEXT")
        repair_cols = {
            r[1] for r in self.con.execute(
                "PRAGMA table_info(d2a_reconcile_repair)")
        }
        if "last_activity_at" not in repair_cols:
            self.con.execute(
                "ALTER TABLE d2a_reconcile_repair "
                "ADD COLUMN last_activity_at TEXT")
        push_cols = {
            r[1] for r in self.con.execute(
                "PRAGMA table_info(d2a_http_push_log)")
        }
        for column, sql_type in (
            ("generation_id", "TEXT"),
            ("error_category", "TEXT"),
            ("retryable", "INTEGER"),
            ("receipt_received", "INTEGER"),
            ("idempotent_replay", "INTEGER"),
            ("receipt_digest", "TEXT"),
        ):
            if column not in push_cols:
                self.con.execute(
                    f"ALTER TABLE d2a_http_push_log ADD COLUMN {column} {sql_type}")
        # 告警静默(错误处理页):alert_key=source|table|category
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS d2a_alert_silence ("
            "alert_key TEXT PRIMARY KEY, "
            "silenced_until TEXT NOT NULL, "
            "created_at TEXT NOT NULL)")
        self.con.commit()

    # ---- raw 表 ----

    def raw_table_exists(self, source: str, table: str) -> bool:
        name = raw_table_name(source, table)
        row = self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone()
        return row is not None

    def raw_table_primary_key(self, source: str, table: str) -> list[str] | None:
        if not self.raw_table_exists(source, table):
            return None
        name = raw_table_name(source, table)
        rows = self.con.execute(f'PRAGMA table_info("{name}")').fetchall()
        pk = [r[1] for r in sorted(rows, key=lambda x: x[5]) if r[5] > 0]
        return pk or None

    def _assert_raw_schema_compatible(
        self, source: str, info: TableInfo,
    ) -> None:
        """增量 upsert 前拒绝列删除/新增/类型变化，避免旧值静默残留。"""
        table = raw_table_name(source, info.name)
        rows = self.con.execute(f'PRAGMA table_info("{table}")').fetchall()
        meta = {name for name, _ in _META_COLS}
        actual = {
            r[1]: str(r[2] or "").upper()
            for r in rows
            if r[1] not in meta
        }
        expected = {name: _TYPE_SQL[portable].upper() for name, portable in info.columns}
        if actual == expected:
            return
        added = sorted(set(expected) - set(actual))
        removed = sorted(set(actual) - set(expected))
        changed = sorted(
            name for name in set(actual) & set(expected)
            if actual[name] != expected[name]
        )
        raise ValueError(
            f"{info.name}: 源表结构与既有 raw 不兼容"
            f"(新增={added}, 删除={removed}, 类型变化={changed})；"
            "已阻止增量写入以避免旧字段静默残留，请确认结构后执行全量重建/backfill")

    def runtime_keys_recently_validated(
        self, source: str, table: str, fingerprint: str, *,
        max_age_hours: float = 24.0,
    ) -> bool:
        row = self.con.execute(
            "SELECT validated_at FROM d2a_runtime_key_validation "
            "WHERE source = ? AND table_name = ? AND strategy_fingerprint = ?",
            (source, table, fingerprint),
        ).fetchone()
        if row is None:
            return False
        try:
            age = datetime.now() - datetime.fromisoformat(row["validated_at"])
        except ValueError:
            return False
        return age.total_seconds() <= max_age_hours * 3600

    def record_runtime_key_validation(
        self, source: str, table: str, fingerprint: str,
    ) -> None:
        self.con.execute(
            "INSERT INTO d2a_runtime_key_validation "
            "(source, table_name, strategy_fingerprint, validated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, table_name, strategy_fingerprint) DO UPDATE SET "
            "validated_at = excluded.validated_at",
            (source, table, fingerprint, _now()),
        )
        self.con.commit()

    def schema_recently_validated(
        self, source: str, table: str, fingerprint: str, *,
        max_age_hours: float = 24.0,
    ) -> bool:
        row = self.con.execute(
            "SELECT validated_at FROM d2a_schema_validation "
            "WHERE source = ? AND table_name = ? AND schema_fingerprint = ?",
            (source, table, fingerprint),
        ).fetchone()
        if row is None:
            return False
        try:
            age = datetime.now() - datetime.fromisoformat(row["validated_at"])
        except ValueError:
            return False
        return age.total_seconds() <= max_age_hours * 3600

    def record_schema_validation(
        self, source: str, table: str, fingerprint: str,
    ) -> None:
        self.con.execute(
            "INSERT INTO d2a_schema_validation "
            "(source, table_name, schema_fingerprint, validated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, table_name, schema_fingerprint) DO UPDATE SET "
            "validated_at = excluded.validated_at",
            (source, table, fingerprint, _now()),
        )
        self.con.commit()

    def ensure_raw_table(self, source: str, info: TableInfo, *,
                        allow_empty_pk: bool = False,
                        commit: bool = True) -> None:
        if not info.pk and not allow_empty_pk:
            raise ValueError(
                f"{info.name}: 源表无主键,无法幂等 upsert;"
                "请在 binding notes 标注并走全量替换策略(E2)")
        self._assert_raw_source_identity(source, info, commit=commit)
        existing_pk = self.raw_table_primary_key(source, info.name)
        if existing_pk is None:
            if not self.raw_table_exists(source, info.name):
                self._create_raw_table(source, info, commit=commit)
                return
            # 无 PK 声明的已存在表(全量快照发布结果)
            if not info.pk:
                return
            existing_pk = []
        if existing_pk == list(info.pk):
            self._assert_raw_schema_compatible(source, info)
            return
        # 运行键变更:受控重建(先校验新键在现有数据上唯一)
        self._rebuild_raw_table_for_pk_change(
            source, info, existing_pk, commit=commit)

    def _assert_raw_source_identity(
        self, source: str, info: TableInfo, *, commit: bool = True,
    ) -> None:
        """增量写入时锁定物理 schema，避免游标重置后混写另一张同名表。"""
        schema = info.schema or ""
        identity = self.con.execute(
            "SELECT schema_name FROM d2a_raw_table_identity "
            "WHERE source = ? AND table_name = ?",
            (source, info.name),
        ).fetchone()
        if identity is not None:
            if identity["schema_name"] != schema:
                raise ValueError(
                    f"{info.name}: raw 当前绑定 schema="
                    f"{identity['schema_name'] or '(未声明)'}，本次为 "
                    f"{schema or '(未声明)'}；已阻止跨物理表增量混写，"
                    "请确认配置后执行 full_refresh 原子替换")
            return

        # 兼容升级旧库：优先采用既有同步游标记载的 schema；没有历史证据时
        # 才以当前已解析的 TableInfo 初始化身份账本。
        prior = self.con.execute(
            "SELECT schema_name FROM d2a_sync_state "
            "WHERE source = ? AND table_name = ?",
            (source, info.name),
        ).fetchone()
        if (
            prior is not None
            and prior["schema_name"] is not None
            and prior["schema_name"] != schema
        ):
            raise ValueError(
                f"{info.name}: 既有同步状态绑定 schema={prior['schema_name']}，"
                f"本次为 {schema or '(未声明)'}；已阻止跨物理表增量混写")
        self._record_raw_source_identity(source, info, commit=commit)

    def _record_raw_source_identity(
        self, source: str, info: TableInfo, *, commit: bool = True,
    ) -> None:
        self.con.execute(
            "INSERT INTO d2a_raw_table_identity "
            "(source, table_name, schema_name, recorded_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source, table_name) DO UPDATE SET "
            "schema_name = excluded.schema_name, "
            "recorded_at = excluded.recorded_at",
            (source, info.name, info.schema or "", _now()),
        )
        if commit:
            self.con.commit()

    def _create_raw_table(self, source: str, info: TableInfo,
                          table_name: str | None = None, *,
                          commit: bool = True) -> None:
        name = table_name or raw_table_name(source, info.name)
        cols = ",\n".join(
            [f'    "{c}" {_TYPE_SQL[t]}' for c, t in info.columns]
            + [f'    "{c}" {t}' for c, t in _META_COLS])
        if info.pk:
            pk = ", ".join(f'"{k}"' for k in info.pk)
            ddl = (f'CREATE TABLE IF NOT EXISTS "{name}" '
                   f"(\n{cols},\n    PRIMARY KEY ({pk})\n)")
        else:
            ddl = f'CREATE TABLE IF NOT EXISTS "{name}" (\n{cols}\n)'
        self.con.execute(ddl)
        if table_name is None:
            self._record_raw_source_identity(source, info, commit=False)
        if commit:
            self.con.commit()

    def _rebuild_raw_table_for_pk_change(
        self, source: str, info: TableInfo, old_pk: list[str], *,
        commit: bool = True,
    ) -> None:
        """将既有 raw 表主键迁移到新运行键;新键不唯一则明确阻断。"""
        table = raw_table_name(source, info.name)
        existing_cols = {
            r[1] for r in self.con.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        missing = [c for c in info.pk if c not in existing_cols]
        if missing:
            raise ValueError(
                f"{info.name}: 无法将落地表主键从 {old_pk} 切换为 {list(info.pk)};"
                f"缺少列 {missing}。请清空该 raw 表后重新同步。")
        key_sql = ", ".join(f'"{c}"' for c in info.pk)
        (dup,) = self.con.execute(
            f'SELECT COUNT(*) FROM ('
            f'SELECT {key_sql} FROM "{table}" '
            f'GROUP BY {key_sql} HAVING COUNT(*) > 1)'
        ).fetchone()
        if dup:
            raise ValueError(
                f"{info.name}: 无法将落地表主键从 {old_pk} 切换为 {list(info.pk)};"
                f"现有数据在新运行键上有 {dup} 组重复。"
                f"请修正源数据/配置键,或删除 raw 表后全量重抽。")
        tmp = f"{table}__pk_mig"
        self.con.execute(f'DROP TABLE IF EXISTS "{tmp}"')
        cols = ",\n".join(
            [f'    "{c}" {_TYPE_SQL[t]}' for c, t in info.columns]
            + [f'    "{c}" {t}' for c, t in _META_COLS])
        pk = ", ".join(f'"{k}"' for k in info.pk)
        self.con.execute(
            f'CREATE TABLE "{tmp}" (\n{cols},\n    PRIMARY KEY ({pk})\n)')
        # 复制业务列 + 元数据列(仅两边都存在的)
        new_cols = [c for c, _ in info.columns] + [c for c, _ in _META_COLS]
        copy_cols = [c for c in new_cols if c in existing_cols]
        col_sql = ", ".join(f'"{c}"' for c in copy_cols)
        self.con.execute(
            f'INSERT INTO "{tmp}" ({col_sql}) SELECT {col_sql} FROM "{table}"')
        self.con.execute(f'DROP TABLE "{table}"')
        self.con.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
        if commit:
            self.con.commit()
            self.log_audit(
                source, "raw_pk_migrate",
                f"table={info.name} from={','.join(old_pk)} to={','.join(info.pk)}",
                0, 0.0)

    def upsert_rows(
        self, source: str, info: TableInfo, rows: list[dict], batch_id: str, *,
        commit: bool = True,
    ) -> int:
        if not rows:
            return 0
        if not info.pk:
            raise ValueError(f"{info.name}: upsert 需要运行键")
        table = raw_table_name(source, info.name)
        return self._write_rows_into(
            table, info, rows, batch_id, upsert=True, commit=commit)

    def _write_rows_into(
        self, table: str, info: TableInfo, rows: list[dict], batch_id: str, *,
        upsert: bool, commit: bool = True,
    ) -> int:
        if not rows:
            return 0
        cols = [c for c, _ in info.columns]
        extracted_at = _now()
        normalized = [{c: normalize_value(r.get(c)) for c in cols} for r in rows]
        payload = [
            {**n, "_d2a_batch_id": batch_id, "_d2a_extracted_at": extracted_at,
             "_d2a_row_hash": row_hash(n)}
            for n in normalized
        ]
        all_cols = cols + ["_d2a_batch_id", "_d2a_extracted_at", "_d2a_row_hash"]
        col_sql = ", ".join(f'"{c}"' for c in all_cols)
        val_sql = ", ".join(f":{c}" for c in all_cols)
        if upsert and info.pk:
            pk_sql = ", ".join(f'"{k}"' for k in info.pk)
            update_sql = ", ".join(
                f'"{c}" = excluded."{c}"' for c in all_cols) + ', "_d2a_deleted_at" = NULL'
            sql = (f'INSERT INTO "{table}" ({col_sql}) VALUES ({val_sql}) '
                   f"ON CONFLICT ({pk_sql}) DO UPDATE SET {update_sql}")
        else:
            sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({val_sql})'
        self.con.executemany(sql, payload)
        if commit:
            self.con.commit()
        return len(rows)

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
        """在独立写事务中校验 generation 并刷新活动租约。"""
        try:
            self.con.execute("BEGIN IMMEDIATE")
            self._touch_ingest_generation_uncommitted(
                source, generation_id, table)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

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
        if generation["status"] in ("committed", "applying", "applied"):
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

    # ---- 全量快照 staging / 原子发布 ----

    def begin_snapshot(
        self, source: str, info: TableInfo, snapshot_id: str, *,
        generation_id: str | None = None,
    ) -> dict:
        """创建或恢复 open 状态的 snapshot staging 表。"""
        sid = _safe_snapshot_id(snapshot_id)
        staging = staging_raw_table_name(source, info.name, sid)
        try:
            self.con.execute("BEGIN IMMEDIATE")
            if generation_id is not None:
                self._touch_ingest_generation_uncommitted(
                    source, generation_id, info.name)
            row = self.con.execute(
                "SELECT status, staging_table, schema_name FROM d2a_snapshot "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                (source, info.name, sid)).fetchone()
            if (
                row is not None
                and row["schema_name"] is not None
                and row["schema_name"] != (info.schema or "")
            ):
                raise ValueError(
                    f"{info.name}: snapshot '{sid}' 已绑定 schema="
                    f"{row['schema_name'] or '(未声明)'}，不能以 "
                    f"{info.schema or '(未声明)'} 重放")
            if row is not None and row["status"] == "published":
                result = {
                    "snapshot_id": sid, "status": "published",
                    "staging_table": row["staging_table"], "duplicate": True,
                }
                self.con.commit()
                return result
            if row is not None and row["status"] == "open":
                exists = self.con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (row["staging_table"],)).fetchone()
                if exists:
                    self.con.execute(
                        "UPDATE d2a_snapshot SET last_activity_at = ? "
                        "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                        (_now(), source, info.name, sid),
                    )
                    result = {
                        "snapshot_id": sid, "status": "open",
                        "staging_table": row["staging_table"], "duplicate": True,
                    }
                    self.con.commit()
                    return result
            self.con.execute(f'DROP TABLE IF EXISTS "{staging}"')
            self._create_raw_table(
                source, info, table_name=staging, commit=False)
            now = _now()
            self.con.execute(
                "INSERT INTO d2a_snapshot "
                "(source, table_name, snapshot_id, status, staging_table, "
                "schema_name, created_at, last_activity_at) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?, ?) "
                "ON CONFLICT (source, table_name, snapshot_id) DO UPDATE SET "
                "status = 'open', staging_table = excluded.staging_table, "
                "schema_name = excluded.schema_name, "
                "expected_rows = NULL, expected_batches = NULL, published_at = NULL, "
                "created_at = excluded.created_at, "
                "last_activity_at = excluded.last_activity_at",
                (source, info.name, sid, staging, info.schema or "", now, now))
            self.con.execute(
                "DELETE FROM d2a_snapshot_batch "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                (source, info.name, sid))
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {"snapshot_id": sid, "status": "open",
                "staging_table": staging, "duplicate": False}

    def write_snapshot_batch(
        self, source: str, info: TableInfo, snapshot_id: str,
        batch_id: str, rows: list[dict], payload_sha256: str | None = None, *,
        generation_id: str | None = None,
    ) -> dict:
        """写入 staging;同一 snapshot_id+batch_id 幂等去重。"""
        sid = _safe_snapshot_id(snapshot_id)
        try:
            self.con.execute("BEGIN IMMEDIATE")
            if generation_id is not None:
                self._touch_ingest_generation_uncommitted(
                    source, generation_id, info.name)
            snap = self.con.execute(
                "SELECT status, staging_table FROM d2a_snapshot "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                (source, info.name, sid)).fetchone()
            if snap is None or snap["status"] != "open":
                raise ValueError(
                    f"{info.name}: snapshot '{sid}' 未 begin 或已关闭(status="
                    f"{None if snap is None else snap['status']})")
            existing = self.con.execute(
                "SELECT rows, payload_sha256 FROM d2a_snapshot_batch "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ? "
                "AND batch_id = ?",
                (source, info.name, sid, batch_id)).fetchone()
            if existing is not None:
                if (
                    payload_sha256 is not None
                    and existing["payload_sha256"] is not None
                    and existing["payload_sha256"] != payload_sha256
                ):
                    raise ValueError(
                        f"{info.name}: snapshot batch_id '{batch_id}' "
                        "已提交但载荷摘要不同")
                self.con.execute(
                    "UPDATE d2a_snapshot SET last_activity_at = ? "
                    "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                    (_now(), source, info.name, sid),
                )
                result = {"ingested": existing["rows"], "duplicate": True,
                          "batch_id": batch_id, "snapshot_id": sid}
                self.con.commit()
                return result
            staging = snap["staging_table"]
            n = self._write_rows_into(
                staging, info, rows, batch_id, upsert=bool(info.pk), commit=False)
            now = _now()
            self.con.execute(
                "INSERT INTO d2a_snapshot_batch "
                "(source, table_name, snapshot_id, batch_id, rows, payload_sha256, "
                "status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'ok', ?)",
                (source, info.name, sid, batch_id, n, payload_sha256, now))
            self.con.execute(
                "UPDATE d2a_snapshot SET last_activity_at = ? "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                (now, source, info.name, sid),
            )
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {"ingested": n, "duplicate": False,
                "batch_id": batch_id, "snapshot_id": sid}

    def complete_snapshot(
        self, source: str, info: TableInfo, snapshot_id: str,
        expected_rows: int, expected_batches: int, *,
        generation_id: str | None = None,
    ) -> dict:
        """核对批次数/行数后原子发布 staging → 当前 raw 表。"""
        sid = _safe_snapshot_id(snapshot_id)
        live = raw_table_name(source, info.name)
        backup = f"{live}__prev"
        try:
            self.con.execute("BEGIN IMMEDIATE")
            if generation_id is not None:
                self._touch_ingest_generation_uncommitted(
                    source, generation_id, info.name)
            snap = self.con.execute(
                "SELECT * FROM d2a_snapshot "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                (source, info.name, sid)).fetchone()
            if snap is None:
                raise ValueError(f"{info.name}: snapshot '{sid}' 不存在")
            if snap["status"] == "published":
                if generation_id is not None:
                    self._record_ingest_table_commit_uncommitted(
                        source, generation_id, info.name,
                        expected_rows, expected_batches)
                result = {
                    "completed": True, "duplicate": True, "snapshot_id": sid,
                    "rows": snap["expected_rows"],
                    "batches": snap["expected_batches"],
                }
                self.con.commit()
                return result
            if snap["status"] != "open":
                raise ValueError(
                    f"{info.name}: snapshot '{sid}' 状态为 {snap['status']}")
            batch_count, row_sum = self.con.execute(
                "SELECT COUNT(*), COALESCE(SUM(rows), 0) "
                "FROM d2a_snapshot_batch "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                (source, info.name, sid)).fetchone()
            staging = snap["staging_table"]
            (actual,) = self.con.execute(
                f'SELECT COUNT(*) FROM "{staging}"').fetchone()
            if batch_count != expected_batches:
                raise ValueError(
                    f"{info.name}: snapshot 批次数不符: got {batch_count}, "
                    f"want {expected_batches}")
            if row_sum != expected_rows or actual != expected_rows:
                raise ValueError(
                    f"{info.name}: snapshot 行数不符: batches_sum={row_sum}, "
                    f"staging={actual}, want={expected_rows}")
            self.con.execute(f'DROP TABLE IF EXISTS "{backup}"')
            live_exists = self.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (live,)).fetchone()
            if live_exists:
                self.con.execute(f'ALTER TABLE "{live}" RENAME TO "{backup}"')
            self.con.execute(f'ALTER TABLE "{staging}" RENAME TO "{live}"')
            self.con.execute(f'DROP TABLE IF EXISTS "{backup}"')
            now = _now()
            self.con.execute(
                "UPDATE d2a_snapshot SET status = 'published', "
                "expected_rows = ?, expected_batches = ?, published_at = ?, "
                "last_activity_at = ?, staging_table = ? "
                "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
                (expected_rows, expected_batches, now, now, live,
                 source, info.name, sid))
            self._record_raw_source_identity(source, info, commit=False)
            if generation_id is not None:
                self._record_ingest_table_commit_uncommitted(
                    source, generation_id, info.name,
                    expected_rows, expected_batches)
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        self.log_audit(
            source, "snapshot_publish",
            f"table={info.name} snapshot={sid} rows={expected_rows}",
            expected_rows, 0.0)
        return {
            "completed": True, "duplicate": False, "snapshot_id": sid,
            "rows": expected_rows, "batches": expected_batches,
        }

    def abort_snapshot(self, source: str, table: str, snapshot_id: str) -> None:
        """失败清理:丢弃 staging,保留当前 raw。"""
        sid = _safe_snapshot_id(snapshot_id)
        snap = self.con.execute(
            "SELECT status, staging_table FROM d2a_snapshot "
            "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
            (source, table, sid)).fetchone()
        if snap is None or snap["status"] == "published":
            return
        staging = snap["staging_table"]
        if staging != raw_table_name(source, table):
            self.con.execute(f'DROP TABLE IF EXISTS "{staging}"')
        self.con.execute(
            "UPDATE d2a_snapshot SET status = 'failed' "
            "WHERE source = ? AND table_name = ? AND snapshot_id = ?",
            (source, table, sid))
        self.con.commit()

    def count(self, source: str, table: str, active_only: bool = False) -> int:
        where = " WHERE _d2a_deleted_at IS NULL" if active_only else ""
        (n,) = self.con.execute(
            f'SELECT COUNT(*) FROM "{raw_table_name(source, table)}"{where}').fetchone()
        return n

    # ---- 对账支撑(E3)----

    def segment_stats(self, source: str, table: str, wm_col: str, start, end) -> dict:
        """与适配器 segment_stats 同口径:活跃行的 COUNT + MAX(水位)。"""
        row = self.con.execute(
            f'SELECT COUNT(*) AS c, MAX("{wm_col}") AS m '
            f'FROM "{raw_table_name(source, table)}" '
            f'WHERE "{wm_col}" >= ? AND "{wm_col}" < ? AND _d2a_deleted_at IS NULL',
            (start, end)).fetchone()
        return {"count": row["c"], "max": row["m"]}

    def reconcile_stats(
        self, source: str, table: str, wm_col: str | None = None,
        start=None, end=None,
    ) -> dict:
        """E6b 平台侧统计；无水位时返回全表活跃行数。"""
        if not self.raw_table_exists(source, table):
            raise ValueError(f"{table}: 平台 raw 尚不存在，不能对账")
        if wm_col is None:
            return {
                "count": self.count(source, table, active_only=True),
                "max": None,
            }
        if start is None and end is None:
            row = self.con.execute(
                f'SELECT COUNT(*) AS c, MIN("{wm_col}") AS lo, '
                f'MAX("{wm_col}") AS hi '
                f'FROM "{raw_table_name(source, table)}" '
                "WHERE _d2a_deleted_at IS NULL",
            ).fetchone()
            return {
                "count": row["c"], "min": row["lo"], "max": row["hi"],
            }
        return self.segment_stats(source, table, wm_col, start, end)

    def begin_reconcile_repair(
        self, source: str, info: TableInfo, repair_id: str,
        wm_col: str | None = None, start=None, end=None, *,
        generation_id: str | None = None,
    ) -> dict:
        """开启 E6b L2，并建立只含运行键的落地侧 staging 表。"""
        rid = _safe_snapshot_id(repair_id)
        if not info.pk:
            raise ValueError(f"{info.name}: 对账修复需要运行键")
        if wm_col is not None and (
            start is None or end is None or str(start) >= str(end)
        ):
            raise ValueError(f"{info.name}: 对账分段边界无效")
        # ensure_raw_table 可能执行兼容 DDL；先拒绝明显失效的 generation，
        # 随后仍在 repair 事务内再次校验，防止两者之间关闭的竞态。
        if generation_id is not None:
            self.touch_ingest_generation(source, generation_id, info.name)
        self.ensure_raw_table(source, info)
        columns_json = json.dumps(
            info.columns, ensure_ascii=False, separators=(",", ":"))
        pk_json = json.dumps(info.pk, ensure_ascii=False, separators=(",", ":"))
        manifest = (
            info.schema or "", columns_json, pk_json, wm_col, start, end)
        digest = hashlib.sha256(
            f"{source}\0{info.name}\0{rid}".encode("utf-8")).hexdigest()[:24]
        staging = f"d2a_reconcile_keys_{digest}"
        key_types = dict(info.columns)
        key_cols = ", ".join(
            f'"{key}" {_TYPE_SQL[key_types[key]]} NOT NULL'
            for key in info.pk)
        pk_sql = ", ".join(f'"{key}"' for key in info.pk)
        try:
            self.con.execute("BEGIN IMMEDIATE")
            if generation_id is not None:
                self._touch_ingest_generation_uncommitted(
                    source, generation_id, info.name)
            existing = self.con.execute(
                "SELECT * FROM d2a_reconcile_repair "
                "WHERE source = ? AND table_name = ? AND repair_id = ?",
                (source, info.name, rid),
            ).fetchone()
            if existing is not None:
                stored = (
                    existing["schema_name"], existing["columns_json"],
                    existing["pk_json"], existing["watermark_col"],
                    existing["segment_start"], existing["segment_end"])
                if stored != manifest:
                    raise ValueError(
                        f"{info.name}: repair_id '{rid}' 的结构或分段边界不一致")
                if existing["status"] in ("open", "completed"):
                    if existing["status"] == "open":
                        self.con.execute(
                            "UPDATE d2a_reconcile_repair "
                            "SET last_activity_at = ? WHERE source = ? "
                            "AND table_name = ? AND repair_id = ?",
                            (_now(), source, info.name, rid),
                        )
                    result = {
                        "repair_id": rid, "status": existing["status"],
                        "duplicate": True,
                    }
                    self.con.commit()
                    return result
            self.con.execute(f'DROP TABLE IF EXISTS "{staging}"')
            self.con.execute(
                f'CREATE TABLE "{staging}" '
                f"({key_cols}, PRIMARY KEY ({pk_sql})) WITHOUT ROWID")
            self.con.execute(
                "INSERT INTO d2a_reconcile_repair "
                "(source, table_name, repair_id, status, staging_table, "
                "schema_name, columns_json, pk_json, watermark_col, "
                "segment_start, segment_end, created_at, last_activity_at) "
                "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source, table_name, repair_id) DO UPDATE SET "
                "status = 'open', staging_table = excluded.staging_table, "
                "created_at = excluded.created_at, "
                "last_activity_at = excluded.last_activity_at, completed_at = NULL, "
                "rows = NULL, batches = NULL, soft_deleted = NULL",
                (source, info.name, rid, staging, info.schema or "",
                 columns_json, pk_json, wm_col, start, end, _now(), _now()),
            )
            self.con.execute(
                "DELETE FROM d2a_reconcile_repair_batch "
                "WHERE source = ? AND table_name = ? AND repair_id = ?",
                (source, info.name, rid),
            )
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {"repair_id": rid, "status": "open", "duplicate": False}

    def write_reconcile_repair_batch(
        self, source: str, table: str, repair_id: str,
        batch_id: str, rows: list[dict], payload_sha256: str, *,
        generation_id: str | None = None,
    ) -> dict:
        """原始行 upsert、运行键 staging 和批次回执同事务提交。"""
        rid = _safe_snapshot_id(repair_id)
        try:
            self.con.execute("BEGIN IMMEDIATE")
            if generation_id is not None:
                self._touch_ingest_generation_uncommitted(
                    source, generation_id, table)
            repair = self.con.execute(
                "SELECT * FROM d2a_reconcile_repair "
                "WHERE source = ? AND table_name = ? AND repair_id = ?",
                (source, table, rid),
            ).fetchone()
            if repair is None or repair["status"] != "open":
                raise ValueError(f"{table}: repair '{rid}' 未开启或已关闭")
            columns = [(str(c), str(t)) for c, t in json.loads(
                repair["columns_json"])]
            pk = [str(c) for c in json.loads(repair["pk_json"])]
            info = TableInfo(
                name=table, columns=columns, pk=pk,
                schema=repair["schema_name"] or None)
            allowed = {c for c, _ in columns}
            if any(set(row) - allowed for row in rows):
                raise ValueError(f"{table}: repair rows 含未声明字段")
            if any(any(row.get(k) is None for k in pk) for row in rows):
                raise ValueError(f"{table}: repair rows 运行键含 NULL")
            receipt = self.con.execute(
                "SELECT payload_sha256, rows "
                "FROM d2a_reconcile_repair_batch "
                "WHERE source = ? AND table_name = ? AND repair_id = ? "
                "AND batch_id = ?",
                (source, table, rid, batch_id),
            ).fetchone()
            if receipt is not None:
                if receipt["payload_sha256"] != payload_sha256:
                    raise ValueError(
                        f"{table}: repair batch_id '{batch_id}' 摘要不一致")
                self.con.execute(
                    "UPDATE d2a_reconcile_repair SET last_activity_at = ? "
                    "WHERE source = ? AND table_name = ? AND repair_id = ?",
                    (_now(), source, table, rid),
                )
                self.con.commit()
                return {
                    "ingested": receipt["rows"], "duplicate": True,
                    "payload_sha256": receipt["payload_sha256"],
                }
            n = self.upsert_rows(
                source, info, rows, f"reconcile-{rid}", commit=False)
            staging = repair["staging_table"]
            key_sql = ", ".join(f'"{k}"' for k in pk)
            value_sql = ", ".join("?" for _ in pk)
            self.con.executemany(
                f'INSERT OR IGNORE INTO "{staging}" ({key_sql}) '
                f"VALUES ({value_sql})",
                [tuple(normalize_value(row[k]) for k in pk) for row in rows],
            )
            self.con.execute(
                "INSERT INTO d2a_reconcile_repair_batch "
                "(source, table_name, repair_id, batch_id, payload_sha256, "
                "rows, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, table, rid, batch_id, payload_sha256, n, _now()),
            )
            self.con.execute(
                "UPDATE d2a_reconcile_repair SET last_activity_at = ? "
                "WHERE source = ? AND table_name = ? AND repair_id = ?",
                (_now(), source, table, rid),
            )
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {
            "ingested": n, "duplicate": False,
            "payload_sha256": payload_sha256,
        }

    def complete_reconcile_repair(
        self, source: str, table: str, repair_id: str,
        expected_rows: int, expected_batches: int, *,
        generation_id: str | None = None,
    ) -> dict:
        """按 staging 运行键做 SQL 反连接并软删，内存占用与表大小无关。"""
        rid = _safe_snapshot_id(repair_id)
        try:
            self.con.execute("BEGIN IMMEDIATE")
            if generation_id is not None:
                self._touch_ingest_generation_uncommitted(
                    source, generation_id, table)
            repair = self.con.execute(
                "SELECT * FROM d2a_reconcile_repair "
                "WHERE source = ? AND table_name = ? AND repair_id = ?",
                (source, table, rid),
            ).fetchone()
            if repair is None:
                raise ValueError(f"{table}: repair '{rid}' 不存在")
            if repair["status"] == "completed":
                result = {
                    "completed": True, "duplicate": True,
                    "soft_deleted": repair["soft_deleted"] or 0,
                }
                self.con.commit()
                return result
            if repair["status"] != "open":
                raise ValueError(
                    f"{table}: repair '{rid}' 状态为 {repair['status']}")
            batch_count, row_sum = self.con.execute(
                "SELECT COUNT(*), COALESCE(SUM(rows), 0) "
                "FROM d2a_reconcile_repair_batch "
                "WHERE source = ? AND table_name = ? AND repair_id = ?",
                (source, table, rid),
            ).fetchone()
            if batch_count != expected_batches or row_sum != expected_rows:
                raise ValueError(
                    f"{table}: repair 完成核验失败: batches={batch_count}/"
                    f"{expected_batches}, rows={row_sum}/{expected_rows}")
            pk = [str(c) for c in json.loads(repair["pk_json"])]
            staging = repair["staging_table"]
            live = raw_table_name(source, table)
            join = " AND ".join(
                f'k."{key}" = r."{key}"' for key in pk)
            segment = ""
            params: list[object] = [_now()]
            if repair["watermark_col"] is not None:
                segment = (
                    f' AND r."{repair["watermark_col"]}" >= ?'
                    f' AND r."{repair["watermark_col"]}" < ?')
                params.extend([repair["segment_start"], repair["segment_end"]])
            cur = self.con.execute(
                f'UPDATE "{live}" AS r SET _d2a_deleted_at = ? '
                "WHERE r._d2a_deleted_at IS NULL"
                f"{segment} AND NOT EXISTS ("
                f'SELECT 1 FROM "{staging}" AS k WHERE {join})',
                tuple(params),
            )
            soft_deleted = max(0, cur.rowcount)
            self.con.execute(f'DROP TABLE "{staging}"')
            self.con.execute(
                "UPDATE d2a_reconcile_repair SET status = 'completed', "
                "rows = ?, batches = ?, soft_deleted = ?, completed_at = ?, "
                "last_activity_at = ? "
                "WHERE source = ? AND table_name = ? AND repair_id = ?",
                (expected_rows, expected_batches, soft_deleted, _now(), _now(),
                 source, table, rid),
            )
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {
            "completed": True, "duplicate": False,
            "soft_deleted": soft_deleted,
        }

    def abort_reconcile_repair(
        self, source: str, table: str, repair_id: str,
    ) -> None:
        rid = _safe_snapshot_id(repair_id)
        repair = self.con.execute(
            "SELECT status, staging_table FROM d2a_reconcile_repair "
            "WHERE source = ? AND table_name = ? AND repair_id = ?",
            (source, table, rid),
        ).fetchone()
        if repair is None or repair["status"] != "open":
            return
        self.con.execute(f'DROP TABLE IF EXISTS "{repair["staging_table"]}"')
        self.con.execute(
            "UPDATE d2a_reconcile_repair SET status = 'failed' "
            "WHERE source = ? AND table_name = ? AND repair_id = ?",
            (source, table, rid),
        )
        self.con.commit()

    def active_key_tuples(
        self, source: str, table: str, pk_cols: list[str],
        wm_col: str | None = None, start=None, end=None,
    ) -> set[tuple]:
        """活跃行的完整运行键元组集合。"""
        if not pk_cols:
            return set()
        cols_sql = ", ".join(f'"{c}"' for c in pk_cols)
        sql = (f'SELECT {cols_sql} FROM "{raw_table_name(source, table)}" '
               f"WHERE _d2a_deleted_at IS NULL")
        params: tuple = ()
        if wm_col is not None:
            sql += f' AND "{wm_col}" >= ? AND "{wm_col}" < ?'
            params = (start, end)
        return {
            tuple(r[c] for c in pk_cols)
            for r in self.con.execute(sql, params)
        }

    def min_watermark(self, source: str, table: str, wm_col: str) -> str | None:
        (m,) = self.con.execute(
            f'SELECT MIN("{wm_col}") FROM "{raw_table_name(source, table)}"').fetchone()
        return m

    # ---- 水位 / 复合键游标状态 ----

    def get_high_water(self, source: str, table: str) -> str | None:
        """返回已提交水位标量(游标中的 w);兼容旧调用。"""
        cursor = self.get_sync_cursor(source, table)
        if cursor is None:
            return None
        w, _keys = cursor
        return None if w is None else str(w)

    def list_sync_watermarks(self, source: str) -> list[dict]:
        """对外水位视图:仅返回解码后的 watermark 标量,不含复合键游标。"""
        from .table import decode_keyset_cursor

        rows = self.con.execute(
            "SELECT table_name, watermark_col, high_water, last_run_at "
            "FROM d2a_sync_state WHERE source = ? ORDER BY table_name",
            (source,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            high = r["high_water"]
            if high is not None:
                w, _keys = decode_keyset_cursor(high)
                high = None if w is None else str(w)
            out.append({
                "table_name": r["table_name"],
                "watermark_col": r["watermark_col"],
                "high_water": high,
                "last_run_at": r["last_run_at"],
            })
        return out

    def get_sync_cursor(
        self,
        source: str,
        table: str,
        *,
        watermark_col: str | None = None,
        key_columns: list[str] | None = None,
        schema: str | None = None,
    ) -> tuple[object, list | None] | None:
        """返回游标，并可校验产生该游标的抽取策略仍兼容。"""
        from .table import decode_keyset_cursor
        row = self.con.execute(
            "SELECT high_water, watermark_col, key_columns_json, schema_name "
            "FROM d2a_sync_state WHERE source = ? AND table_name = ?",
            (source, table)).fetchone()
        if row is None or row["high_water"] is None:
            return None
        if watermark_col is not None and row["watermark_col"] != watermark_col:
            raise ValueError(
                f"{table}: watermark 已从 '{row['watermark_col']}' 改为 "
                f"'{watermark_col}'，旧游标不兼容；请先执行显式游标重置/backfill")
        if key_columns is not None and row["key_columns_json"]:
            stored_keys = json.loads(row["key_columns_json"])
            if stored_keys != list(key_columns):
                raise ValueError(
                    f"{table}: 运行键已从 {stored_keys} 改为 {list(key_columns)}，"
                    "旧游标不兼容；请先执行显式游标重置/backfill")
        if schema is not None and row["schema_name"] is not None:
            if row["schema_name"] != schema:
                raise ValueError(
                    f"{table}: schema 已从 '{row['schema_name']}' 改为 '{schema}'，"
                    "旧游标不兼容；请先执行显式游标重置/backfill")
        return decode_keyset_cursor(row["high_water"])

    def reset_sync_cursor(self, source: str, table: str) -> bool:
        """显式删除单表游标，供抽取策略变更后从配置下界重新同步。

        只清理游标状态，不删除 raw 数据；下一轮同步仍通过 upsert/主键迁移
        保持落地表可用。返回是否实际删除了游标。
        """
        cur = self.con.execute(
            "DELETE FROM d2a_sync_state WHERE source = ? AND table_name = ?",
            (source, table),
        )
        self.con.commit()
        return cur.rowcount > 0

    def set_high_water(self, source: str, table: str, watermark_col: str,
                       high_water: str | None, batch_id: str) -> None:
        """兼容旧接口:写入已完成水位游标(无中途键)。"""
        self.set_sync_cursor(
            source, table, watermark_col, high_water, None, batch_id)

    def set_sync_cursor(
        self,
        source: str,
        table: str,
        watermark_col: str,
        watermark,
        key_values: list | None,
        batch_id: str,
        *,
        force: bool = False,
        commit: bool = True,
        key_columns: list[str] | None = None,
        schema: str | None = None,
    ) -> None:
        """原子写入统一游标。默认只前进不后退。

        key_values=None 表示水位整表完成;list 表示已完成到该复合键边界。
        commit=False 时不提交,供外层在单事务中与本游标一起提交。
        """
        from .table import encode_keyset_cursor

        if watermark is None:
            return
        new_raw = encode_keyset_cursor(watermark, key_values)
        old = self.get_sync_cursor(source, table)
        if not force and old is not None:
            old_w, old_k = old
            if not _cursor_ahead(watermark, key_values, old_w, old_k):
                return
        self.con.execute(
            "INSERT INTO d2a_sync_state "
            "(source, table_name, watermark_col, high_water, last_run_at, last_batch_id, "
            "key_columns_json, schema_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, table_name) DO UPDATE SET "
            "watermark_col = excluded.watermark_col, high_water = excluded.high_water, "
            "last_run_at = excluded.last_run_at, last_batch_id = excluded.last_batch_id, "
            "key_columns_json = COALESCE(excluded.key_columns_json, key_columns_json), "
            "schema_name = COALESCE(excluded.schema_name, schema_name)",
            (source, table, watermark_col, new_raw, _now(), batch_id,
             json.dumps(list(key_columns)) if key_columns is not None else None,
             schema))
        if commit:
            self.con.commit()

    # ---- 审计与运行汇总 ----

    def log_audit(self, source: str, action: str, sql: str, rows: int,
                  duration_ms: float, batch_id: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO d2a_audit_log (ts, source, action, sql, rows, duration_ms, batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), source, action, sql, rows, round(duration_ms, 2), batch_id))
        self.con.commit()

    # ---- HTTP 推送记录 ----

    def record_push_log(
        self, source: str, step_kind: str, table: str, mode: str,
        *, run_id: int | None = None, batch_id: str | None = None,
        rows_count: int | None = None, status: str = "ok",
        error_detail: str | None = None, retry_count: int = 0,
        duration_ms: float | None = None,
        generation_id: str | None = None,
        error_category: str | None = None,
        retryable: bool | None = None,
        receipt_received: bool | None = None,
        idempotent_replay: bool | None = None,
        receipt_digest: str | None = None,
    ) -> int:
        """记录一次 HTTP 推送;供 HttpPushSink 调用。"""
        cur = self.con.execute(
            "INSERT INTO d2a_http_push_log "
            "(source, run_id, step_kind, table_name, mode, batch_id, rows_count, "
            "status, error_detail, retry_count, duration_ms, generation_id, "
            "error_category, retryable, receipt_received, idempotent_replay, "
            "receipt_digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source, run_id, step_kind, table, mode, batch_id, rows_count,
             status, error_detail, retry_count, duration_ms, generation_id,
             error_category, None if retryable is None else int(retryable),
             None if receipt_received is None else int(receipt_received),
             None if idempotent_replay is None else int(idempotent_replay),
             receipt_digest, _now()))
        self.con.commit()
        return cur.lastrowid

    def set_run_generation(self, run_id: int, generation_id: str) -> None:
        """把中间机运行与跨机 generation 关联，供状态与排障页面追踪。"""
        self.con.execute(
            "UPDATE d2a_sync_run SET generation_id = ? WHERE id = ?",
            (generation_id, run_id))
        self.con.commit()

    def list_push_logs(
        self, source: str | None = None, limit: int = 50, offset: int = 0,
        table: str | None = None,
    ) -> tuple[list[sqlite3.Row], int]:
        """列出推送记录,返回 (rows, total)。可按 source / table 过滤。"""
        conds: list[str] = []
        params: list = []
        if source:
            conds.append("source = ?")
            params.append(source)
        if table:
            conds.append("table_name = ?")
            params.append(table)
        where = f"WHERE {' AND '.join(conds)}" if conds else ""
        (total,) = self.con.execute(
            f"SELECT COUNT(*) FROM d2a_http_push_log {where}", params).fetchone()
        rows = self.con.execute(
            f"SELECT * FROM d2a_http_push_log {where} "
            f"ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
        return rows, total

    def push_log_table_summaries(self, source: str | None = None) -> list[dict]:
        """按表汇总推送状态:每 (source, table) 取最近批次,判定是否推送完成。

        status:completed(complete_table 已成功)/ failed(最近批次含失败步骤)/
        pushing(批次未完结)。rows 仅累计 write 成功行数(与批次进度口径一致)。
        """
        where = "WHERE batch_id IS NOT NULL"
        params: list = []
        if source:
            where += " AND source = ?"
            params.append(source)
        latest = self.con.execute(
            f"SELECT source, table_name, batch_id, MAX(created_at) AS last_at "
            f"FROM d2a_http_push_log {where} "
            f"GROUP BY source, table_name, batch_id", params).fetchall()
        # 每表保留 last_at 最新的批次
        by_table: dict[tuple[str, str], sqlite3.Row] = {}
        for r in latest:
            key = (r["source"], r["table_name"])
            if key not in by_table or r["last_at"] > by_table[key]["last_at"]:
                by_table[key] = r
        summaries: list[dict] = []
        for (src, table), r in by_table.items():
            progress = self.push_log_batch_progress(src, table, r["batch_id"])
            meta_row = self.con.execute(
                "SELECT mode, MAX(generation_id) AS generation_id, "
                "MAX(retry_count) AS retry_count, "
                "MAX(receipt_received) AS receipt_received, "
                "MAX(idempotent_replay) AS idempotent_replay "
                "FROM d2a_http_push_log WHERE source = ? AND table_name = ? "
                "AND batch_id = ?",
                (src, table, r["batch_id"])).fetchone()
            error_row = self.con.execute(
                "SELECT status, error_category, retryable FROM d2a_http_push_log "
                "WHERE source = ? AND table_name = ? AND batch_id = ? "
                "AND status IN ('failed', 'retrying') ORDER BY id DESC LIMIT 1",
                (src, table, r["batch_id"])).fetchone()
            latest_row = self.con.execute(
                "SELECT status FROM d2a_http_push_log "
                "WHERE source = ? AND table_name = ? AND batch_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (src, table, r["batch_id"])).fetchone()
            if progress["completed"]:
                status = "completed"
            elif latest_row and latest_row["status"] == "failed":
                status = "failed"
            elif latest_row and latest_row["status"] == "retrying":
                status = "retrying"
            else:
                status = "pushing"
            summaries.append({
                "source": src,
                "table_name": table,
                "batch_id": r["batch_id"],
                "mode": meta_row["mode"] if meta_row else None,
                "generation_id": meta_row["generation_id"] if meta_row else None,
                "retry_count": meta_row["retry_count"] if meta_row else 0,
                "receipt_received": (
                    bool(meta_row["receipt_received"])
                    if meta_row and meta_row["receipt_received"] is not None
                    else None),
                "idempotent_replay": (
                    bool(meta_row["idempotent_replay"])
                    if meta_row and meta_row["idempotent_replay"] is not None
                    else None),
                "error_category": (
                    error_row["error_category"] if error_row else None),
                "retryable": (
                    bool(error_row["retryable"])
                    if error_row and error_row["retryable"] is not None
                    else None),
                "status": status,
                "completed": progress["completed"],
                "steps_ok": progress["ok"],
                "steps_failed": progress["failed"],
                "rows": progress["rows"],
                "write_ok_batches": progress["write_ok_batches"],
                "duration_ms": progress["duration_ms"],
                "last_at": r["last_at"],
            })
        summaries.sort(key=lambda s: s["last_at"], reverse=True)
        return summaries

    def push_log_batch_progress(
        self, source: str, table_name: str, batch_id: str,
    ) -> dict:
        """给定 batch 的推送进度摘要。

        ``complete_table`` 的 ``rows_count`` 是整表累计值，不能与每个
        ``write`` 的实际批次行数相加，否则页面会重复统计推送行数。
        """
        rows = self.con.execute(
            "SELECT step_kind, status, COUNT(*) AS cnt, "
            "COALESCE(SUM(rows_count), 0) AS total_rows, "
            "COALESCE(SUM(duration_ms), 0) AS total_ms "
            "FROM d2a_http_push_log "
            "WHERE source = ? AND table_name = ? AND batch_id = ? "
            "GROUP BY step_kind, status",
            (source, table_name, batch_id)).fetchall()
        result: dict = {
            "ok": 0, "failed": 0, "rows": 0, "duration_ms": 0.0,
            "write_ok_batches": 0, "completed": False,
        }
        for r in rows:
            if r["status"] == "ok":
                result["ok"] += r["cnt"]
            elif r["status"] == "failed":
                result["failed"] += r["cnt"]
            else:
                # retrying 是过程证据，不应让最终批次被误判失败。
                continue
            if r["step_kind"] == "write" and r["status"] == "ok":
                result["rows"] += r["total_rows"] or 0
                result["write_ok_batches"] += r["cnt"]
            if r["step_kind"] == "complete_table" and r["status"] == "ok":
                result["completed"] = True
            result["duration_ms"] += r["total_ms"] or 0
        result["total"] = result["ok"] + result["failed"]
        return result

    def start_run(self, source: str, run_type: str, *, commit: bool = True) -> int:
        if run_type not in self.RUN_TYPES:
            raise ValueError(f"未知 run_type '{run_type}',可用:{sorted(self.RUN_TYPES)}")
        cur = self.con.execute(
            "INSERT INTO d2a_sync_run (source, started_at, status, run_type, steps_recorded) "
            "VALUES (?, ?, 'running', ?, 1)",
            (source, _now(), run_type))
        if commit:
            self.con.commit()
        return cur.lastrowid

    def finish_run(
        self, run_id: int, *, tables: int, rows: int,
        status: str = "ok", detail: str = "", commit: bool = True,
    ) -> None:
        self.con.execute(
            "UPDATE d2a_sync_run SET finished_at = ?, tables = ?, rows = ?, "
            "status = ?, detail = ? WHERE id = ?",
            (_now(), tables, rows, status, detail, run_id))
        if commit:
            self.con.commit()

    def finish_running_run(
        self, run_id: int, *, status: str = "failed", detail: str = "",
        commit: bool = True,
    ) -> None:
        """仅在 run 仍为 running 时写入终态;已被 incremental_sync 改为
        ok/paused/failed 时不动,避免外层异常覆盖已记录的终态。"""
        self.con.execute(
            "UPDATE d2a_sync_run SET finished_at = ?, status = ?, detail = ? "
            "WHERE id = ? AND status = 'running'",
            (_now(), status, detail, run_id))
        if commit:
            self.con.commit()

    def recover_abandoned_runs(
        self, source: str, *, detail: str = "connector 重启，已回收上次中断的运行",
    ) -> int:
        """在已持有 source 跨进程锁时关闭崩溃遗留的 running 记录。"""
        run_ids = [
            int(row["id"])
            for row in self.con.execute(
                "SELECT id FROM d2a_sync_run WHERE source = ? "
                "AND status = 'running' AND run_type IN ('sync', 'reconcile')",
                (source,),
            ).fetchall()
        ]
        if not run_ids:
            return 0
        marks = ",".join("?" for _ in run_ids)
        now = _now()
        self.con.execute(
            f"UPDATE d2a_run_step SET status = 'aborted', finished_at = ?, "
            f"error = COALESCE(error, ?) WHERE run_id IN ({marks}) "
            "AND status = 'running'",
            (now, detail, *run_ids),
        )
        self.con.execute(
            f"UPDATE d2a_sync_run SET status = 'failed', finished_at = ?, "
            f"detail = ? WHERE id IN ({marks}) AND status = 'running'",
            (now, detail, *run_ids),
        )
        self.con.commit()
        return len(run_ids)

    # ---- run steps(M4)----

    def add_step(self, run_id: int, ordinal: int, kind: str, target: str, **fields) -> int:
        """新建 step(默认 running);kind∈table|object|segment|batch|dataset。"""
        if kind not in ("table", "object", "segment", "batch", "dataset"):
            raise ValueError(f"非法 step kind '{kind}'")
        cur = self.con.execute(
            "INSERT INTO d2a_run_step (run_id, ordinal, kind, target, status,"
            " started_at, finished_at, batch_id, rows_in, rows_out, quarantined,"
            " repaired, soft_deleted, watermark_before, watermark_after, error, error_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, ordinal, kind, target,
             fields.get("status", "running"),
             fields.get("started_at", _now()),
             fields.get("finished_at"),
             fields.get("batch_id"),
             fields.get("rows_in"),
             fields.get("rows_out"),
             fields.get("quarantined"),
             fields.get("repaired"),
             fields.get("soft_deleted"),
             fields.get("watermark_before"),
             fields.get("watermark_after"),
             fields.get("error"),
             fields.get("error_id")))
        self.con.commit()
        return cur.lastrowid

    def update_step(self, step_id: int, **fields) -> None:
        """按主键更新 step 度量/状态(结束时关闭 step)。

        进入终态(ok/paused/failed/aborted)且未显式给 finished_at 时自动补上。
        """
        allowed = {"status", "finished_at", "batch_id", "rows_in", "rows_out",
                   "quarantined", "repaired", "soft_deleted", "watermark_before",
                   "watermark_after", "error", "error_id", "batches", "progressed_at",
                   "expected_rows"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        if ("finished_at" not in fields
                and fields.get("status") in ("ok", "paused", "failed", "aborted")):
            fields["finished_at"] = _now()
        sql = "UPDATE d2a_run_step SET " + ", ".join(f"{k} = ?" for k in fields) + \
            " WHERE id = ?"
        self.con.execute(sql, list(fields.values()) + [step_id])
        self.con.commit()

    def steps_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT * FROM d2a_run_step WHERE run_id = ? ORDER BY ordinal, id",
            (run_id,)).fetchall()

    # ---- 告警静默(错误处理页)----

    def silence_alert(self, alert_key: str, *, hours: int = 24) -> str:
        """静默一条告警 hours 小时(重复调用顺延),返回截止时间。"""
        from datetime import timedelta
        until = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        self.con.execute(
            "INSERT INTO d2a_alert_silence (alert_key, silenced_until, created_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(alert_key) DO UPDATE SET silenced_until = excluded.silenced_until",
            (alert_key, until, _now()))
        self.con.commit()
        return until

    def list_alert_silences(self) -> list[dict]:
        """未过期的静默记录。"""
        rows = self.con.execute(
            "SELECT alert_key, silenced_until, created_at FROM d2a_alert_silence "
            "WHERE silenced_until > ? ORDER BY created_at DESC", (_now(),)).fetchall()
        return [dict(r) for r in rows]

    def delete_alert_silence(self, alert_key: str) -> bool:
        cur = self.con.execute(
            "DELETE FROM d2a_alert_silence WHERE alert_key = ?", (alert_key,))
        self.con.commit()
        return cur.rowcount > 0

    def record_sync_batch_progress(
        self, *, step_id: int, source: str, table: str,
        watermark_col: str | None = None, watermark: str | None = None,
        key_values: list | None = None, force_cursor: bool = False,
        rows_in: int = 0, rows_out: int = 0, batches: int = 0,
        batch_id: str | None = None, progressed_at: str | None = None,
        key_columns: list[str] | None = None, schema: str | None = None,
    ) -> None:
        """在同一事务内更新 step 进度与可恢复水位游标,然后提交一次。

        增量表同时推进游标;全量表(watermark_col=None)只更新 step。
        游标写入使用 commit=False 避免各自提交,最后由本方法统一 commit。
        """
        if watermark_col is not None:
            self.set_sync_cursor(
                source, table, watermark_col, watermark, key_values,
                batch_id, force=force_cursor, commit=False,
                key_columns=key_columns, schema=schema)
        self.con.execute(
            "UPDATE d2a_run_step SET rows_in = ?, rows_out = ?, batches = ?, "
            "progressed_at = ?, batch_id = COALESCE(?, batch_id) "
            "WHERE id = ?",
            (rows_in, rows_out, batches,
             progressed_at or _now(), batch_id, step_id))
        self.con.commit()
