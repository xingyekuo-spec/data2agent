"""M3:配置运行键覆盖、复合键增量 keyset、错误键拒绝。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.base import (
    RuntimeKeyError,
    TableInfo,
    apply_configured_keys,
    decode_keyset_cursor,
    encode_keyset_cursor,
    resolve_runtime_keys,
)
from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import incremental_sync
from data2agent.shared.store.landing import LandingStore, raw_table_name


SOURCE = "demo"


def _make_composite_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE BAL (
            ITEM_ID TEXT NOT NULL,
            WH_ID TEXT NOT NULL,
            QTY REAL,
            UPDATE_TIME TEXT NOT NULL,
            PRIMARY KEY (ITEM_ID, WH_ID)
        );
        INSERT INTO BAL VALUES ('A','W1',1,'2026-07-10 10:00:00');
        INSERT INTO BAL VALUES ('A','W2',2,'2026-07-10 10:00:00');
        INSERT INTO BAL VALUES ('B','W1',3,'2026-07-10 10:00:00');
        INSERT INTO BAL VALUES ('B','W2',4,'2026-07-10 11:00:00');
        INSERT INTO BAL VALUES ('C','W1',5,'2026-07-11 09:00:00');
        """
    )
    con.close()


def test_encode_decode_keyset_cursor():
    raw = encode_keyset_cursor("2026-07-10", ["A", "W1"])
    w, keys = decode_keyset_cursor(raw)
    assert w == "2026-07-10"
    assert keys == ["A", "W1"]


def test_apply_configured_keys_overrides_db_pk():
    info = TableInfo(
        name="T",
        columns=[("ID", "int"), ("CODE", "text"), ("W", "text")],
        pk=["ID"],
    )
    out = apply_configured_keys(info, ["CODE"])
    assert out.pk == ["CODE"]
    assert out.key_source == "configured"
    with pytest.raises(RuntimeKeyError, match="不存在"):
        apply_configured_keys(info, ["NOPE"])


def test_composite_keyset_no_dup_no_loss(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    _make_composite_db(src)
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"BAL"}, batch_size=2)
    report = incremental_sync(
        adapter, landing, SOURCE,
        watermarks={"BAL": "UPDATE_TIME"},
        key_columns={"BAL": ["ITEM_ID", "WH_ID"]},
    )
    assert report.tables[0].rows == 5
    raw = raw_table_name(SOURCE, "BAL")
    rows = landing.con.execute(
        f'SELECT ITEM_ID, WH_ID, QTY FROM "{raw}" ORDER BY ITEM_ID, WH_ID'
    ).fetchall()
    assert len(rows) == 5
    assert {(r["ITEM_ID"], r["WH_ID"]) for r in rows} == {
        ("A", "W1"), ("A", "W2"), ("B", "W1"), ("B", "W2"), ("C", "W1"),
    }
    # 同水位多行靠复合键分页
    assert landing.get_high_water(SOURCE, "BAL") == "2026-07-11 09:00:00"


def test_configured_business_key_overrides_pk(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE ITEM (
            SURROGATE INTEGER PRIMARY KEY,
            CODE TEXT NOT NULL UNIQUE,
            UPDATE_TIME TEXT NOT NULL
        );
        INSERT INTO ITEM VALUES (1, 'SKU1', '2026-07-10');
        INSERT INTO ITEM VALUES (2, 'SKU2', '2026-07-11');
        """
    )
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"ITEM"}, batch_size=10)
    incremental_sync(
        adapter, landing, SOURCE,
        watermarks={"ITEM": "UPDATE_TIME"},
        key_columns={"ITEM": ["CODE"]},
    )
    raw = raw_table_name(SOURCE, "ITEM")
    # 落地主键应为 CODE,而非 SURROGATE
    cols = {
        r[1] for r in landing.con.execute(f'PRAGMA table_info("{raw}")').fetchall()
    }
    assert "CODE" in cols
    pk = landing.con.execute(f'PRAGMA table_info("{raw}")').fetchall()
    pk_cols = [r[1] for r in pk if r[5]]  # pk ordinal
    assert pk_cols == ["CODE"]
    # 审计记录键来源,不含键值
    audits = landing.con.execute(
        "SELECT sql FROM d2a_audit_log WHERE action = 'runtime_keys'"
    ).fetchall()
    assert audits
    assert "configured" in audits[0]["sql"]
    assert "SKU1" not in audits[0]["sql"]


def test_bad_key_fails_before_sync(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE T (ID TEXT PRIMARY KEY, W TEXT);
        INSERT INTO T VALUES ('1', '2026-07-10');
        """
    )
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"T"})
    with pytest.raises(RuntimeKeyError, match="不存在"):
        incremental_sync(
            adapter, landing, SOURCE,
            watermarks={"T": "W"},
            key_columns={"T": ["NOPE"]},
        )


def test_null_runtime_key_fails(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE T (
            A TEXT,
            B TEXT,
            W TEXT NOT NULL
        );
        INSERT INTO T VALUES ('x', NULL, '2026-07-10');
        """
    )
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"T"})
    with pytest.raises(RuntimeKeyError, match="NULL"):
        incremental_sync(
            adapter, landing, SOURCE,
            watermarks={"T": "W"},
            key_columns={"T": ["A", "B"]},
        )


def test_increment_resume_after_lookback(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    _make_composite_db(src)
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"BAL"}, batch_size=2)
    keys = {"BAL": ["ITEM_ID", "WH_ID"]}
    wm = {"BAL": "UPDATE_TIME"}
    incremental_sync(adapter, landing, SOURCE, watermarks=wm, key_columns=keys)
    # 追加同水位新行 + 更高水位
    con = sqlite3.connect(src)
    con.execute(
        "INSERT INTO BAL VALUES ('D','W1',9,'2026-07-11 09:00:00')")
    con.execute(
        "INSERT INTO BAL VALUES ('E','W1',8,'2026-07-12 00:00:00')")
    con.commit()
    con.close()
    report = incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"BAL"}, batch_size=2),
        landing, SOURCE, watermarks=wm, key_columns=keys, lookback_days=3,
    )
    assert report.tables[0].strategy == "increment"
    raw = raw_table_name(SOURCE, "BAL")
    n = landing.con.execute(f'SELECT COUNT(*) FROM "{raw}"').fetchone()[0]
    assert n == 7
    assert landing.get_high_water(SOURCE, "BAL") == "2026-07-12 00:00:00"


def test_resolve_runtime_keys_requires_keys_for_incremental():
    info = TableInfo(name="X", columns=[("A", "text")], pk=[])
    with pytest.raises(RuntimeKeyError):
        resolve_runtime_keys(info, None, require_keys=True)


def test_watermark_change_rejects_old_cursor(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.execute(
        "CREATE TABLE T (ID INTEGER PRIMARY KEY, OLD_W TEXT, NEW_W TEXT)")
    con.execute(
        "INSERT INTO T VALUES (1, '2099-01-01', '2026-07-01')")
    con.commit()
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    landing.set_sync_cursor(
        SOURCE, "T", "OLD_W", "2099-01-01", None, "old",
        key_columns=["ID"], schema="main")
    with pytest.raises(ValueError, match="旧游标不兼容"):
        incremental_sync(
            SqliteReadOnlyAdapter(str(src), {"T"}), landing, SOURCE,
            watermarks={"T": "NEW_W"}, key_columns={"T": ["ID"]})
    row = landing.con.execute(
        "SELECT watermark_col FROM d2a_sync_state "
        "WHERE source = ? AND table_name = ?", (SOURCE, "T")).fetchone()
    assert row["watermark_col"] == "OLD_W"
