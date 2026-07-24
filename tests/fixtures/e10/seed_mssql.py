"""向 SQL Server 灌入 E10-like 表形 + seed 数据,并创建只读账号。

双重用途:MSSQL 适配器集成测试(tests/integration/mssql)。等待 MSSQL 就绪 → 建库 → 建只读账号
(d2a_reader,db_datareader)→ 按 e10_schema.TABLES 建表(_DATE 列用 DATETIME2,
其余 TEXT→NVARCHAR、NUMERIC→DECIMAL)→ 插入 build() 数据。幂等:重跑先删表。
入口:python -m tests.fixtures.e10.seed_mssql(需环境变量 D2A_IT_MSSQL_SA_DSN)。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date

import pyodbc

from tests.fixtures.e10.schema import TABLES
from tests.fixtures.e10.seed import build

SA_DSN = os.environ["D2A_IT_MSSQL_SA_DSN"]
DB = "d2a_e10"


def _mssql_type(col: str, decl: str) -> str:
    if "PRIMARY KEY" in decl:
        return "INT PRIMARY KEY"
    if decl == "INTEGER":
        return "INT"
    if decl == "NUMERIC":
        return "DECIMAL(18, 4)"
    if col.endswith("_DATE"):        # 真实 E10 为日期时间列,还原之
        return "DATETIME2(0)"
    return "NVARCHAR(400)"


def wait_ready(dsn: str, tries: int = 90) -> pyodbc.Connection:
    for i in range(tries):
        try:
            return pyodbc.connect(dsn, timeout=5)
        except pyodbc.Error:
            time.sleep(2)
    print("MSSQL 未在预期时间内就绪", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    sa = wait_ready(SA_DSN)
    sa.autocommit = True
    cur = sa.cursor()
    cur.execute(f"IF DB_ID('{DB}') IS NULL CREATE DATABASE {DB}")
    cur.execute("IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'd2a_reader') "
                "CREATE LOGIN d2a_reader WITH PASSWORD = 'D2a!Reader1'")

    db = pyodbc.connect(SA_DSN + f";DATABASE={DB}")
    db.autocommit = True
    cur = db.cursor()
    cur.execute("IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'd2a_reader') "
                "CREATE USER d2a_reader FOR LOGIN d2a_reader")
    cur.execute("ALTER ROLE db_datareader ADD MEMBER d2a_reader")

    data = build(seed=42, asof=date(2026, 7, 10))
    for table, (_, cols) in TABLES.items():
        cur.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE [{table}]")
        body = ",\n".join(f"  [{c}] {_mssql_type(c, t)}" for c, t, _ in cols)
        cur.execute(f"CREATE TABLE [{table}] (\n{body}\n)")
        names = [c for c, _, _ in cols]
        ins = (f"INSERT INTO [{table}] ({', '.join(f'[{c}]' for c in names)}) "
               f"VALUES ({', '.join('?' for _ in names)})")
        cur.fast_executemany = True
        cur.executemany(ins, [[r.get(c) for c in names] for r in data[table]])
        print(f"  {table}: {len(data[table])} 行")
    print("MSSQL 集成环境就绪(库 d2a_e10,只读账号 d2a_reader)")


if __name__ == "__main__":
    main()
