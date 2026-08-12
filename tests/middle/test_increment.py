"""抽取框架 E2 测试:水位增量、回看、keyset 分页、水位状态机。"""

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import (
    cleanup_orphan_spools,
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


def test_sync_only_tables_limits_scope(pack, source_db, landing):
    """only_tables:定向重试只同步指定表,其余表不动。"""
    report = _sync(source_db, pack, landing,
                   run_id=landing.start_run(SOURCE, "sync"),
                   only_tables={"CUSTOMER"})
    assert [t.table for t in report.tables] == ["CUSTOMER"]
    steps = landing.steps_for_run(report.run_id)
    assert [s["target"] for s in steps] == ["CUSTOMER"]


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


def test_initial_sync_respects_start_date(pack, source_db, landing):
    """首轮同步带 start_date:只抽起始日期之后的行,不抽历史全量。"""
    src = sqlite3.connect(source_db)
    (total, _min_wm, _max_wm) = src.execute(
        'SELECT COUNT(*), MIN(LAST_MODIFIED_DATE), MAX(LAST_MODIFIED_DATE) '
        'FROM "CUSTOMER"').fetchone()
    # 取中位偏后的起始日期,确保过滤生效
    (start,) = src.execute(
        'SELECT LAST_MODIFIED_DATE FROM "CUSTOMER" '
        'ORDER BY LAST_MODIFIED_DATE LIMIT 1 OFFSET ?', (total // 2,)).fetchone()
    (want_rows,) = src.execute(
        'SELECT COUNT(*) FROM "CUSTOMER" WHERE LAST_MODIFIED_DATE >= ?',
        (start,)).fetchone()
    src.close()
    assert 0 < want_rows < total

    report = _sync(source_db, pack, landing,
                   run_id=landing.start_run(SOURCE, "sync"),
                   only_tables={"CUSTOMER"},
                   start_dates={"CUSTOMER": start})
    assert report.tables[0].rows == want_rows, "首轮应只抽起始日期之后的行"
    # 预估行数同口径
    steps = landing.steps_for_run(report.run_id)
    assert steps[0]["expected_rows"] == want_rows
    # 落地行均不早于起始日期
    rows = landing.con.execute(
        f'SELECT MIN(LAST_MODIFIED_DATE) FROM "{raw_table_name(SOURCE, "CUSTOMER")}"').fetchone()
    assert rows[0] >= start
    # 水位已建立:第二轮走增量,不会重扫全量
    cur = landing.get_sync_cursor(SOURCE, "CUSTOMER")
    assert cur is not None and cur[0] is not None


def test_cli_sync_one_shot_respects_start_date(pack, source_db, tmp_path, monkeypatch):
    """回归:一次性 sync 命令也须生效 start_date 下界(曾只有 serve 调度路径传)。"""
    import sys

    from data2agent.middle.extract.__main__ import main

    src = sqlite3.connect(source_db)
    (total,) = src.execute('SELECT COUNT(*) FROM "CUSTOMER"').fetchone()
    (start,) = src.execute(
        'SELECT LAST_MODIFIED_DATE FROM "CUSTOMER" '
        'ORDER BY LAST_MODIFIED_DATE LIMIT 1 OFFSET ?', (total // 2,)).fetchone()
    start_day = start[:10]
    (want,) = src.execute(
        'SELECT COUNT(*) FROM "CUSTOMER" WHERE LAST_MODIFIED_DATE >= ?',
        (start_day,)).fetchone()
    src.close()
    assert 0 < want < total

    landing_db = tmp_path / "cli-landing.sqlite"
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing_db}\n"
        "sources:\n"
        f"  {SOURCE}:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {source_db}\n"
        "    sync_every: 30m\n"
        f'    start_date: "{start_day}"\n'
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: LAST_MODIFIED_DATE\n"
        "    sink:\n"
        "      type: local\n",
        encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["extract", "sync", "--config", str(cfg)])
    assert main() == 0
    con = sqlite3.connect(landing_db)
    n, min_wm = con.execute(
        f'SELECT COUNT(*), MIN(LAST_MODIFIED_DATE) '
        f'FROM "{raw_table_name(SOURCE, "CUSTOMER")}"').fetchone()
    con.close()
    assert n == want, "CLI 一次性 sync 首轮应只抽 start_date 之后的行"
    assert min_wm >= start_day


def test_nullable_watermark_fails_before_silently_skipping_rows(tmp_path):
    src = tmp_path / "nullable.sqlite"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE T (ID INTEGER PRIMARY KEY, WM TEXT, V TEXT)")
    con.executemany(
        "INSERT INTO T VALUES (?, ?, ?)",
        [(1, "2026-01-01 00:00:00", "ok"), (2, None, "would-be-lost")])
    con.commit()
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    with pytest.raises(ValueError, match="水位列.*NULL"):
        incremental_sync(
            SqliteReadOnlyAdapter(str(src), {"T"}), landing, "demo",
            watermarks={"T": "WM"})
    assert not landing.raw_table_exists("demo", "T")


class _RecordingRemoteSink:
    def __init__(self, spool_dir: Path | None = None, *, fail_write: bool = False):
        self.spool_dir = spool_dir
        self.fail_write = fail_write
        self.rows = 0
        self.observed_modes: list[int] = []

    def begin_sync(self, _source, _tables, _run_id):
        return None

    def complete_sync(self, _source):
        return None

    def abort_sync(self, _source):
        return None

    def begin_table(self, _source, _info, **_kwargs):
        return None

    def write(self, _source, _info, rows, _batch_id, **_kwargs):
        if self.spool_dir:
            files = list(self.spool_dir.glob("*.spool"))
            assert len(files) == 1
            self.observed_modes.append(files[0].stat().st_mode & 0o777)
        if self.fail_write:
            raise RuntimeError("simulated push failure")
        self.rows += len(rows)
        return len(rows)

    def complete_table(self, *_args, **_kwargs):
        return None

    def abort_table(self, *_args, **_kwargs):
        return None


def _full_refresh_source(tmp_path: Path) -> Path:
    source = tmp_path / "full.sqlite"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE T (ID INTEGER PRIMARY KEY, VALUE TEXT)")
    con.executemany("INSERT INTO T VALUES (?, ?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()
    return source


def test_strict_stream_full_refresh_never_creates_spool(tmp_path, monkeypatch):
    source = _full_refresh_source(tmp_path)
    state = LandingStore(tmp_path / "middle-state.sqlite")
    sink = _RecordingRemoteSink()

    def forbidden_tempfile(*_args, **_kwargs):
        raise AssertionError("strict_stream 不得创建磁盘 spool")

    monkeypatch.setattr(
        "data2agent.middle.extract.increment.tempfile.NamedTemporaryFile",
        forbidden_tempfile,
    )
    report = incremental_sync(
        SqliteReadOnlyAdapter(str(source), {"T"}), state, "demo",
        watermarks={}, sink=sink, full_refresh_spool_policy="strict_stream",
    )
    assert report.total_rows == 2 and sink.rows == 2
    assert not state.raw_table_exists("demo", "T")


@pytest.mark.parametrize("fail_write", [False, True])
def test_encrypted_volume_spool_has_minimum_permissions_and_is_cleaned(
    tmp_path, fail_write,
):
    source = _full_refresh_source(tmp_path)
    state = LandingStore(tmp_path / "middle-state.sqlite")
    spool_dir = tmp_path / "encrypted-volume"
    sink = _RecordingRemoteSink(spool_dir, fail_write=fail_write)
    if fail_write:
        with pytest.raises(RuntimeError, match="simulated push failure"):
            incremental_sync(
                SqliteReadOnlyAdapter(str(source), {"T"}), state, "demo",
                watermarks={}, sink=sink,
                full_refresh_spool_policy="encrypted_temp_volume",
                spool_directory=str(spool_dir),
            )
    else:
        incremental_sync(
            SqliteReadOnlyAdapter(str(source), {"T"}), state, "demo",
            watermarks={}, sink=sink,
            full_refresh_spool_policy="encrypted_temp_volume",
            spool_directory=str(spool_dir),
        )
    assert spool_dir.stat().st_mode & 0o777 == 0o700
    assert sink.observed_modes == [0o600]
    assert list(spool_dir.glob("*.spool")) == []
    assert not state.raw_table_exists("demo", "T")


def test_orphan_spool_cleanup_is_scoped_to_source(tmp_path):
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    demo_prefix = hashlib.sha256(b"demo").hexdigest()[:12]
    other_prefix = hashlib.sha256(b"other").hexdigest()[:12]
    demo = spool_dir / f"d2a-full-{demo_prefix}-orphan.spool"
    other = spool_dir / f"d2a-full-{other_prefix}-live.spool"
    demo.write_bytes(b"raw")
    other.write_bytes(b"raw")
    assert cleanup_orphan_spools(spool_dir, "demo") == 1
    assert not demo.exists()
    assert other.exists()
