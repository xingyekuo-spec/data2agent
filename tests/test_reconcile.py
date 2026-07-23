"""抽取框架 E3 测试:分段对账 L1/L2、软删、复活、deep 兜底原地改动。"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.reconcile import month_segments, reconcile
from tests.helpers import whitelist_from_pack
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
WM = "LAST_MODIFIED_DATE"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def env(tmp_path, pack):
    """seed 源库 + 已完成初始同步的落地库。"""
    source_db = tmp_path / "source.sqlite"
    write_db(source_db, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    incremental_sync(_adapter(source_db, pack), landing, SOURCE,
                     watermarks_from_pack(pack, SOURCE))
    return source_db, landing


def _adapter(source_db, pack, **kw):
    return SqliteReadOnlyAdapter(str(source_db), whitelist_from_pack(pack, SOURCE), **kw)


def _reconcile(source_db, pack, landing, **kw):
    return reconcile(_adapter(source_db, pack), landing, SOURCE,
                     watermarks_from_pack(pack, SOURCE), **kw)


def test_month_segments():
    segs = month_segments("2025-11-15 08:00:00", "2026-02-01 00:00:00")
    assert [s[0] for s in segs] == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert segs[0][1] == "2025-11-01 00:00:00" and segs[0][2] == "2025-12-01 00:00:00"


def test_clean_state_is_consistent(env, pack):
    source_db, landing = env
    report = _reconcile(source_db, pack, landing)
    assert report.mismatched == [] and report.total_soft_deleted == 0
    assert all(s.repaired_rows == 0 for s in report.segments), "一致段不应触发 L2"


def test_physical_delete_soft_deleted(env, pack):
    source_db, landing = env
    rw = sqlite3.connect(source_db)
    (wm,) = rw.execute(f"SELECT {WM} FROM QUOTATION WHERE Id = 10").fetchone()
    rw.execute("DELETE FROM QUOTATION WHERE Id = 10")
    rw.commit()

    report = _reconcile(source_db, pack, landing)
    bad = [s for s in report.mismatched if s.table == "QUOTATION"]
    assert len(bad) == 1 and bad[0].segment == wm[:7], "L1 应定位到被删行所在月段"
    assert bad[0].soft_deleted == 1

    row = landing.con.execute(
        f'SELECT _d2a_deleted_at FROM "{raw_table_name(SOURCE, "QUOTATION")}" WHERE Id = 10'
    ).fetchone()
    assert row["_d2a_deleted_at"] is not None, "落地行应软删而非物理删"
    assert landing.count(SOURCE, "QUOTATION", active_only=True) == 179
    assert landing.count(SOURCE, "QUOTATION") == 180


def test_dim_table_delete_and_repair(env, pack):
    source_db, landing = env
    rw = sqlite3.connect(source_db)
    rw.execute("DELETE FROM CURRENCY WHERE Id = 4")
    rw.commit()
    report = _reconcile(source_db, pack, landing)
    cur = next(s for s in report.segments if s.table == "CURRENCY")
    assert not cur.consistent and cur.soft_deleted == 1
    assert landing.count(SOURCE, "CURRENCY", active_only=True) == 3


def test_inplace_edit_needs_deep(env, pack):
    """不动水位的原地改动:L1 察觉不到(已知边界),deep 修复。"""
    source_db, landing = env
    rw = sqlite3.connect(source_db)
    rw.execute("UPDATE ITEM SET STANDARD_COST = 999.99 WHERE Id = 1")  # 不改水位
    rw.commit()

    report = _reconcile(source_db, pack, landing)
    assert all(s.consistent for s in report.segments if s.table == "ITEM"), \
        "L1 的 COUNT+MAX 不应察觉纯原地改动(设计已声明的边界)"
    stale = landing.con.execute(
        f'SELECT STANDARD_COST FROM "{raw_table_name(SOURCE, "ITEM")}" WHERE Id = 1').fetchone()
    assert stale["STANDARD_COST"] != 999.99

    _reconcile(source_db, pack, landing, deep=True)
    fixed = landing.con.execute(
        f'SELECT STANDARD_COST FROM "{raw_table_name(SOURCE, "ITEM")}" WHERE Id = 1').fetchone()
    assert fixed["STANDARD_COST"] == 999.99


def test_soft_deleted_row_revives(env, pack):
    source_db, landing = env
    rw = sqlite3.connect(source_db)
    row = rw.execute("SELECT * FROM CURRENCY WHERE Id = 2").fetchone()
    rw.execute("DELETE FROM CURRENCY WHERE Id = 2")
    rw.commit()
    _reconcile(source_db, pack, landing)
    assert landing.count(SOURCE, "CURRENCY", active_only=True) == 3

    rw.execute(f"INSERT INTO CURRENCY VALUES ({', '.join('?' * len(row))})", tuple(row))
    rw.commit()
    _reconcile(source_db, pack, landing)
    revived = landing.con.execute(
        f'SELECT _d2a_deleted_at FROM "{raw_table_name(SOURCE, "CURRENCY")}" WHERE Id = 2'
    ).fetchone()
    assert revived["_d2a_deleted_at"] is None, "重现的行应复活(清软删标)"
    assert landing.count(SOURCE, "CURRENCY", active_only=True) == 4


def test_read_segment_bounded(env, pack):
    source_db, _ = env
    adapter = _adapter(source_db, pack, batch_size=20)
    info = adapter.table_info("QUOTATION")
    rows = [r for b in adapter.read_segment(info, WM, "2026-01-01 00:00:00",
                                            "2026-02-01 00:00:00") for r in b]
    assert rows, "段内应有数据"
    assert all("2026-01-01 00:00:00" <= str(r[WM]) < "2026-02-01 00:00:00" for r in rows)
