"""原样落地层:raw_{source}__{table},按源主键 upsert,幂等。

E1 实现基于 SQLite(开发 / 展厅);生产 PostgreSQL 走同一 SQL 子集,后续切片补。
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
    tables INTEGER, rows INTEGER, status TEXT, detail TEXT
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


class LandingStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(db_path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_SYSTEM_DDL)

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

    def start_run(self, source: str) -> int:
        cur = self.con.execute(
            "INSERT INTO d2a_sync_run (source, started_at, status) VALUES (?, ?, 'running')",
            (source, _now()))
        self.con.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, tables: int, rows: int,
                   status: str = "ok", detail: str = "") -> None:
        self.con.execute(
            "UPDATE d2a_sync_run SET finished_at = ?, tables = ?, rows = ?, "
            "status = ?, detail = ? WHERE id = ?",
            (_now(), tables, rows, status, detail, run_id))
        self.con.commit()
