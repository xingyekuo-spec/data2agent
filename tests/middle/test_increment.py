"""抽取框架 E2 测试:水位增量、回看、keyset 分页、水位状态机。"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import (
    incremental_sync,
    subtract_lookback,
)
from data2agent.shared.store.landing import LandingStore, raw_table_name
from tests.helpers import watermarks_from_pack, whitelist_from_pack
from data2agent.shared.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"
WM = "LAST_MODIFIED_DATE"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def source_db(tmp_path) -> Path:
    db = tmp_path / "source.sqlite"
    write_db(db, build(seed=42, asof=date(2026, 7, 10)))
    return db


@pytest.fixture()
def landing(tmp_path) -> LandingStore:
    return LandingStore(tmp_path / "landing.sqlite")


def _adapter(source_db, pack, **kw):
    return SqliteReadOnlyAdapter(str(source_db), whitelist_from_pack(pack, SOURCE), **kw)


def _sync(source_db, pack, landing, **kw):
    return incremental_sync(_adapter(source_db, pack, **kw.pop("adapter_kw", {})),
                            landing, SOURCE, watermarks_from_pack(pack, SOURCE), **kw)


def test_watermarks_from_pack(pack):
    wms = watermarks_from_pack(pack, SOURCE)
    assert wms == {t: WM for t in
                   ["CUSTOMER", "ITEM", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]}
    assert "CURRENCY" not in wms, "未声明水位的维表应走 full_refresh"


def test_sync_records_expected_rows_for_progress(pack, source_db, landing):
    """同步前预估行数写入步骤:增量表按水位口径,full_refresh 表为整表行数。"""
    report = _sync(source_db, pack, landing, run_id=landing.start_run(SOURCE, "sync"))
    assert report.total_rows > 0
    steps = landing.steps_for_run(report.run_id)
    assert steps, "应记录表级步骤"
    src = sqlite3.connect(source_db)
    try:
        for s in steps:
            assert s["expected_rows"] is not None, f"{s['target']} 缺预估行数"
            wm_col = {"CUSTOMER", "ITEM", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"}
            if s["target"] in wm_col:
                # 首轮增量 = 全表扫描,预估 = 整表行数
                (want,) = src.execute(
                    f'SELECT COUNT(*) FROM "{s["target"]}"').fetchone()
            else:
                (want,) = src.execute(
                    f'SELECT COUNT(*) FROM "{s["target"]}"').fetchone()
            assert s["expected_rows"] == want
            assert s["rows_out"] == s["expected_rows"], "完成后进度应为 100%"
    finally:
        src.close()


def test_count_for_sync_increment_filter(pack, source_db):
    """增量预估带水位过滤:COUNT(WHERE wm >= since) 与读取同口径。"""
    adapter = _adapter(source_db, pack)
    info = adapter.table_info("CUSTOMER")
    total = adapter.count_for_sync(info, WM)
    (all_rows,) = sqlite3.connect(source_db).execute(
        'SELECT COUNT(*) FROM "CUSTOMER"').fetchone()
    assert total == all_rows
    since = "2026-07-05"
    est = adapter.count_for_sync(info, WM, since)
    (want,) = sqlite3.connect(source_db).execute(
        'SELECT COUNT(*) FROM "CUSTOMER" WHERE LAST_MODIFIED_DATE >= ?',
        (since,)).fetchone()
    assert est == want


def test_subtract_lookback():
    assert subtract_lookback("2026-07-10 08:30:00", 3) == "2026-07-07 08:30:00"
    assert subtract_lookback("2026-07-10", 3) == "2026-07-07"
    with pytest.raises(ValueError, match="无法解析水位值"):
        subtract_lookback("下周三", 3)


def test_initial_run_establishes_watermark(source_db, pack, landing):
    report = _sync(source_db, pack, landing)
    by_table = {t.table: t for t in report.tables}
    assert by_table["CURRENCY"].strategy == "full_refresh"
    assert by_table["SALES_ORDER"].strategy == "initial"
    src = sqlite3.connect(source_db)
    (expected,) = src.execute(f"SELECT MAX({WM}) FROM SALES_ORDER").fetchone()
    assert landing.get_high_water(SOURCE, "SALES_ORDER") == expected
    assert landing.get_high_water(SOURCE, "CURRENCY") is None


def test_second_run_pulls_only_lookback_window(source_db, pack, landing):
    _sync(source_db, pack, landing)
    total_orders = landing.count(SOURCE, "SALES_ORDER")
    report = _sync(source_db, pack, landing, lookback_days=3)
    orders = next(t for t in report.tables if t.table == "SALES_ORDER")
    assert orders.strategy == "increment"
    assert 0 < orders.rows < total_orders, "第二轮只应重拉回看窗口内的行"
    assert landing.count(SOURCE, "SALES_ORDER") == total_orders, "幂等:落地行数不变"


def test_incremental_picks_up_source_change(source_db, pack, landing):
    _sync(source_db, pack, landing)
    old_water = landing.get_high_water(SOURCE, "CUSTOMER")
    rw = sqlite3.connect(source_db)
    rw.execute(f"UPDATE CUSTOMER SET CUSTOMER_NAME = '增量改名', {WM} = '2026-07-11 09:00:00' "
               "WHERE Id = 5")
    rw.commit()
    _sync(source_db, pack, landing)
    row = landing.con.execute(
        f'SELECT CUSTOMER_NAME FROM "{raw_table_name(SOURCE, "CUSTOMER")}" WHERE Id = 5'
    ).fetchone()
    assert row["CUSTOMER_NAME"] == "增量改名"
    new_water = landing.get_high_water(SOURCE, "CUSTOMER")
    assert new_water == "2026-07-11 09:00:00" and new_water > old_water


def test_watermark_never_retreats(landing):
    landing.set_high_water(SOURCE, "T", WM, "2026-07-10 00:00:00", "b1")
    landing.set_high_water(SOURCE, "T", WM, "2026-07-01 00:00:00", "b2")  # 更旧
    landing.set_high_water(SOURCE, "T", WM, None, "b3")                    # 空轮
    assert landing.get_high_water(SOURCE, "T") == "2026-07-10 00:00:00"


def test_failure_does_not_advance_watermark(source_db, pack, landing, monkeypatch):
    _sync(source_db, pack, landing)
    before = landing.get_high_water(SOURCE, "SALES_ORDER")
    rw = sqlite3.connect(source_db)
    rw.execute(f"UPDATE SALES_ORDER SET {WM} = '2026-07-12 08:00:00' WHERE Id = 1")
    rw.commit()

    real = LandingStore.upsert_rows

    def boom(self, source, info, rows, batch_id):
        if info.name == "SALES_ORDER":
            raise RuntimeError("模拟落地失败")
        return real(self, source, info, rows, batch_id)

    monkeypatch.setattr(LandingStore, "upsert_rows", boom)
    with pytest.raises(RuntimeError):
        _sync(source_db, pack, landing)
    assert landing.get_high_water(SOURCE, "SALES_ORDER") == before, "失败批次不得推进水位"
    run = landing.con.execute("SELECT status FROM d2a_sync_run ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "failed"

    monkeypatch.setattr(LandingStore, "upsert_rows", real)
    _sync(source_db, pack, landing)  # 恢复后重跑,水位补上
    assert landing.get_high_water(SOURCE, "SALES_ORDER") == "2026-07-12 08:00:00"


def test_keyset_handles_watermark_ties(source_db, pack, landing):
    """多行同水位值时,(水位, 主键) keyset 不丢行不重行。"""
    rw = sqlite3.connect(source_db)
    rw.execute(f"UPDATE QUOTATION SET {WM} = '2026-06-01 12:00:00' WHERE Id <= 60")
    rw.commit()
    _sync(source_db, pack, landing, adapter_kw={"batch_size": 25})  # 平局组(60)> 批大小(25)
    src_ids = {r[0] for r in rw.execute("SELECT Id FROM QUOTATION")}
    landed = {r[0] for r in landing.con.execute(
        f'SELECT Id FROM "{raw_table_name(SOURCE, "QUOTATION")}"')}
    assert landed == src_ids
    (n,) = landing.con.execute(
        f'SELECT COUNT(*) FROM "{raw_table_name(SOURCE, "QUOTATION")}"').fetchone()
    assert n == len(src_ids), "keyset 分页不得产生重复行"
