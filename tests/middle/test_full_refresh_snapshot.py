"""M4 全量快照:staging、原子发布、失败回滚、重放、无主键、删除行消失。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.base import TableInfo
from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import incremental_sync
from data2agent.shared.store.landing import LandingStore, raw_table_name

SOURCE = "demo"


def _src_currency(tmp_path: Path, rows: list[tuple]) -> Path:
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE CURRENCY (CODE TEXT PRIMARY KEY, NAME TEXT)")
    con.executemany("INSERT INTO CURRENCY VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return src


def test_snapshot_removes_deleted_source_rows(tmp_path: Path):
    src = _src_currency(tmp_path, [("USD", "美元"), ("EUR", "欧元"), ("JPY", "日元")])
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"CURRENCY"})
    incremental_sync(adapter, landing, SOURCE, watermarks={})
    assert landing.count(SOURCE, "CURRENCY") == 3

    rw = sqlite3.connect(src)
    rw.execute("DELETE FROM CURRENCY WHERE CODE = 'EUR'")
    rw.commit()
    rw.close()

    incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"CURRENCY"}), landing, SOURCE, watermarks={})
    assert landing.count(SOURCE, "CURRENCY") == 2
    codes = {
        r[0] for r in landing.con.execute(
            f'SELECT CODE FROM "{raw_table_name(SOURCE, "CURRENCY")}"')
    }
    assert codes == {"USD", "JPY"}


def test_snapshot_failure_keeps_previous_raw(tmp_path: Path):
    src = _src_currency(tmp_path, [("USD", "美元"), ("EUR", "欧元")])
    landing = LandingStore(tmp_path / "landing.sqlite")
    incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"CURRENCY"}), landing, SOURCE, watermarks={})
    assert landing.count(SOURCE, "CURRENCY") == 2

    info = TableInfo(
        name="CURRENCY", columns=[("CODE", "text"), ("NAME", "text")], pk=["CODE"])
    begun = landing.begin_snapshot(SOURCE, info, "snap-fail")
    assert begun["status"] == "open"
    landing.write_snapshot_batch(
        SOURCE, info, "snap-fail", "b1",
        [{"CODE": "CNY", "NAME": "人民币"}])
    # 故意用错误行数完成 → 失败
    with pytest.raises(ValueError, match="行数不符"):
        landing.complete_snapshot(SOURCE, info, "snap-fail", expected_rows=99, expected_batches=1)
    # 当前 raw 仍是上一完整快照
    assert landing.count(SOURCE, "CURRENCY") == 2
    codes = {
        r[0] for r in landing.con.execute(
            f'SELECT CODE FROM "{raw_table_name(SOURCE, "CURRENCY")}"')
    }
    assert codes == {"USD", "EUR"}
    landing.abort_snapshot(SOURCE, "CURRENCY", "snap-fail")
    staging = begun["staging_table"]
    assert landing.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (staging,)
    ).fetchone() is None


def test_snapshot_batch_replay_dedupes(tmp_path: Path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    info = TableInfo(
        name="CURRENCY", columns=[("CODE", "text"), ("NAME", "text")], pk=["CODE"])
    landing.begin_snapshot(SOURCE, info, "snap1")
    rows = [{"CODE": "USD", "NAME": "美元"}, {"CODE": "EUR", "NAME": "欧元"}]
    r1 = landing.write_snapshot_batch(SOURCE, info, "snap1", "b1", rows)
    r2 = landing.write_snapshot_batch(SOURCE, info, "snap1", "b1", rows)
    assert r1["ingested"] == 2 and r1["duplicate"] is False
    assert r2["ingested"] == 2 and r2["duplicate"] is True
    landing.complete_snapshot(SOURCE, info, "snap1", 2, 1)
    # 完成事件重放幂等
    again = landing.complete_snapshot(SOURCE, info, "snap1", 2, 1)
    assert again["duplicate"] is True
    assert landing.count(SOURCE, "CURRENCY") == 2


def test_zero_row_snapshot_publishes_empty_table(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE EMPTY_T (ID INTEGER PRIMARY KEY, V TEXT)")
    con.commit()
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    # 先放一行旧数据
    info = TableInfo(name="EMPTY_T", columns=[("ID", "int"), ("V", "text")], pk=["ID"])
    landing.begin_snapshot(SOURCE, info, "old")
    landing.write_snapshot_batch(
        SOURCE, info, "old", "b0", [{"ID": 1, "V": "gone"}])
    landing.complete_snapshot(SOURCE, info, "old", 1, 1)
    assert landing.count(SOURCE, "EMPTY_T") == 1

    report = incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"EMPTY_T"}), landing, SOURCE, watermarks={})
    assert report.tables[0].rows == 0
    assert landing.raw_table_exists(SOURCE, "EMPTY_T")
    assert landing.count(SOURCE, "EMPTY_T") == 0


def test_no_pk_full_refresh_snapshot(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE LOG (MSG TEXT, TS TEXT);
        INSERT INTO LOG VALUES ('a', '1');
        INSERT INTO LOG VALUES ('b', '2');
        INSERT INTO LOG VALUES ('a', '1');
        """
    )
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    report = incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"LOG"}), landing, SOURCE, watermarks={})
    assert report.tables[0].rows == 3
    assert landing.count(SOURCE, "LOG") == 3
    assert landing.raw_table_primary_key(SOURCE, "LOG") is None

    rw = sqlite3.connect(src)
    rw.execute("DELETE FROM LOG WHERE MSG = 'b'")
    rw.commit()
    rw.close()
    incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"LOG"}), landing, SOURCE, watermarks={})
    assert landing.count(SOURCE, "LOG") == 2


