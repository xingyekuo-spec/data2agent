"""SQL Server 只读适配器:鼎捷 E10 / 易飞 生产环境用(pyodbc,惰性导入)。

连接要求:只读账号 + ApplicationIntent=ReadOnly(缺失则自动追加);
连接与语句超时默认收紧,错峰窗口 / 限流由上层与基类共同强制。
"""

from __future__ import annotations

from .base import SourceAdapter, TableInfo

_TYPE_MAP = {
    "int": "int", "bigint": "int", "smallint": "int", "tinyint": "int", "bit": "int",
    # 精确数值以十进制文本传输/落地，禁止 Decimal → IEEE-754 float 丢精度。
    "decimal": "text", "numeric": "text", "money": "text", "smallmoney": "text",
    "float": "real", "real": "real",
    "binary": "blob", "varbinary": "blob", "image": "blob",
}

_COLUMNS_SQL = """SELECT COLUMN_NAME, DATA_TYPE, ORDINAL_POSITION
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"""

_PK_SQL = """SELECT kcu.COLUMN_NAME
FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
  ON kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
 AND kcu.TABLE_SCHEMA = tc.TABLE_SCHEMA
 AND kcu.TABLE_NAME = tc.TABLE_NAME
WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
  AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
ORDER BY kcu.ORDINAL_POSITION"""


class MssqlReadOnlyAdapter(SourceAdapter):
    def __init__(self, conn_str: str, whitelist: set[str], *,
                 login_timeout: int = 10, query_timeout: int = 300, **kwargs):
        super().__init__(whitelist, **kwargs)
        import pyodbc  # 惰性导入:connect 依赖组未装时,其余功能不受影响

        if "applicationintent" not in conn_str.lower():
            conn_str = conn_str.rstrip(";") + ";ApplicationIntent=ReadOnly"
        if "encrypt=" not in conn_str.lower():
            conn_str = conn_str.rstrip(";") + ";Encrypt=yes"
        if "trustservercertificate=" not in conn_str.lower():
            conn_str = (
                conn_str.rstrip(";") + ";TrustServerCertificate=no")
        # 普通增量查询用 autocommit，避免 pyodbc 默认事务跨 HTTP
        # 推送长期持有 ERP 共享锁。每条 SELECT 仍是语句级一致读。
        self.con = pyodbc.connect(
            conn_str, readonly=True, timeout=login_timeout, autocommit=True)
        self.con.timeout = query_timeout  # 语句超时(秒)

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.con.cursor()
        cur.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def _stream_execute(self, sql: str, params: tuple = ()):
        cur = self.con.cursor()
        try:
            # full_refresh 要求整条 SELECT 是同一数据库快照。优先使用
            # SNAPSHOT（不阻塞 ERP 写）；数据库未开启时退化到
            # SERIALIZABLE。上层会先将游标快速落本机 spool 再推送，
            # 因此退化锁不会被 HTTP 延迟拉长。
            try:
                cur.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
                cur.execute(sql, params)
            except Exception as exc:
                message = str(exc).lower()
                if "snapshot isolation" not in message and "3951" not in message:
                    raise
                cur.close()
                cur = self.con.cursor()
                cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            while True:
                batch = cur.fetchmany(self.batch_size)
                if not batch:
                    break
                yield [dict(zip(names, row)) for row in batch]
        finally:
            cur.close()
            try:
                reset = self.con.cursor()
                reset.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                reset.close()
            except Exception:
                pass

    def table_info(self, name: str) -> TableInfo:
        self._check_table(name)
        schema = self.table_schemas.get(name, "dbo")
        cols = self._audited_fetch(_COLUMNS_SQL, (schema, name), action="schema")
        if not cols:
            raise ValueError(f"源库中不存在表 '{schema}.{name}'")
        columns = [(r["COLUMN_NAME"], _TYPE_MAP.get(r["DATA_TYPE"].lower(), "text")) for r in cols]
        pk_rows = self._audited_fetch(_PK_SQL, (schema, name), action="schema")
        return TableInfo(
            name=name,
            columns=columns,
            pk=[r["COLUMN_NAME"] for r in pk_rows],
            key_source="database_pk",
            schema=schema,
        )

    def validate_watermark(self, table: TableInfo, column: str) -> None:
        schema = table.schema or self.table_schemas.get(table.name, "dbo")
        rows = self._audited_fetch(
            "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ?",
            (schema, table.name, column), action="validate_watermark_type")
        if not rows:
            raise ValueError(f"{table.name}: 水位列 '{column}' 不存在")
        sql_type = str(rows[0]["DATA_TYPE"]).lower()
        allowed = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset"}
        if sql_type not in allowed:
            raise ValueError(
                f"{table.name}: 水位列 '{column}' 类型为 {sql_type}，"
                f"只支持 {sorted(allowed)}；time/rowversion 不支持日历回看")
        super().validate_watermark(table, column)

    def _page_sql(self, table: TableInfo, limit: int, offset: int) -> str:
        # ROW_NUMBER 分页:兼容 SQL Server 2008 R2(不支持 OFFSET/FETCH,2012+)
        cols = ", ".join(f"[{c}]" for c, _ in table.columns)
        order = ", ".join(f"[{k}]" for k in table.pk) or "(SELECT NULL)"
        return (
            f"SELECT TOP {int(limit)} {cols} FROM ("
            f"SELECT {cols}, ROW_NUMBER() OVER (ORDER BY {order}) AS _d2a_rn "
            f"FROM {self._table_ref(table)}"
            f") AS _d2a_p WHERE _d2a_rn > {int(offset)} ORDER BY _d2a_rn")

    def _quote(self, ident: str) -> str:
        return f"[{ident}]"

    def _table_ref(self, table: TableInfo) -> str:
        schema = table.schema or getattr(self, "table_schemas", {}).get(
            table.name, "dbo")
        return f"{self._quote(schema)}.{self._quote(table.name)}"

    def _limit_clause(self, limit: int) -> str:
        return f" TOP {int(limit)}"
