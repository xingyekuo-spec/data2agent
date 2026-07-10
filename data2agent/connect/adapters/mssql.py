"""SQL Server 只读适配器:鼎捷 E10 / 易飞 生产环境用(pyodbc,惰性导入)。

连接要求:只读账号 + ApplicationIntent=ReadOnly(缺失则自动追加);
连接与语句超时默认收紧,错峰窗口 / 限流由上层与基类共同强制。
"""

from __future__ import annotations

from .base import SourceAdapter, TableInfo

_TYPE_MAP = {
    "int": "int", "bigint": "int", "smallint": "int", "tinyint": "int", "bit": "int",
    "decimal": "real", "numeric": "real", "float": "real", "real": "real",
    "money": "real", "smallmoney": "real",
    "binary": "blob", "varbinary": "blob", "image": "blob",
}

_COLUMNS_SQL = """SELECT COLUMN_NAME, DATA_TYPE, ORDINAL_POSITION
FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION"""

_PK_SQL = """SELECT kcu.COLUMN_NAME
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_NAME = ?
ORDER BY kcu.ORDINAL_POSITION"""


class MssqlReadOnlyAdapter(SourceAdapter):
    def __init__(self, conn_str: str, whitelist: set[str], *,
                 login_timeout: int = 10, query_timeout: int = 300, **kwargs):
        super().__init__(whitelist, **kwargs)
        import pyodbc  # 惰性导入:connect 依赖组未装时,其余功能不受影响

        if "applicationintent" not in conn_str.lower():
            conn_str = conn_str.rstrip(";") + ";ApplicationIntent=ReadOnly"
        self.con = pyodbc.connect(conn_str, readonly=True, timeout=login_timeout)
        self.con.timeout = query_timeout  # 语句超时(秒)

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.con.cursor()
        cur.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def table_info(self, name: str) -> TableInfo:
        self._check_table(name)
        cols = self._audited_fetch(_COLUMNS_SQL, (name,), action="schema")
        if not cols:
            raise ValueError(f"源库中不存在表 '{name}'")
        columns = [(r["COLUMN_NAME"], _TYPE_MAP.get(r["DATA_TYPE"].lower(), "text")) for r in cols]
        pk_rows = self._audited_fetch(_PK_SQL, (name,), action="schema")
        return TableInfo(name=name, columns=columns, pk=[r["COLUMN_NAME"] for r in pk_rows])

    def _page_sql(self, table: TableInfo, limit: int, offset: int) -> str:
        cols = ", ".join(f"[{c}]" for c, _ in table.columns)
        order = ", ".join(f"[{k}]" for k in table.pk) or "(SELECT NULL)"
        return (f"SELECT {cols} FROM [{table.name}] ORDER BY {order} "
                f"OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY")
