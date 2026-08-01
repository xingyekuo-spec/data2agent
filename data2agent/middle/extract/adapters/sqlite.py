"""SQLite 只读适配器:开发 / 参考库 / 测试用,与 mssql 适配器行为等价。"""

from __future__ import annotations

import sqlite3

from .base import SourceAdapter, TableInfo

_TYPE_MAP = [  # decltype 关键词 -> 可移植类型(按序匹配)
    ("INT", "int"),
    ("REAL", "real"), ("FLOA", "real"), ("DOUB", "real"),
    # 与 SQL Server 路径一致，精确十进制数以文本保存，避免 float 舍入。
    ("NUMERIC", "text"), ("DECIMAL", "text"), ("MONEY", "text"),
    ("BLOB", "blob"),
]


def _portable_type(decltype: str | None) -> str:
    d = (decltype or "").upper()
    for key, t in _TYPE_MAP:
        if key in d:
            return t
    return "text"


class SqliteReadOnlyAdapter(SourceAdapter):
    def __init__(self, db_path: str, whitelist: set[str], **kwargs):
        super().__init__(whitelist, **kwargs)
        self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, params)]

    def _stream_execute(self, sql: str, params: tuple = ()):
        cur = self.con.execute(sql, params)
        try:
            while True:
                batch = cur.fetchmany(self.batch_size)
                if not batch:
                    return
                yield [dict(r) for r in batch]
        finally:
            cur.close()

    def table_info(self, name: str) -> TableInfo:
        self._check_table(name)
        rows = self._audited_fetch(f'PRAGMA table_info("{name}")', action="schema")
        if not rows:
            raise ValueError(f"源库中不存在表 '{name}'")
        columns = [(r["name"], _portable_type(r["type"])) for r in rows]
        pk = [r["name"] for r in sorted(rows, key=lambda r: r["pk"]) if r["pk"] > 0]
        return TableInfo(
            name=name, columns=columns, pk=pk, key_source="database_pk",
            schema=self.table_schemas.get(name, "main"))

    def _page_sql(self, table: TableInfo, limit: int, offset: int) -> str:
        cols = ", ".join(f'"{c}"' for c, _ in table.columns)
        order = ", ".join(f'"{k}"' for k in table.pk) or "1"
        return f'SELECT {cols} FROM "{table.name}" ORDER BY {order} LIMIT {limit} OFFSET {offset}'

    def _quote(self, ident: str) -> str:
        return f'"{ident}"'

    def _limit_clause(self, limit: int) -> str:
        return f" LIMIT {int(limit)}"
