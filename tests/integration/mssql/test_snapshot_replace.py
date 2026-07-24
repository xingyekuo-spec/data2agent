"""MSSQL full_refresh 快照原子替换集成测试(环境变量门控)。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from data2agent.connect.adapters.mssql import MssqlReadOnlyAdapter
from data2agent.connect.increment import incremental_sync
from data2agent.connect.landing import LandingStore, raw_table_name

DSN = os.environ.get("D2A_IT_MSSQL_DSN")
SA_DSN = os.environ.get("D2A_IT_MSSQL_SA_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="需要 MSSQL 集成环境(tests/integration/mssql/docker-compose.yml)")

SOURCE = "digiwin_e10"
TEMP_CODE = "D2A_SNAP_TMP"


def test_full_refresh_snapshot_removes_deleted_row(tmp_path: Path):
    """插入临时币别 → 全量快照 → 删除临时行 → 再快照，raw 中应消失。"""
    import pyodbc

    if not SA_DSN:
        pytest.skip("需要 D2A_IT_MSSQL_SA_DSN 以写入临时行")

    sa = pyodbc.connect(SA_DSN + ";DATABASE=d2a_e10")
    sa.autocommit = True
    # 清理可能残留的临时行
    sa.execute("DELETE FROM CURRENCY WHERE CURRENCY_CODE = ?", TEMP_CODE)
    next_id = sa.execute("SELECT ISNULL(MAX(Id), 0) + 1 FROM CURRENCY").fetchone()[0]
    sa.execute(
        "INSERT INTO CURRENCY (Id, CURRENCY_CODE, CURRENCY_NAME) VALUES (?, ?, ?)",
        next_id, TEMP_CODE, "snapshot-temp",
    )

    landing = LandingStore(tmp_path / "landing.sqlite")
    whitelist = {"CURRENCY"}
    try:
        incremental_sync(
            MssqlReadOnlyAdapter(DSN, whitelist, batch_size=64),
            landing, SOURCE, watermarks={})
        codes = {
            r[0] for r in landing.con.execute(
                f'SELECT CURRENCY_CODE FROM "{raw_table_name(SOURCE, "CURRENCY")}"')
        }
        assert TEMP_CODE in codes

        sa.execute("DELETE FROM CURRENCY WHERE CURRENCY_CODE = ?", TEMP_CODE)
        incremental_sync(
            MssqlReadOnlyAdapter(DSN, whitelist, batch_size=64),
            landing, SOURCE, watermarks={})
        codes_after = {
            r[0] for r in landing.con.execute(
                f'SELECT CURRENCY_CODE FROM "{raw_table_name(SOURCE, "CURRENCY")}"')
        }
        assert TEMP_CODE not in codes_after
    finally:
        sa.execute("DELETE FROM CURRENCY WHERE CURRENCY_CODE = ?", TEMP_CODE)
        sa.close()
