"""原样落地层:raw_{source}__{table},按源主键 upsert,幂等。

落地库为 SQLite(开发 / 展厅 / 首个工厂现场验证);单写者 + 多只读者,
初始化即开 WAL + busy_timeout。PostgreSQL 属后续切片(触发信号见设计 §4)。
系统表:d2a_audit_log(逐条源 SQL)、d2a_sync_run(逐轮汇总)。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .adapters.base import TableInfo
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord

_TYPE_SQL = {"int": "INTEGER", "real": "REAL", "text": "TEXT", "blob": "BLOB"}

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
    run_type TEXT, steps_recorded INTEGER
);
CREATE TABLE IF NOT EXISTS d2a_sync_state (
    source TEXT NOT NULL, table_name TEXT NOT NULL,
    watermark_col TEXT NOT NULL, high_water TEXT,
    last_run_at TEXT, last_batch_id TEXT,
    PRIMARY KEY (source, table_name)
);
CREATE TABLE IF NOT EXISTS d2a_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, object TEXT NOT NULL,
    keys_json TEXT, reason TEXT NOT NULL, raw_json TEXT,
    batch_id TEXT, created_at TEXT NOT NULL, resolved_at TEXT
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
CREATE TABLE IF NOT EXISTS d2a_console_access_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    subject TEXT NOT NULL,       -- 稳定主体标识(console-admin),不记 Token/指纹
    resource_type TEXT NOT NULL, -- raw | object | quarantine_raw
    source TEXT,
    resource TEXT NOT NULL,
    allowed INTEGER NOT NULL,    -- 1 允许 / 0 拒绝
    reason_code TEXT NOT NULL,
    page_offset INTEGER, page_limit INTEGER, returned_rows INTEGER,
    request_id TEXT
    -- 严禁:Token、Token 指纹、查询值原文、返回数据、完整 SQL、traceback
);
CREATE INDEX IF NOT EXISTS idx_d2a_access_ts ON d2a_console_access_audit (ts);
CREATE INDEX IF NOT EXISTS idx_d2a_access_type_allowed
    ON d2a_console_access_audit (resource_type, allowed);
CREATE TABLE IF NOT EXISTS d2a_dataset_version (
    dataset_version TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    template_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('building', 'published', 'failed', 'retired')),
    built_at TEXT NOT NULL,
    published_at TEXT,
    previous_dataset_version TEXT,
    error TEXT,
    object_manifest TEXT,
    CHECK (
        status NOT IN ('published', 'retired')
        OR (published_at IS NOT NULL AND trim(published_at) != '')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_d2a_dataset_one_published
    ON d2a_dataset_version (source) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_d2a_dataset_source_built
    ON d2a_dataset_version (source, built_at DESC);
CREATE TABLE IF NOT EXISTS d2a_object_version (
    dataset_version TEXT NOT NULL,
    object TEXT NOT NULL,
    object_version TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    batch_id TEXT,
    build_table TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('building', 'built', 'failed', 'published', 'retired')
    ),
    built_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (dataset_version, object),
    UNIQUE (object_version),
    FOREIGN KEY (dataset_version) REFERENCES d2a_dataset_version (dataset_version),
    CHECK (
        status NOT IN ('published', 'retired')
        OR (published_at IS NOT NULL AND trim(published_at) != '')
    )
);
CREATE INDEX IF NOT EXISTS idx_d2a_object_version_object
    ON d2a_object_version (object, built_at DESC);
"""

# 旧库 CREATE TABLE IF NOT EXISTS 不会补 CHECK;用触发器幂等强制状态联动。
_VERSION_PUBLISHED_AT_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_d2a_dataset_published_at_ins
BEFORE INSERT ON d2a_dataset_version
FOR EACH ROW
WHEN NEW.status IN ('published', 'retired')
 AND (NEW.published_at IS NULL OR trim(NEW.published_at) = '')
BEGIN
  SELECT RAISE(ABORT, 'published_at required when status is published or retired');
END;
CREATE TRIGGER IF NOT EXISTS trg_d2a_dataset_published_at_upd
BEFORE UPDATE ON d2a_dataset_version
FOR EACH ROW
WHEN NEW.status IN ('published', 'retired')
 AND (NEW.published_at IS NULL OR trim(NEW.published_at) = '')
BEGIN
  SELECT RAISE(ABORT, 'published_at required when status is published or retired');
