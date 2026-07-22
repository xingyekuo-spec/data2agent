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
    run_type TEXT, steps_recorded INTEGER,
    dataset_version TEXT
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
    template_snapshot TEXT,
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
    purged_at TEXT,
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
CREATE TABLE IF NOT EXISTS d2a_field_lineage (
    dataset_version TEXT NOT NULL,
    object_version TEXT NOT NULL,
    object TEXT NOT NULL,
    object_key_json TEXT NOT NULL,
    object_key_hash TEXT NOT NULL,
    property TEXT NOT NULL,
    result_value_json TEXT NOT NULL,
    trace_status TEXT NOT NULL CHECK (trace_status IN ('available', 'unavailable')),
    unavailable_reason TEXT,
    transform_kind TEXT NOT NULL CHECK (
        transform_kind IN ('direct', 'derived', 'unmapped')
    ),
    transform_steps_json TEXT NOT NULL,
    source TEXT NOT NULL,
    map_batch_id TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    binding_status TEXT NOT NULL,
    template_version TEXT NOT NULL,
    PRIMARY KEY (dataset_version, object, object_key_json, property),
    UNIQUE (dataset_version, object, object_key_hash, property),
    FOREIGN KEY (dataset_version, object)
        REFERENCES d2a_object_version (dataset_version, object)
);
CREATE INDEX IF NOT EXISTS idx_d2a_field_lineage_key_hash
    ON d2a_field_lineage (dataset_version, object, object_key_hash);
CREATE TABLE IF NOT EXISTS d2a_field_lineage_input (
    dataset_version TEXT NOT NULL,
    object TEXT NOT NULL,
    object_key_json TEXT NOT NULL,
    property TEXT NOT NULL,
    input_ordinal INTEGER NOT NULL CHECK (input_ordinal >= 0),
    role TEXT NOT NULL CHECK (role IN ('value', 'join_fk', 'derived_condition')),
    source TEXT,
    source_table TEXT,
    source_pk_json TEXT,
    source_column TEXT,
    source_value_json TEXT,
    extract_batch_id TEXT,
    join_json TEXT,
    PRIMARY KEY (dataset_version, object, object_key_json, property, input_ordinal),
    FOREIGN KEY (dataset_version, object, object_key_json, property)
        REFERENCES d2a_field_lineage (
            dataset_version, object, object_key_json, property
        )
);
CREATE INDEX IF NOT EXISTS idx_d2a_field_lineage_input_obj
    ON d2a_field_lineage_input (dataset_version, object);
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

