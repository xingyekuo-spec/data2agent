"""MSSQL 适配器集成测试(容器内运行,环境变量门控)。

验证与 sqlite 适配器的行为等价:全量落地、水位增量、keyset 分页、
只读账号 + 只读守卫。本地 / CI 触发方式见 docker-compose.yml 头注释。
"""

import os
from pathlib import Path

import pytest

from data2agent.middle.extract.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore, raw_table_name
from tests.helpers import whitelist_from_pack
from data2agent.shared.metamodel.loader import load_pack

DSN = os.environ.get("D2A_IT_MSSQL_DSN")
SA_DSN = os.environ.get("D2A_IT_MSSQL_SA_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="需要 MSSQL 集成环境(tests/integration/mssql/docker-compose.yml)")

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "digiwin_e10"
WM = "LAST_MODIFIED_DATE"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def landing(tmp_path):
    return LandingStore(tmp_path / "landing.sqlite")


def _adapter(pack, **kw):
    from data2agent.middle.extract.adapters.mssql import MssqlReadOnlyAdapter
    return MssqlReadOnlyAdapter(DSN, whitelist_from_pack(pack, SOURCE), **kw)


def _sync(pack, landing, **kw):
    return incremental_sync(_adapter(pack, **kw.pop("adapter_kw", {})), landing,
                            SOURCE, watermarks_from_pack(pack, SOURCE), **kw)


def test_initial_sync_counts(pack, landing):
    report = _sync(pack, landing, adapter_kw={"batch_size": 64})
    by = {t.table: t for t in report.tables}
    assert by["QUOTATION"].rows == 180 and by["QUOTATION"].batches == 3
    assert by["SALES_ORDER_D"].rows == 239
    assert landing.count(SOURCE, "SALES_ORDER") == 97
    assert landing.get_high_water(SOURCE, "SALES_ORDER")


def test_incremental_picks_up_change(pack, landing):
    import pyodbc

    _sync(pack, landing)
    sa = pyodbc.connect(SA_DSN + ";DATABASE=d2a_e10")
    sa.autocommit = True
    sa.execute(f"UPDATE CUSTOMER SET CUSTOMER_NAME = N'集成改名', {WM} = '2026-07-12 09:00:00' "
               "WHERE Id = 3")
    report = _sync(pack, landing)
    cust = next(t for t in report.tables if t.table == "CUSTOMER")
    assert cust.strategy == "increment"
    row = landing.con.execute(
        f'SELECT CUSTOMER_NAME FROM "{raw_table_name(SOURCE, "CUSTOMER")}" WHERE Id = 3'
    ).fetchone()
    assert row["CUSTOMER_NAME"] == "集成改名"
    assert landing.get_high_water(SOURCE, "CUSTOMER") == "2026-07-12 09:00:00"
    sa.execute(f"UPDATE CUSTOMER SET CUSTOMER_NAME = N'恢复', {WM} = '2026-07-12 09:30:00' "
               "WHERE Id = 3")  # 留给后续轮次,不影响幂等


def test_reconcile_catches_physical_delete(pack, landing):
    import pyodbc

    from data2agent.middle.extract.reconcile import reconcile

    _sync(pack, landing)
    sa = pyodbc.connect(SA_DSN + ";DATABASE=d2a_e10")
    sa.autocommit = True
    sa.execute("DELETE FROM SALES_ORDER_D WHERE Id = 7")
    report = reconcile(_adapter(pack), landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    assert report.total_soft_deleted == 1
    row = landing.con.execute(
        f'SELECT _d2a_deleted_at FROM "{raw_table_name(SOURCE, "SALES_ORDER_D")}" WHERE Id = 7'
    ).fetchone()
    assert row["_d2a_deleted_at"] is not None


def test_readonly_account_and_guard(pack):
    import pyodbc

    from data2agent.middle.extract.adapters.base import ReadOnlyViolation

    adapter = _adapter(pack)
    with pytest.raises(ReadOnlyViolation):
        adapter._audited_fetch("DELETE FROM CUSTOMER")   # 守卫在发出前拦截
    with pytest.raises(pyodbc.Error):
        adapter.con.execute("DELETE FROM CUSTOMER")      # 即便绕过守卫,只读账号无权限