def test_keyset_full_refresh_does_not_skip_after_low_key_delete(tmp_path: Path):
    src = _src_currency(
        tmp_path,
        [("A", "a"), ("B", "b"), ("C", "c"), ("D", "d")])

    class MutatingAdapter(SqliteReadOnlyAdapter):
        mutated = False

        def _execute(self, sql, params=()):
            if " WHERE ((" in sql and not self.mutated:
                self.mutated = True
                rw = sqlite3.connect(src)
                rw.execute("DELETE FROM CURRENCY WHERE CODE = 'A'")
                rw.commit()
                rw.close()
            return super()._execute(sql, params)

    landing = LandingStore(tmp_path / "landing.sqlite")
    incremental_sync(
        MutatingAdapter(str(src), {"CURRENCY"}, batch_size=2),
        landing, SOURCE)
    raw = raw_table_name(SOURCE, "CURRENCY")
    codes = [
        r[0] for r in landing.con.execute(
            f'SELECT CODE FROM "{raw}" ORDER BY CODE')
    ]
    # A 属于首条 SELECT 的语句级快照；关键是后续现存 C 不得被 OFFSET 跳过。
    assert codes == ["A", "B", "C", "D"]


def test_incremental_schema_drop_fails_closed(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE T (ID INTEGER PRIMARY KEY, V TEXT, W TEXT)")
    con.execute("INSERT INTO T VALUES (1, 'old', '2026-07-01')")
    con.commit()
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"T"}), landing, SOURCE,
        watermarks={"T": "W"})
    con = sqlite3.connect(src)
    con.execute("ALTER TABLE T DROP COLUMN V")
    con.execute("UPDATE T SET W = '2026-07-02'")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="结构与既有 raw 不兼容"):
        incremental_sync(
            SqliteReadOnlyAdapter(str(src), {"T"}), landing, SOURCE,
            watermarks={"T": "W"})


def test_raw_physical_schema_identity_requires_full_refresh(tmp_path: Path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    dbo = TableInfo(
        "T", [("ID", "int"), ("V", "text")], ["ID"], schema="dbo")
    archive = TableInfo(
        "T", [("ID", "int"), ("V", "text")], ["ID"], schema="archive")
    landing.ensure_raw_table(SOURCE, dbo)
    landing.upsert_rows(
        SOURCE, dbo, [{"ID": 1, "V": "old"}], "b1")

    with pytest.raises(ValueError, match="跨物理表增量混写"):
        landing.ensure_raw_table(SOURCE, archive)

    landing.begin_snapshot(SOURCE, archive, "schema-switch")
    landing.write_snapshot_batch(
        SOURCE, archive, "schema-switch", "b2",
        [{"ID": 2, "V": "new"}])
    landing.complete_snapshot(
        SOURCE, archive, "schema-switch", 1, 1)
    landing.ensure_raw_table(SOURCE, archive)
    assert landing.con.execute(
        "SELECT schema_name FROM d2a_raw_table_identity "
        "WHERE source = ? AND table_name = 'T'", (SOURCE,)
    ).fetchone()["schema_name"] == "archive"