# 冻结字段创建后不可改写;GC 仅允许已 retired 对象 build_table→NULL + purged_at。
# status/published_at/previous_dataset_version/error 可变(发布/回滚激活路径)。
# current/previous 保护由 T06 GC 逻辑强制,不在触发器层猜测。
_VERSION_FREEZE_TRIGGERS = """
DROP TRIGGER IF EXISTS trg_d2a_dataset_freeze_upd;
DROP TRIGGER IF EXISTS trg_d2a_object_freeze_upd;
CREATE TRIGGER IF NOT EXISTS trg_d2a_dataset_freeze_upd
BEFORE UPDATE ON d2a_dataset_version
FOR EACH ROW
WHEN NEW.source IS NOT OLD.source
  OR NEW.template_version IS NOT OLD.template_version
  OR IFNULL(NEW.object_manifest, '') IS NOT IFNULL(OLD.object_manifest, '')
  OR IFNULL(NEW.template_snapshot, '') IS NOT IFNULL(OLD.template_snapshot, '')
  OR NEW.built_at IS NOT OLD.built_at
BEGIN
  SELECT RAISE(ABORT, 'frozen dataset fields are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_d2a_object_freeze_upd
BEFORE UPDATE ON d2a_object_version
FOR EACH ROW
WHEN NEW.dataset_version IS NOT OLD.dataset_version
  OR NEW.object IS NOT OLD.object
  OR NEW.object_version IS NOT OLD.object_version
  OR NEW.binding_hash IS NOT OLD.binding_hash
  OR NEW.built_at IS NOT OLD.built_at
  OR (
    NEW.row_count IS NOT OLD.row_count
    AND OLD.status != 'building'
  )
  OR (
    IFNULL(NEW.build_table, '') IS NOT IFNULL(OLD.build_table, '')
    AND OLD.status != 'building'
    AND NOT (
      OLD.status = 'retired'
      AND OLD.build_table IS NOT NULL
      AND NEW.build_table IS NULL
      AND NEW.purged_at IS NOT NULL
      AND trim(NEW.purged_at) != ''
      AND NEW.status = 'retired'
    )
  )
  OR (
    IFNULL(NEW.purged_at, '') IS NOT IFNULL(OLD.purged_at, '')
    AND NOT (
      OLD.status = 'retired'
      AND OLD.purged_at IS NULL
      AND NEW.purged_at IS NOT NULL
      AND trim(NEW.purged_at) != ''
      AND NEW.build_table IS NULL
      AND NEW.status = 'retired'
    )
  )
  OR (
    IFNULL(NEW.lineage_schema_version, -1)
      IS NOT IFNULL(OLD.lineage_schema_version, -1)
    AND OLD.status != 'building'
  )
  OR (
    IFNULL(NEW.lineage_field_count, -1)
      IS NOT IFNULL(OLD.lineage_field_count, -1)
    AND OLD.status != 'building'
  )
BEGIN
  SELECT RAISE(ABORT, 'frozen object fields are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_d2a_field_lineage_no_update
BEFORE UPDATE ON d2a_field_lineage
BEGIN
  SELECT RAISE(ABORT, 'field lineage is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_d2a_field_lineage_input_no_update
BEFORE UPDATE ON d2a_field_lineage_input
BEGIN
  SELECT RAISE(ABORT, 'field lineage input is immutable');
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
        template_snapshot=(
            row["template_snapshot"] if "template_snapshot" in keys else None
        ),
    )


def _row_to_object_version(row: sqlite3.Row) -> ObjectVersionRecord:
    keys = set(row.keys())
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
        purged_at=row["purged_at"] if "purged_at" in keys else None,
        lineage_schema_version=(
            row["lineage_schema_version"]
            if "lineage_schema_version" in keys else None
        ),
        lineage_field_count=(
            row["lineage_field_count"] if "lineage_field_count" in keys else None
        ),
    )


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
        # 版本元数据要求 object_version 必须隶属于真实 dataset_version；
        # SQLite 默认不执行外键约束，因此每个连接显式开启。
        self.con.execute("PRAGMA foreign_keys=ON")
        # WAL:写批次不阻塞 MCP / 控制台的只读连接(单写者 + 多读者场景);
        # busy_timeout:偶发写锁竞争时等待而非立即抛 "database is locked"。
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.executescript(_SYSTEM_DDL)
        self._migrate()

    @classmethod
    def open_readonly(cls, db_path: str | Path) -> "LandingStore":
        """打开已有落地库的只读连接(无 DDL/migrate);供 MCP/Console 快照读取。"""
        inst = cls.__new__(cls)
        inst.db_path = str(db_path)
        inst.con = sqlite3.connect(f"file:{inst.db_path}?mode=ro", uri=True)
        inst.con.row_factory = sqlite3.Row
        inst.con.execute("PRAGMA foreign_keys=ON")
        inst.con.execute("PRAGMA busy_timeout=5000")
        return inst

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
        self.con.executescript(_VERSION_FREEZE_TRIGGERS)
        # v0.3:数据集冻结对象清单与模板快照;旧库缺列则补上。
        ds_cols = {r[1] for r in self.con.execute("PRAGMA table_info(d2a_dataset_version)")}
        if "object_manifest" not in ds_cols:
            self.con.execute(
                "ALTER TABLE d2a_dataset_version ADD COLUMN object_manifest TEXT")
        if "template_snapshot" not in ds_cols:
            self.con.execute(
                "ALTER TABLE d2a_dataset_version ADD COLUMN template_snapshot TEXT")
        obj_cols = {r[1] for r in self.con.execute("PRAGMA table_info(d2a_object_version)")}
        if "purged_at" not in obj_cols:
            self.con.execute(
                "ALTER TABLE d2a_object_version ADD COLUMN purged_at TEXT")
        if "lineage_schema_version" not in obj_cols:
            self.con.execute(
                "ALTER TABLE d2a_object_version "
                "ADD COLUMN lineage_schema_version INTEGER")
        if "lineage_field_count" not in obj_cols:
            self.con.execute(
                "ALTER TABLE d2a_object_version "
                "ADD COLUMN lineage_field_count INTEGER")
        # 血缘表在 _SYSTEM_DDL 中 CREATE IF NOT EXISTS;旧库同样幂等建表。
        self.con.executescript(
            """
            CREATE TABLE IF NOT EXISTS d2a_field_lineage (
                dataset_version TEXT NOT NULL,
                object_version TEXT NOT NULL,
                object TEXT NOT NULL,
                object_key_json TEXT NOT NULL,
                object_key_hash TEXT NOT NULL,
                property TEXT NOT NULL,
                result_value_json TEXT NOT NULL,
                trace_status TEXT NOT NULL
                    CHECK (trace_status IN ('available', 'unavailable')),
                unavailable_reason TEXT,
                transform_kind TEXT NOT NULL CHECK (
                    transform_kind IN ('direct', 'derived', 'unmapped')
                ),
                transform_steps_json TEXT NOT NULL,
                source TEXT NOT NULL,
                map_batch_id TEXT NOT NULL,
                binding_hash TEXT NOT NULL,
                binding_status TEXT NOT NULL,
                template_version TEXT NOT NULL,
                PRIMARY KEY (dataset_version, object, object_key_json, property),
                UNIQUE (dataset_version, object, object_key_hash, property),
                FOREIGN KEY (dataset_version, object)
                    REFERENCES d2a_object_version (dataset_version, object)
            );
            CREATE INDEX IF NOT EXISTS idx_d2a_field_lineage_key_hash
                ON d2a_field_lineage (dataset_version, object, object_key_hash);
            CREATE TABLE IF NOT EXISTS d2a_field_lineage_input (
                dataset_version TEXT NOT NULL,
                object TEXT NOT NULL,
                object_key_json TEXT NOT NULL,
                property TEXT NOT NULL,
                input_ordinal INTEGER NOT NULL CHECK (input_ordinal >= 0),
                role TEXT NOT NULL CHECK (
                    role IN ('value', 'join_fk', 'derived_condition')
                ),
                source TEXT,
                source_table TEXT,
                source_pk_json TEXT,
                source_column TEXT,
                source_value_json TEXT,
                extract_batch_id TEXT,
                join_json TEXT,
                PRIMARY KEY (
                    dataset_version, object, object_key_json, property, input_ordinal
                ),
                FOREIGN KEY (dataset_version, object, object_key_json, property)
                    REFERENCES d2a_field_lineage (
                        dataset_version, object, object_key_json, property
                    )
            );
            CREATE INDEX IF NOT EXISTS idx_d2a_field_lineage_input_obj
                ON d2a_field_lineage_input (dataset_version, object);
            """
        )
        try:
            self.con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_d2a_dataset_one_building "
                "ON d2a_dataset_version (source) WHERE status = 'building'")
        except sqlite3.IntegrityError:
            # 脏旧库可能已有多 building;不阻断启动,由后续构建恢复关闭陈旧候选。
            self.con.execute(
                "CREATE INDEX IF NOT EXISTS idx_d2a_dataset_building_lookup "
                "ON d2a_dataset_version (source) WHERE status = 'building'")
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

    def quarantine_supersede_except(
        self,
        source: str,
        object_name: str,
        keep_batch_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        """发布成功后取代旧隔离;保留本轮 build 写入的 batch 记录。"""
        keep = {b for b in (keep_batch_ids or set()) if b}
        if not keep:
            self.quarantine_supersede(source, object_name)
            return
        placeholders = ",".join("?" * len(keep))
        self.con.execute(
            f"UPDATE d2a_quarantine SET resolved_at = ? "
            f"WHERE source = ? AND object = ? AND resolved_at IS NULL "
            f"AND (batch_id IS NULL OR batch_id NOT IN ({placeholders}))",
            (_now(), source, object_name, *keep),
        )
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

    # ---- 数据集/对象版本 CRUD(v0.3 M2)----

    def insert_dataset_version(
        self, record: DatasetVersionRecord, *, commit: bool = True,
    ) -> None:
        """插入数据集版本行;冻结字段此后仅能通过生命周期/激活路径变更。

        commit=False 供构建预占与 Run 绑定同一短事务。
        """
        self.con.execute(
            "INSERT INTO d2a_dataset_version ("
            "dataset_version, source, template_version, status, built_at, "
            "published_at, previous_dataset_version, error, object_manifest, "
            "template_snapshot"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.dataset_version,
                record.source,
                record.template_version,
                record.status,
                record.built_at,
                record.published_at,
                record.previous_dataset_version,
                record.error,
                record.object_manifest,
                record.template_snapshot,
            ),
        )
        if commit:
            self.con.commit()

    def update_dataset_lifecycle(
        self,
        dataset_version: str,
        *,
        status: str,
        published_at: str | None = None,
        previous_dataset_version: str | None = None,
        error: str | None = None,
        clear_error: bool = False,
        commit: bool = True,
    ) -> None:
        """仅更新状态机字段;不得改写冻结的模板/清单/built_at。

        commit=False 供发布/回滚临界事务批量提交。
        """
        sets = ["status = ?"]
        params: list[object] = [status]
        if published_at is not None:
            sets.append("published_at = ?")
            params.append(published_at)
        if previous_dataset_version is not None:
            sets.append("previous_dataset_version = ?")
            params.append(previous_dataset_version)
        if clear_error:
            sets.append("error = NULL")
        elif error is not None:
            sets.append("error = ?")
            params.append(error)
        params.append(dataset_version)
        cur = self.con.execute(
            f"UPDATE d2a_dataset_version SET {', '.join(sets)} "
            "WHERE dataset_version = ?",
            params,
        )
        if cur.rowcount != 1:
            raise ValueError(f"数据集版本 {dataset_version} 不存在")
        if commit:
            self.con.commit()

    def insert_object_version(self, record: ObjectVersionRecord) -> None:
        self.con.execute(
            "INSERT INTO d2a_object_version ("
            "dataset_version, object, object_version, binding_hash, row_count, "
            "batch_id, build_table, status, built_at, published_at, purged_at, "
            "lineage_schema_version, lineage_field_count"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.dataset_version,
                record.object,
                record.object_version,
                record.binding_hash,
                record.row_count,
                record.batch_id,
                record.build_table,
                record.status,
                record.built_at,
                record.published_at,
                record.purged_at,
                record.lineage_schema_version,
                record.lineage_field_count,
            ),
        )
        self.con.commit()

    def update_object_lifecycle(
        self,
        dataset_version: str,
        object_name: str,
        *,
        status: str,
        published_at: str | None = None,
        batch_id: str | None = None,
        commit: bool = True,
    ) -> None:
        """更新对象状态/发布时间/批次;不得改写 binding/row_count/build_table。"""
        sets = ["status = ?"]
        params: list[object] = [status]
        if published_at is not None:
            sets.append("published_at = ?")
            params.append(published_at)
        if batch_id is not None:
            sets.append("batch_id = ?")
            params.append(batch_id)
        params.extend([dataset_version, object_name])
        cur = self.con.execute(
            f"UPDATE d2a_object_version SET {', '.join(sets)} "
            "WHERE dataset_version = ? AND object = ?",
            params,
        )
        if cur.rowcount != 1:
            raise ValueError(
                f"对象版本 {dataset_version}/{object_name} 不存在")
        if commit:
            self.con.commit()

    def update_object_build_result(
        self,
        dataset_version: str,
        object_name: str,
        *,
        status: str,
        row_count: int,
        build_table: str | None,
        batch_id: str | None = None,
        commit: bool = True,
    ) -> None:
        """构建完成写回:仅 building 行可更新 row_count/build_table 并转入 built/failed。"""
        if status not in ("built", "failed"):
            raise ValueError("构建结果状态必须是 built 或 failed")
        if row_count < 0:
            raise ValueError("row_count 不得为负")
        sets = [
            "status = ?",
            "row_count = ?",
            "build_table = ?",
        ]
        params: list[object] = [status, row_count, build_table]
        if batch_id is not None:
            sets.append("batch_id = ?")
            params.append(batch_id)
        params.extend([dataset_version, object_name])
        cur = self.con.execute(
            f"UPDATE d2a_object_version SET {', '.join(sets)} "
            "WHERE dataset_version = ? AND object = ? AND status = 'building'",
            params,
        )
        if cur.rowcount != 1:
            raise ValueError(
                f"无法写回构建结果 {dataset_version}/{object_name}"
            )
        if commit:
            self.con.commit()

    def purge_object_build_table(
        self,
        dataset_version: str,
        object_name: str,
        *,
        purged_at: str,
        commit: bool = True,
    ) -> None:
        """GC tombstone:仅 retired 对象可将 build_table 置空并写入 purged_at。"""
        cur = self.con.execute(
            "UPDATE d2a_object_version "
            "SET build_table = NULL, purged_at = ? "
            "WHERE dataset_version = ? AND object = ? AND status = 'retired' "
            "AND build_table IS NOT NULL AND purged_at IS NULL",
            (purged_at, dataset_version, object_name),
        )
        if cur.rowcount != 1:
            raise ValueError(
                f"无法清理对象物理表 {dataset_version}/{object_name}"
            )
        if commit:
            self.con.commit()

    def set_run_dataset_version(
        self, run_id: int, dataset_version: str, *, commit: bool = True,
    ) -> None:
        """将 Run 绑定到真实数据集版本;未知版本 fail-closed。"""
        if self.get_dataset_version(dataset_version) is None:
            raise ValueError(f"数据集版本 {dataset_version} 不存在")
        cur = self.con.execute(
            "UPDATE d2a_sync_run SET dataset_version = ? WHERE id = ?",
            (dataset_version, run_id),
        )
        if cur.rowcount != 1:
            raise ValueError(f"运行 {run_id} 不存在")
        if commit:
            self.con.commit()

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

    # ---- field lineage (M4) -------------------------------------------------

    def update_object_lineage_meta(
        self,
        dataset_version: str,
        object_name: str,
        *,
        lineage_schema_version: int,
        lineage_field_count: int,
        commit: bool = True,
    ) -> None:
        """building 阶段写入血缘完整性元数据;离开 building 后由 freeze 触发器冻结。"""
        if lineage_field_count < 0:
            raise ValueError("lineage_field_count 不得为负")
        cur = self.con.execute(
            "UPDATE d2a_object_version "
            "SET lineage_schema_version = ?, lineage_field_count = ? "
            "WHERE dataset_version = ? AND object = ? AND status = 'building'",
            (
                lineage_schema_version,
                lineage_field_count,
                dataset_version,
                object_name,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError(
                f"无法写入血缘元数据 {dataset_version}/{object_name}"
            )
        if commit:
            self.con.commit()

    def insert_field_lineage(
        self,
        nodes: Sequence,
        inputs: Sequence,
        *,
        commit: bool = True,
    ) -> None:
        """批量写入字段节点与输入边;失败时由调用方事务回滚。

        hash 冲突(UNIQUE dataset/object/hash/property)必须让构建失败。
        """
        from .field_lineage import FieldLineageInputRow, FieldLineageNode

        node_rows = []
        for node in nodes:
            if not isinstance(node, FieldLineageNode):
                raise TypeError("nodes 须为 FieldLineageNode")
            node_rows.append((
                node.dataset_version,
                node.object_version,
                node.object,
                node.object_key_json,
                node.object_key_hash,
                node.property,
                node.result_value_json,
                node.trace_status,
                node.unavailable_reason,
                node.transform_kind,
                node.transform_steps_json,
                node.source,
                node.map_batch_id,
                node.binding_hash,
                node.binding_status,
                node.template_version,
            ))
        input_rows = []
        for item in inputs:
            if not isinstance(item, FieldLineageInputRow):
                raise TypeError("inputs 须为 FieldLineageInputRow")
            input_rows.append((
                item.dataset_version,
                item.object,
                item.object_key_json,
                item.property,
                item.input_ordinal,
                item.role,
                item.source,
                item.source_table,
                item.source_pk_json,
                item.source_column,
                item.source_value_json,
                item.extract_batch_id,
                item.join_json,
            ))
        if node_rows:
            self.con.executemany(
                "INSERT INTO d2a_field_lineage ("
                "dataset_version, object_version, object, object_key_json, "
                "object_key_hash, property, result_value_json, trace_status, "
                "unavailable_reason, transform_kind, transform_steps_json, "
                "source, map_batch_id, binding_hash, binding_status, "
                "template_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                node_rows,
            )
        if input_rows:
            self.con.executemany(
                "INSERT INTO d2a_field_lineage_input ("
                "dataset_version, object, object_key_json, property, "
                "input_ordinal, role, source, source_table, source_pk_json, "
                "source_column, source_value_json, extract_batch_id, join_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                input_rows,
            )
        if commit:
            self.con.commit()

    def delete_field_lineage(
        self,
        dataset_version: str,
        object_name: str | None = None,
        *,
        commit: bool = True,
    ) -> int:
        """受控清理血缘(失败恢复/GC);先删 input 再删 node。返回删除的节点数。"""
        if object_name is None:
            self.con.execute(
                "DELETE FROM d2a_field_lineage_input WHERE dataset_version = ?",
                (dataset_version,),
            )
            cur = self.con.execute(
                "DELETE FROM d2a_field_lineage WHERE dataset_version = ?",
                (dataset_version,),
            )
        else:
            self.con.execute(
                "DELETE FROM d2a_field_lineage_input "
                "WHERE dataset_version = ? AND object = ?",
                (dataset_version, object_name),
            )
            cur = self.con.execute(
                "DELETE FROM d2a_field_lineage "
                "WHERE dataset_version = ? AND object = ?",
                (dataset_version, object_name),
            )
        if commit:
            self.con.commit()
        return cur.rowcount

    def count_field_lineage(
        self, dataset_version: str, object_name: str,
    ) -> int:
        (n,) = self.con.execute(
            "SELECT COUNT(*) FROM d2a_field_lineage "
            "WHERE dataset_version = ? AND object = ?",
            (dataset_version, object_name),
        ).fetchone()
        return n

    def get_field_lineage_by_key_hash(
        self,
        dataset_version: str,
        object_name: str,
        object_key_hash: str,
        *,
        property_name: str | None = None,
    ) -> list[sqlite3.Row]:
        """按 key token 查询字段节点;可选单属性过滤。"""
        if property_name is None:
            return self.con.execute(
                "SELECT * FROM d2a_field_lineage "
                "WHERE dataset_version = ? AND object = ? AND object_key_hash = ? "
                "ORDER BY property",
                (dataset_version, object_name, object_key_hash),
            ).fetchall()
        return self.con.execute(
            "SELECT * FROM d2a_field_lineage "
            "WHERE dataset_version = ? AND object = ? AND object_key_hash = ? "
            "AND property = ?",
            (dataset_version, object_name, object_key_hash, property_name),
        ).fetchall()

    def get_field_lineage_inputs_by_key_hash(
        self,
        dataset_version: str,
        object_name: str,
        object_key_hash: str,
        *,
        property_name: str | None = None,
    ) -> list[sqlite3.Row]:
        """按 key token 查询输入边;按 property, input_ordinal 稳定排序。"""
        base = (
            "SELECT i.* FROM d2a_field_lineage_input i "
            "JOIN d2a_field_lineage n "
            "ON i.dataset_version = n.dataset_version "
            "AND i.object = n.object "
            "AND i.object_key_json = n.object_key_json "
            "AND i.property = n.property "
            "WHERE n.dataset_version = ? AND n.object = ? "
            "AND n.object_key_hash = ?"
        )
        params: list = [dataset_version, object_name, object_key_hash]
        if property_name is not None:
            base += " AND n.property = ?"
            params.append(property_name)
        base += " ORDER BY i.property, i.input_ordinal"
        return self.con.execute(base, params).fetchall()