END;
CREATE TRIGGER IF NOT EXISTS trg_d2a_object_published_at_ins
BEFORE INSERT ON d2a_object_version
FOR EACH ROW
WHEN NEW.status IN ('published', 'retired')
 AND (NEW.published_at IS NULL OR trim(NEW.published_at) = '')
BEGIN
  SELECT RAISE(ABORT, 'published_at required when status is published or retired');
END;
CREATE TRIGGER IF NOT EXISTS trg_d2a_object_published_at_upd
BEFORE UPDATE ON d2a_object_version
FOR EACH ROW
WHEN NEW.status IN ('published', 'retired')
 AND (NEW.published_at IS NULL OR trim(NEW.published_at) = '')
BEGIN
  SELECT RAISE(ABORT, 'published_at required when status is published or retired');
END;
"""


def raw_table_name(source: str, table: str) -> str:
    return f"raw_{source}__{table}"


def normalize_value(v):
    """源侧驱动返回的对象类型收敛为可移植类型(设计 §4:int/real/text/blob)。"""
    if isinstance(v, (datetime, date)):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    return v


def row_hash(row: dict) -> str:
    """行内容指纹(不含元数据列,基于归一化后的值),对账 L2 的比对依据。"""
    canonical = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dataset_version(row: sqlite3.Row) -> DatasetVersionRecord:
    keys = set(row.keys())
    return DatasetVersionRecord(
        dataset_version=row["dataset_version"],
        source=row["source"],
        template_version=row["template_version"],
        status=row["status"],
        built_at=row["built_at"],
        published_at=row["published_at"],
        previous_dataset_version=row["previous_dataset_version"],
        error=row["error"],
        object_manifest=row["object_manifest"] if "object_manifest" in keys else None,
    )


def _row_to_object_version(row: sqlite3.Row) -> ObjectVersionRecord:
    return ObjectVersionRecord(
        dataset_version=row["dataset_version"],
        object=row["object"],
        object_version=row["object_version"],
        binding_hash=row["binding_hash"],
        row_count=row["row_count"],
        batch_id=row["batch_id"],
        build_table=row["build_table"],
        status=row["status"],
        built_at=row["built_at"],
        published_at=row["published_at"],
    )


class LandingStore:
    RUN_TYPES = frozenset({"sync", "apply", "reconcile", "ingest", "validation"})

    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self.con = sqlite3.connect(db_path)
        self.con.row_factory = sqlite3.Row
        # 版本元数据要求 object_version 必须隶属于真实 dataset_version；
        # SQLite 默认不执行外键约束，因此每个连接显式开启。
        self.con.execute("PRAGMA foreign_keys=ON")
        # WAL:写批次不阻塞 MCP / 控制台的只读连接(单写者 + 多读者场景);
        # busy_timeout:偶发写锁竞争时等待而非立即抛 "database is locked"。
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.executescript(_SYSTEM_DDL)
        self._migrate()

    def _migrate(self) -> None:
        """幂等兼容升级(M3/M4):d2a_sync_run 增加结构化运行元数据。

        旧记录保持 NULL(类型/step 证据未知),不批量回填猜测;重复初始化无副作用。
        """
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(d2a_sync_run)")}
        if "run_type" not in cols:
            self.con.execute("ALTER TABLE d2a_sync_run ADD COLUMN run_type TEXT")
        if "steps_recorded" not in cols:
            self.con.execute("ALTER TABLE d2a_sync_run ADD COLUMN steps_recorded INTEGER")
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
        # v0.3:published/retired 必须带 published_at(旧库靠触发器补强制)。
        self.con.executescript(_VERSION_PUBLISHED_AT_TRIGGERS)
        # v0.3:数据集冻结对象清单(完整性分母);旧库缺列则补上。
        ds_cols = {r[1] for r in self.con.execute("PRAGMA table_info(d2a_dataset_version)")}
        if "object_manifest" not in ds_cols:
            self.con.execute(
                "ALTER TABLE d2a_dataset_version ADD COLUMN object_manifest TEXT")
        self.con.commit()

    # ---- raw 表 ----

    def ensure_raw_table(self, source: str, info: TableInfo) -> None:
        if not info.pk:
            raise ValueError(
                f"{info.name}: 源表无主键,无法幂等 upsert;"
                "请在 binding notes 标注并走全量替换策略(E2)")
        cols = ",\n".join(
            [f'    "{c}" {_TYPE_SQL[t]}' for c, t in info.columns]
            + [f'    "{c}" {t}' for c, t in _META_COLS])
        pk = ", ".join(f'"{k}"' for k in info.pk)
        self.con.execute(
            f'CREATE TABLE IF NOT EXISTS "{raw_table_name(source, info.name)}" '
            f"(\n{cols},\n    PRIMARY KEY ({pk})\n)")
        self.con.commit()

    def upsert_rows(self, source: str, info: TableInfo, rows: list[dict], batch_id: str) -> int:
        if not rows:
            return 0
        table = raw_table_name(source, info.name)
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
        pk_sql = ", ".join(f'"{k}"' for k in info.pk)
        update_sql = ", ".join(
            f'"{c}" = excluded."{c}"' for c in all_cols) + ', "_d2a_deleted_at" = NULL'
        self.con.executemany(
            f'INSERT INTO "{table}" ({col_sql}) VALUES ({val_sql}) '
            f"ON CONFLICT ({pk_sql}) DO UPDATE SET {update_sql}",
            payload)
        self.con.commit()
        return len(rows)

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

    def active_pks(self, source: str, table: str, pk_col: str,
                   wm_col: str | None = None, start=None, end=None) -> set:
        sql = f'SELECT "{pk_col}" FROM "{raw_table_name(source, table)}" WHERE _d2a_deleted_at IS NULL'
        params: tuple = ()
        if wm_col is not None:
            sql += f' AND "{wm_col}" >= ? AND "{wm_col}" < ?'
            params = (start, end)
        return {r[0] for r in self.con.execute(sql, params)}

    def mark_deleted(self, source: str, table: str, pk_col: str, pks: set) -> int:
        """软删打标:源侧消失的行,永不物理删。"""
        if not pks:
            return 0
        now, table_sql = _now(), raw_table_name(source, table)
        pk_list = sorted(pks)
        for i in range(0, len(pk_list), 500):  # SQLite 参数上限内分块
            chunk = pk_list[i:i + 500]
            self.con.execute(
                f'UPDATE "{table_sql}" SET _d2a_deleted_at = ? '
                f'WHERE "{pk_col}" IN ({", ".join("?" * len(chunk))})',
                (now, *chunk))
        self.con.commit()
        return len(pks)

    def min_watermark(self, source: str, table: str, wm_col: str) -> str | None:
        (m,) = self.con.execute(
            f'SELECT MIN("{wm_col}") FROM "{raw_table_name(source, table)}"').fetchone()
        return m

    # ---- 水位状态 ----

    def get_high_water(self, source: str, table: str) -> str | None:
        row = self.con.execute(
            "SELECT high_water FROM d2a_sync_state WHERE source = ? AND table_name = ?",
            (source, table)).fetchone()
        return row["high_water"] if row else None

    def set_high_water(self, source: str, table: str, watermark_col: str,
                       high_water: str | None, batch_id: str) -> None:
        """水位只前进不后退(字符串比较,ISO 时间格式下与时间序一致)。"""
        old = self.get_high_water(source, table)
        if high_water is None or (old is not None and high_water < old):
            high_water = old
        self.con.execute(
            "INSERT INTO d2a_sync_state "
            "(source, table_name, watermark_col, high_water, last_run_at, last_batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, table_name) DO UPDATE SET "
            "watermark_col = excluded.watermark_col, high_water = excluded.high_water, "
            "last_run_at = excluded.last_run_at, last_batch_id = excluded.last_batch_id",
            (source, table, watermark_col, high_water, _now(), batch_id))
        self.con.commit()

    # ---- 隔离区(E4)----

    def quarantine_supersede(self, source: str, object_name: str) -> None:
        """新一轮映射前,把该对象上一轮未处理的隔离记录标记为已被取代(历史保留)。"""
        self.con.execute(
            "UPDATE d2a_quarantine SET resolved_at = ? "
            "WHERE source = ? AND object = ? AND resolved_at IS NULL",
            (_now(), source, object_name))
        self.con.commit()

    def quarantine_add(self, source: str, object_name: str, records: list[dict],
                       batch_id: str) -> None:
        if not records:
            return
        now = _now()
        self.con.executemany(
            "INSERT INTO d2a_quarantine "
            "(source, object, keys_json, reason, raw_json, batch_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(source, object_name,
              json.dumps(r["keys"], ensure_ascii=False, default=str),
              r["reason"],
              json.dumps(r["raw"], ensure_ascii=False, default=str),
              batch_id, now)
             for r in records])
        self.con.commit()

    def quarantine_count(self, source: str, object_name: str) -> int:
        (n,) = self.con.execute(
            "SELECT COUNT(*) FROM d2a_quarantine "
            "WHERE source = ? AND object = ? AND resolved_at IS NULL",
            (source, object_name)).fetchone()
        return n

    # ---- 审计与运行汇总 ----

    def log_audit(self, source: str, action: str, sql: str, rows: int,
                  duration_ms: float, batch_id: str | None = None) -> None:
        self.con.execute(
            "INSERT INTO d2a_audit_log (ts, source, action, sql, rows, duration_ms, batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), source, action, sql, rows, round(duration_ms, 2), batch_id))
        self.con.commit()

    def start_run(self, source: str, run_type: str) -> int:
        if run_type not in self.RUN_TYPES:
            raise ValueError(f"未知 run_type '{run_type}',可用:{sorted(self.RUN_TYPES)}")
        cur = self.con.execute(
            "INSERT INTO d2a_sync_run (source, started_at, status, run_type, steps_recorded) "
            "VALUES (?, ?, 'running', ?, 1)",
            (source, _now(), run_type))
        self.con.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, tables: int, rows: int,
                   status: str = "ok", detail: str = "") -> None:
        self.con.execute(
            "UPDATE d2a_sync_run SET finished_at = ?, tables = ?, rows = ?, "
            "status = ?, detail = ? WHERE id = ?",
            (_now(), tables, rows, status, detail, run_id))
        self.con.commit()

    # ---- run steps(M4)----

    def add_step(self, run_id: int, ordinal: int, kind: str, target: str, **fields) -> int:
        """新建 step(默认 running);kind∈table|object|segment|batch。"""
        if kind not in ("table", "object", "segment", "batch"):
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
                   "watermark_after", "error", "error_id"}
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

    # ---- 控制台访问审计(M4)----

    def log_access(self, *, subject: str, resource_type: str, source: str | None,
                   resource: str, allowed: bool, reason_code: str,
                   page_offset: int | None = None, page_limit: int | None = None,
                   returned_rows: int | None = None,
                   request_id: str | None = None) -> int:
        """记录控制台数据访问(允许/拒绝)。

        只记主体/目标/结果/查询形状/行数;严禁 Token、q 原文、返回值、traceback。
        """
        if resource_type not in ("raw", "object", "quarantine_raw"):
            raise ValueError(f"非法 resource_type '{resource_type}'")
        cur = self.con.execute(
            "INSERT INTO d2a_console_access_audit (ts, subject, resource_type, source,"
            " resource, allowed, reason_code, page_offset, page_limit, returned_rows,"
            " request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), subject, resource_type, source, resource,
             1 if allowed else 0, reason_code,
             page_offset, page_limit, returned_rows, request_id))
        self.con.commit()
        return cur.lastrowid

    # ---- 数据集/对象版本只读查询(v0.3 M1)----

    def list_dataset_versions(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list["DatasetVersionRecord"], int]:
        """按 built_at DESC, dataset_version DESC 列出版本；空表返回 ([], 0)。"""
        where: list[str] = []
        params: list[object] = []
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        (total,) = self.con.execute(
            f"SELECT COUNT(*) FROM d2a_dataset_version{wsql}", params
        ).fetchone()
        rows = self.con.execute(
            f"SELECT * FROM d2a_dataset_version{wsql} "
            "ORDER BY built_at DESC, dataset_version DESC "
            "LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_dataset_version(r) for r in rows], total

    def get_dataset_version(self, version: str) -> "DatasetVersionRecord | None":
        row = self.con.execute(
            "SELECT * FROM d2a_dataset_version WHERE dataset_version = ?",
            (version,),
        ).fetchone()
        return _row_to_dataset_version(row) if row else None

    def get_published_dataset(self, source: str) -> "DatasetVersionRecord | None":
        row = self.con.execute(
            "SELECT * FROM d2a_dataset_version "
            "WHERE source = ? AND status = 'published'",
            (source,),
        ).fetchone()
        return _row_to_dataset_version(row) if row else None

    def list_object_versions(
        self, dataset_version: str
    ) -> list["ObjectVersionRecord"]:
        rows = self.con.execute(
            "SELECT * FROM d2a_object_version WHERE dataset_version = ? "
            "ORDER BY built_at DESC, object DESC",
            (dataset_version,),
        ).fetchall()
        return [_row_to_object_version(r) for r in rows]
