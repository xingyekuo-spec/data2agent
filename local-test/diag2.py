# diag2.py — 验证 SQL Server 版本是否支持 OFFSET/FETCH 分页语法
# 在中间机便携包目录执行:
#   cd C:\d2a-portable-middle-manual-81273e2\runtime
#   $env:D2A_E10_DSN="(secrets.env 里的真实值)"
#   .\python.exe diag2.py
import os
import pyodbc

dsn = os.environ["D2A_E10_DSN"]
if "applicationintent" not in dsn.lower():
    dsn = dsn.rstrip(";") + ";ApplicationIntent=ReadOnly"

con = pyodbc.connect(dsn, readonly=True, timeout=10)
con.timeout = 60
cur = con.cursor()

# 1. 服务器版本与数据库兼容级别
cur.execute("SELECT @@VERSION")
print("VERSION:", cur.fetchone()[0].split("\n")[0])
cur.execute("SELECT DB_NAME()")
db = cur.fetchone()[0]
cur.execute(
    "SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()")
print("DB:", db, "| compatibility_level:", cur.fetchone()[0],
      "(100=SQL2008, 110=2012, 120=2014, 130=2016, 140=2017, 150=2019, 160=2022)")

# 2. 应用元数据扫描实际使用的分页查询(OFFSET/FETCH)
print("\n--- 测试应用的扫描分页查询(OFFSET/FETCH)---")
try:
    cur.execute(
        "SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE "
        "FROM INFORMATION_SCHEMA.TABLES t "
        "ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME "
        "OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY")
    rows = cur.fetchall()
    print("OFFSET/FETCH OK, 返回", len(rows), "行 → 扫描失败另有原因,请反馈")
except Exception as e:
    print("OFFSET/FETCH 失败 → 确认根因:")
    print(type(e).__name__, e)

# 3. 兼容写法(ROW_NUMBER,SQL 2005+ 均支持)
print("\n--- 测试兼容分页写法(ROW_NUMBER)---")
try:
    cur.execute(
        "SELECT * FROM ("
        "  SELECT t.TABLE_SCHEMA, t.TABLE_NAME, t.TABLE_TYPE,"
        "         ROW_NUMBER() OVER (ORDER BY t.TABLE_SCHEMA, t.TABLE_NAME) AS rn"
        "  FROM INFORMATION_SCHEMA.TABLES t"
        ") x WHERE rn > 0 AND rn <= 5")
    rows = cur.fetchall()
    print("ROW_NUMBER OK, 返回", len(rows), "行 → 可用此写法修复")
except Exception as e:
    print("ROW_NUMBER 也失败:")
    print(type(e).__name__, e)
