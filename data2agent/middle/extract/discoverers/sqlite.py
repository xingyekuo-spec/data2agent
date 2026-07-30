"""SQLite 元数据发现(开发 / 测试 / 协议替身)。"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone

from ....shared.config import SourceConfig
from ..metadata import (
    ColumnMeta,
    ForeignKeyMeta,
    KeyCheckResult,
    KeyMeta,
    MetadataError,
    TableDetail,
    TableSummary,
    WatermarkCheckResult,
    schema_fingerprint,
    suggest_watermark_candidates,
    validate_identifier,
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def create_sqlite_discoverer(scfg: SourceConfig) -> "SqliteMetadataDiscoverer":
    path = scfg.path or os.environ.get(scfg.dsn_env or "", "")
    if not path:
        raise MetadataError("missing_path", "sqlite 源缺少 path / dsn_env")
    return SqliteMetadataDiscoverer(path)


class SqliteMetadataDiscoverer:
    def __init__(self, db_path: str):
        self._con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._con.row_factory = sqlite3.Row

    def close(self) -> None:
        self._con.close()

    def default_schema(self) -> str:
        return "main"

    def list_schemas(self) -> list[str]:
        return ["main"]

    def list_tables(
        self,
        *,
        schema: str | None = None,
        q: str | None = None,
        object_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[TableSummary], int]:
        schema = schema or "main"
        if schema != "main":
            return [], 0
        rows = self._con.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        summaries: list[TableSummary] = []
        needle = (q or "").casefold()
        for row in rows:
            name = row["name"]
            otype = "view" if row["type"] == "view" else "table"
            if object_type and otype != object_type:
                continue
            if needle and needle not in name.casefold():
                continue
            # 目录项不在此拉详情;失败由表扫描任务计入 table_errors。
            summaries.append(TableSummary(
                schema="main",
                name=name,
                object_type=otype,
                estimated_rows=None,
                primary_key=(),
                unique_keys=(),
                watermark_candidates=(),
            ))
        total = len(summaries)
        return summaries[offset:offset + limit], total

    def get_table(self, schema: str, table: str) -> TableDetail:
        validate_identifier(table, kind="表名")
        if schema not in (None, "", "main"):
            raise MetadataError("table_missing", f"schema '{schema}' 不存在")
        master = self._con.execute(
            "SELECT type FROM sqlite_master WHERE name = ? AND type IN ('table','view')",
            (table,),
        ).fetchone()
        if master is None:
            raise MetadataError("table_missing", f"表 '{table}' 不存在")
        otype = "view" if master["type"] == "view" else "table"
        cols_raw = self._con.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not cols_raw:
            raise MetadataError("table_missing", f"表 '{table}' 不存在")
        columns = [
            ColumnMeta(
                name=r["name"],
                ordinal=r["cid"] + 1,
                sql_type=(r["type"] or "TEXT"),
                nullable=not bool(r["notnull"]),
            )
            for r in cols_raw
        ]
        pk = tuple(
            r["name"] for r in sorted(cols_raw, key=lambda x: x["pk"]) if r["pk"] > 0
        )
        unique_keys = self._unique_keys(table, pk)
        fks = self._foreign_keys(table)
        try:
            estimated = self._con.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"]
        except sqlite3.Error:
            estimated = None
        candidates = tuple(suggest_watermark_candidates(list(columns)))
        scanned_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return TableDetail(
            schema="main",
            name=table,
            object_type=otype,
            columns=tuple(columns),
            primary_key=pk,
            unique_keys=tuple(unique_keys),
            foreign_keys=tuple(fks),
            estimated_rows=estimated,
            watermark_candidates=candidates,
            schema_fingerprint=schema_fingerprint(list(columns), list(pk), unique_keys),
            scanned_at=scanned_at,
        )

    def _unique_keys(self, table: str, pk: tuple[str, ...]) -> list[KeyMeta]:
        out: list[KeyMeta] = []
        if pk:
            out.append(KeyMeta(name="PRIMARY", columns=pk, kind="primary"))
        indexes = self._con.execute(f'PRAGMA index_list("{table}")').fetchall()
        for idx in indexes:
            if not idx["unique"]:
                continue
            # PRAGMA index_list 的 partial 列(SQLite >= 3.8.0):过滤唯一索引不能作全表键
            keys = set(idx.keys())
            if "partial" in keys and idx["partial"]:
                continue
            name = idx["name"]
            if self._index_has_where_filter(name):
                continue
            cols = tuple(
                r["name"]
                for r in self._con.execute(f'PRAGMA index_info("{name}")').fetchall()
            )
            if not cols or cols == pk:
                continue
            out.append(KeyMeta(name=name, columns=cols, kind="unique_index"))
        return out

    def _index_has_where_filter(self, index_name: str) -> bool:
        """检测 CREATE UNIQUE INDEX ... WHERE ... 的部分唯一索引。"""
        row = self._con.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        if row is None or not row["sql"]:
            return False
        # 自动索引(UNIQUE 约束)无 sql;显式部分索引用 WHERE 子句
        return " where " in row["sql"].casefold()

    def _foreign_keys(self, table: str) -> list[ForeignKeyMeta]:
        rows = self._con.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        grouped: dict[int, list] = {}
        for r in rows:
            grouped.setdefault(r["id"], []).append(r)
        out: list[ForeignKeyMeta] = []
        for fid, group in grouped.items():
            group = sorted(group, key=lambda x: x["seq"])
            out.append(ForeignKeyMeta(
                name=f"fk_{fid}",
                columns=tuple(r["from"] for r in group),
                referenced_schema="main",
                referenced_table=group[0]["table"],
                referenced_columns=tuple(r["to"] for r in group),
            ))
        return out

    def check_key(self, schema: str, table: str, columns: list[str],
                  *, timeout_seconds: float = 30) -> KeyCheckResult:
        _ = timeout_seconds  # sqlite 无语句级超时钩子;调用方控制外层超时
        validate_identifier(table, kind="表名")
        if not columns:
            return KeyCheckResult(False, "key_missing", "未提供候选键列")
        for col in columns:
            validate_identifier(col, kind="键列名")
        detail = self.get_table(schema or "main", table)
        known = {c.name for c in detail.columns}
        missing = [c for c in columns if c not in known]
        if missing:
            return KeyCheckResult(False, "key_missing", f"字段不存在: {', '.join(missing)}")
        quoted = ", ".join(f'"{c}"' for c in columns)
        null_sql = (
            f'SELECT COUNT(*) AS c FROM "{table}" '
            f"WHERE {' OR '.join(f'\"{c}\" IS NULL' for c in columns)}"
        )
        null_count = self._con.execute(null_sql).fetchone()["c"]
        if null_count:
            return KeyCheckResult(
                False, "key_not_unique",
                f"候选键存在 NULL({null_count} 行)",
                null_count=null_count, duplicate_groups=None,
            )
        dup_sql = (
            f'SELECT COUNT(*) AS c FROM ('
            f'SELECT {quoted} FROM "{table}" GROUP BY {quoted} HAVING COUNT(*) > 1)'
        )
        dup_groups = self._con.execute(dup_sql).fetchone()["c"]
        if dup_groups:
            return KeyCheckResult(
                False, "key_not_unique",
                f"候选键存在重复({dup_groups} 组)",
                null_count=0, duplicate_groups=dup_groups,
            )
        return KeyCheckResult(True, "ready", "候选键唯一且无 NULL",
                              null_count=0, duplicate_groups=0)

    def check_watermark(self, schema: str, table: str, column: str) -> WatermarkCheckResult:
        validate_identifier(table, kind="表名")
        validate_identifier(column, kind="水位列名")
        detail = self.get_table(schema or "main", table)
        col = next((c for c in detail.columns if c.name == column), None)
        if col is None:
            return WatermarkCheckResult(False, "watermark_missing", "字段不存在")
        candidates = set(detail.watermark_candidates)
        candidate = column in candidates
        type_l = col.sql_type.lower()
        type_ok = any(h in type_l for h in (
            "date", "time", "int", "real", "num", "text",
        ))
        if not type_ok:
            return WatermarkCheckResult(
                False, "watermark_invalid", "字段类型不适合作为水位",
                sql_type=col.sql_type, candidate=candidate,
            )
        return WatermarkCheckResult(
            True, "ready",
            "水位字段可用(仍需现场确认)" if candidate else "字段存在,建议人工确认语义",
            sql_type=col.sql_type, candidate=candidate,
        )
