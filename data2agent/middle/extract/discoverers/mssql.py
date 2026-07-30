"""SQL Server 元数据发现(生产)。系统表 SQL 仅存在于本模块。"""

from __future__ import annotations

import os
import re
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
    map_odbc_error,
    schema_fingerprint,
    suggest_watermark_candidates,
    validate_identifier,
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def create_mssql_discoverer(scfg: SourceConfig) -> "MssqlMetadataDiscoverer":
    dsn = os.environ.get(scfg.dsn_env or "", "")
    if not dsn:
        raise MetadataError("missing_dsn", f"环境变量 {scfg.dsn_env} 未设置")
    return MssqlMetadataDiscoverer(dsn)


def _quote(ident: str) -> str:
    validate_identifier(ident)
    return f"[{ident}]"


def _qname(schema: str, table: str) -> str:
    return f"{_quote(schema)}.{_quote(table)}"


class MssqlMetadataDiscoverer:
    def __init__(self, conn_str: str, *, login_timeout: int = 10, query_timeout: int = 60):
        import pyodbc

        if "applicationintent" not in conn_str.lower():
            conn_str = conn_str.rstrip(";") + ";ApplicationIntent=ReadOnly"
        try:
            self._con = pyodbc.connect(conn_str, readonly=True, timeout=login_timeout)
            self._con.timeout = query_timeout
        except Exception as e:
            raise map_odbc_error(e) from e

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:
            pass

    def default_schema(self) -> str:
        return "dbo"

    def _fetch(self, sql: str, params: tuple = ()) -> list[dict]:
        try:
            cur = self._con.cursor()
            cur.execute(sql, params)
            if cur.description is None:
                return []
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        except MetadataError:
            raise
        except Exception as e:
            raise map_odbc_error(e) from e

    def list_schemas(self) -> list[str]:
        rows = self._fetch(
            "SELECT DISTINCT TABLE_SCHEMA AS s FROM INFORMATION_SCHEMA.TABLES "
            "ORDER BY TABLE_SCHEMA"
        )
        return [r["s"] for r in rows]

    def list_tables(
        self,
        *,
        schema: str | None = None,
        q: str | None = None,
        object_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[TableSummary], int]:
        where = ["1=1"]
        params: list = []
        if schema:
            validate_identifier(schema, kind="schema")
            where.append("t.TABLE_SCHEMA = ?")
            params.append(schema)
        if object_type == "table":
            where.append("t.TABLE_TYPE = 'BASE TABLE'")
        elif object_type == "view":
            where.append("t.TABLE_TYPE = 'VIEW'")
        if q:
            where.append("t.TABLE_NAME LIKE ?")
            params.append(f"%{q}%")
        where_sql = " AND ".join(where)
        count_row = self._fetch(
            f"SELECT COUNT(*) AS c FROM INFORMATION_SCHEMA.TABLES t WHERE {where_sql}",
            tuple(params),
        )[0]
        total = int(count_row["c"])
        rows = self._fetch(
            f"""
            SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE
            FROM INFORMATION_SCHEMA.TABLES t
            WHERE {where_sql}
            ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME
            OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
            """,
            tuple(params) + (offset, limit),
        )
        # 列表只返回目录项;不在此调用 get_table。
        # 详情/键/水位由扫描任务或详情接口拉取,失败才能计入 table_errors / partial。
        summaries = [
            TableSummary(
                schema=r["TABLE_SCHEMA"],
                name=r["TABLE_NAME"],
                object_type="view" if r["TABLE_TYPE"] == "VIEW" else "table",
                estimated_rows=None,
                primary_key=(),
                unique_keys=(),
                watermark_candidates=(),
            )
            for r in rows
        ]
        return summaries, total

    def get_table(self, schema: str, table: str) -> TableDetail:
        schema = schema or "dbo"
        validate_identifier(schema, kind="schema")
        validate_identifier(table, kind="表名")
        cols = self._fetch(
            """
            SELECT COLUMN_NAME, DATA_TYPE, ORDINAL_POSITION, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
            """,
            (schema, table),
        )
        if not cols:
            raise MetadataError("table_missing", f"表 '{schema}.{table}' 不存在")
        columns = [
            ColumnMeta(
                name=r["COLUMN_NAME"],
                ordinal=int(r["ORDINAL_POSITION"]),
                sql_type=str(r["DATA_TYPE"]),
                nullable=str(r["IS_NULLABLE"]).upper() == "YES",
            )
            for r in cols
        ]
        pk_rows = self._fetch(
            """
            SELECT kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
             AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
             AND kcu.TABLE_NAME = tc.TABLE_NAME
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
              AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
            ORDER BY kcu.ORDINAL_POSITION
            """,
            (schema, table),
        )
        pk = tuple(r["COLUMN_NAME"] for r in pk_rows)
        unique_keys = self._unique_keys(schema, table, pk)
        fks = self._foreign_keys(schema, table)
        estimated = self._estimated_rows(schema, table)
        candidates = tuple(suggest_watermark_candidates(list(columns)))
        scanned_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        otype_rows = self._fetch(
            "SELECT TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            (schema, table),
        )
        otype = "view" if otype_rows and otype_rows[0]["TABLE_TYPE"] == "VIEW" else "table"
        return TableDetail(
            schema=schema,
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

    def _unique_keys(self, schema: str, table: str, pk: tuple[str, ...]) -> list[KeyMeta]:
        out: list[KeyMeta] = []
        if pk:
            out.append(KeyMeta(name="PRIMARY", columns=pk, kind="primary"))
        rows = self._fetch(
            """
            SELECT tc.CONSTRAINT_NAME, kcu.COLUMN_NAME, kcu.ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
             AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
             AND kcu.TABLE_NAME = tc.TABLE_NAME
            WHERE tc.CONSTRAINT_TYPE = 'UNIQUE'
              AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
            ORDER BY tc.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
            """,
            (schema, table),
        )
        grouped: dict[str, list[str]] = {}
        for r in rows:
            grouped.setdefault(r["CONSTRAINT_NAME"], []).append(r["COLUMN_NAME"])
        for name, cols in grouped.items():
            tup = tuple(cols)
            if tup == pk:
                continue
            out.append(KeyMeta(name=name, columns=tup, kind="unique_constraint"))
        # unique indexes that are not constraints
        idx_rows = self._fetch(
            """
            SELECT i.name AS index_name, c.name AS column_name, ic.key_ordinal
            FROM sys.indexes i
            JOIN sys.index_columns ic
              ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
              ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE i.is_unique = 1 AND i.is_primary_key = 0 AND i.is_unique_constraint = 0
              AND i.has_filter = 0
              AND s.name = ? AND t.name = ? AND ic.is_included_column = 0
            ORDER BY i.name, ic.key_ordinal
            """,
            (schema, table),
        )
        idx_grouped: dict[str, list[str]] = {}
        for r in idx_rows:
            idx_grouped.setdefault(r["index_name"], []).append(r["column_name"])
        for name, cols in idx_grouped.items():
            out.append(KeyMeta(name=name, columns=tuple(cols), kind="unique_index"))
        return out

    def _foreign_keys(self, schema: str, table: str) -> list[ForeignKeyMeta]:
        rows = self._fetch(
            """
            SELECT fk.name AS fk_name,
                   c1.name AS col_name,
                   s2.name AS ref_schema,
                   t2.name AS ref_table,
                   c2.name AS ref_col,
                   fkc.constraint_column_id AS ord
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.tables t1 ON t1.object_id = fk.parent_object_id
            JOIN sys.schemas s1 ON s1.schema_id = t1.schema_id
            JOIN sys.columns c1 ON c1.object_id = fkc.parent_object_id
                               AND c1.column_id = fkc.parent_column_id
            JOIN sys.tables t2 ON t2.object_id = fk.referenced_object_id
            JOIN sys.schemas s2 ON s2.schema_id = t2.schema_id
            JOIN sys.columns c2 ON c2.object_id = fkc.referenced_object_id
                               AND c2.column_id = fkc.referenced_column_id
            WHERE s1.name = ? AND t1.name = ?
            ORDER BY fk.name, fkc.constraint_column_id
            """,
            (schema, table),
        )
        grouped: dict[str, list[dict]] = {}
        for r in rows:
            grouped.setdefault(r["fk_name"], []).append(r)
        out: list[ForeignKeyMeta] = []
        for name, group in grouped.items():
            group = sorted(group, key=lambda x: x["ord"])
            out.append(ForeignKeyMeta(
                name=name,
                columns=tuple(r["col_name"] for r in group),
                referenced_schema=group[0]["ref_schema"],
                referenced_table=group[0]["ref_table"],
                referenced_columns=tuple(r["ref_col"] for r in group),
            ))
        return out

    def _estimated_rows(self, schema: str, table: str) -> int | None:
        rows = self._fetch(
            """
            SELECT SUM(p.rows) AS row_count
            FROM sys.tables t
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            WHERE s.name = ? AND t.name = ?
            """,
            (schema, table),
        )
        if not rows or rows[0]["row_count"] is None:
            return None
        return int(rows[0]["row_count"])

    def check_key(self, schema: str, table: str, columns: list[str],
                  *, timeout_seconds: float = 30) -> KeyCheckResult:
        schema = schema or "dbo"
        if not columns:
            return KeyCheckResult(False, "key_missing", "未提供候选键列")
        for col in columns:
            validate_identifier(col, kind="键列名")
        try:
            detail = self.get_table(schema, table)
        except MetadataError as e:
            return KeyCheckResult(False, e.code, e.message)
        known = {c.name for c in detail.columns}
        missing = [c for c in columns if c not in known]
        if missing:
            return KeyCheckResult(False, "key_missing", f"字段不存在: {', '.join(missing)}")

        old_timeout = self._con.timeout
        self._con.timeout = max(1, int(timeout_seconds))
        try:
            qtable = _qname(schema, table)
            null_pred = " OR ".join(f"{_quote(c)} IS NULL" for c in columns)
            null_row = self._fetch(
                f"SELECT COUNT_BIG(*) AS c FROM {qtable} WHERE {null_pred}"
            )[0]
            null_count = int(null_row["c"])
            if null_count:
                return KeyCheckResult(
                    False, "key_not_unique",
                    f"候选键存在 NULL({null_count} 行)",
                    null_count=null_count,
                )
            group_cols = ", ".join(_quote(c) for c in columns)
            dup_row = self._fetch(
                f"SELECT COUNT_BIG(*) AS c FROM ("
                f"SELECT {group_cols} FROM {qtable} "
                f"GROUP BY {group_cols} HAVING COUNT_BIG(*) > 1) d"
            )[0]
            dup_groups = int(dup_row["c"])
            if dup_groups:
                return KeyCheckResult(
                    False, "key_not_unique",
                    f"候选键存在重复({dup_groups} 组)",
                    null_count=0, duplicate_groups=dup_groups,
                )
            return KeyCheckResult(True, "ready", "候选键唯一且无 NULL",
                                  null_count=0, duplicate_groups=0)
        except MetadataError as e:
            return KeyCheckResult(False, e.code, e.message)
        except Exception as e:
            msg = str(e)
            if "timeout" in msg.lower():
                return KeyCheckResult(False, "timeout", "键校验超时")
            if "permission" in msg.lower() or "denied" in msg.lower():
                return KeyCheckResult(False, "permission_denied", "无权限校验候选键")
            return KeyCheckResult(False, "key_check_failed", "键校验失败")
        finally:
            self._con.timeout = old_timeout

    def check_watermark(self, schema: str, table: str, column: str) -> WatermarkCheckResult:
        schema = schema or "dbo"
        try:
            detail = self.get_table(schema, table)
        except MetadataError as e:
            return WatermarkCheckResult(False, e.code, e.message)
        validate_identifier(column, kind="水位列名")
        col = next((c for c in detail.columns if c.name == column), None)
        if col is None:
            return WatermarkCheckResult(False, "watermark_missing", "字段不存在")
        candidate = column in detail.watermark_candidates
        type_l = col.sql_type.lower()
        allowed = any(h in type_l for h in (
            "date", "time", "int", "bigint", "smallint", "numeric", "decimal",
            "float", "real", "rowversion", "timestamp",
        ))
        if not allowed:
            return WatermarkCheckResult(
                False, "watermark_invalid", "字段类型不适合作为水位",
                sql_type=col.sql_type, candidate=candidate,
            )
        return WatermarkCheckResult(
            True, "ready",
            "水位字段可用(仍需现场确认)" if candidate else "字段存在,建议人工确认语义",
            sql_type=col.sql_type, candidate=candidate,
        )
