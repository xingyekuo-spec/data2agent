"""M3 阻断项回归:键唯一性、PK 迁移、持久化游标、复合键对账、回补运行键。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.base import RuntimeKeyError, encode_keyset_cursor
from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import incremental_sync
from data2agent.shared.store.landing import LandingStore, raw_table_name
from data2agent.middle.extract.reconcile import reconcile


SOURCE = "demo"


def test_duplicate_configured_key_rejected(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE ITEM (
            SURROGATE INTEGER PRIMARY KEY,
            CODE TEXT NOT NULL,
            UPDATE_TIME TEXT NOT NULL
        );
        INSERT INTO ITEM VALUES (1, 'SKU1', '2026-07-10');
        INSERT INTO ITEM VALUES (2, 'SKU1', '2026-07-11');
        """
    )
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"ITEM"})
    with pytest.raises(RuntimeKeyError, match="不唯一"):
        incremental_sync(
            adapter, landing, SOURCE,
            watermarks={"ITEM": "UPDATE_TIME"},
            key_columns={"ITEM": ["CODE"]},
        )
    assert not landing.raw_table_exists(SOURCE, "ITEM")


def test_raw_pk_migrates_after_explicit_cursor_reset(tmp_path: Path):
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
    # 先按数据库 PK 同步
    incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"ITEM"}), landing, SOURCE,
        watermarks={"ITEM": "UPDATE_TIME"},
    )
    assert landing.raw_table_primary_key(SOURCE, "ITEM") == ["SURROGATE"]
    # 直接切换业务键必须拒绝旧游标，避免跳过同水位下尚未读取的记录。
    with pytest.raises(ValueError, match="旧游标不兼容"):
        incremental_sync(
            SqliteReadOnlyAdapter(str(src), {"ITEM"}), landing, SOURCE,
            watermarks={"ITEM": "UPDATE_TIME"},
            key_columns={"ITEM": ["CODE"]},
        )
    # 运维显式确认从配置下界重跑后，才允许迁移 raw 主键。
    assert landing.reset_sync_cursor(SOURCE, "ITEM") is True
    incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"ITEM"}), landing, SOURCE,
        watermarks={"ITEM": "UPDATE_TIME"},
        key_columns={"ITEM": ["CODE"]},
    )
    assert landing.raw_table_primary_key(SOURCE, "ITEM") == ["CODE"]
    raw = raw_table_name(SOURCE, "ITEM")
    n = landing.con.execute(f'SELECT COUNT(*) FROM "{raw}"').fetchone()[0]
    assert n == 2


def test_persisted_cursor_resumes_without_rerunning_prefix(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
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
    landing = LandingStore(tmp_path / "landing.sqlite")
    keys = {"BAL": ["ITEM_ID", "WH_ID"]}
    wm = {"BAL": "UPDATE_TIME"}

    calls = {"n": 0}

    def pause_after_first_batch():
        calls["n"] += 1
        # 1=表循环入口, 2=第一批前 → 写入后落游标; 3=第二批前 → 暂停
        return calls["n"] <= 2

    report = incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"BAL"}, batch_size=2),
        landing, SOURCE, watermarks=wm, key_columns=keys,
        should_continue=pause_after_first_batch,
    )
    assert report.paused is True
    cursor = landing.get_sync_cursor(SOURCE, "BAL")
    assert cursor is not None
    cur_w, cur_k = cursor
    assert cur_k == ["A", "W2"]  # batch_size=2 后边界

    # 续传应走 resume,且最终 5 行不重复
    report2 = incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"BAL"}, batch_size=2),
        landing, SOURCE, watermarks=wm, key_columns=keys,
    )
    assert report2.tables[0].strategy == "resume"
    raw = raw_table_name(SOURCE, "BAL")
    n = landing.con.execute(f'SELECT COUNT(*) FROM "{raw}"').fetchone()[0]
    assert n == 5
    done = landing.get_sync_cursor(SOURCE, "BAL")
    assert done is not None and done[1] is None  # 完成态


def test_reconcile_soft_deletes_composite_key(tmp_path: Path):
    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE BAL (
            ITEM_ID TEXT NOT NULL,
            WH_ID TEXT NOT NULL,
            QTY REAL,
            UPDATE_TIME TEXT NOT NULL,
            PRIMARY KEY (ITEM_ID, WH_ID)
        );
        INSERT INTO BAL VALUES ('A','1',1,'2026-07-10 10:00:00');
        INSERT INTO BAL VALUES ('A','2',2,'2026-07-10 10:00:00');
        INSERT INTO BAL VALUES ('B','1',3,'2026-07-10 11:00:00');
        """
    )
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    keys = {"BAL": ["ITEM_ID", "WH_ID"]}
    wm = {"BAL": "UPDATE_TIME"}
    incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"BAL"}), landing, SOURCE,
        watermarks=wm, key_columns=keys,
    )
    rw = sqlite3.connect(src)
    rw.execute("DELETE FROM BAL WHERE ITEM_ID='A' AND WH_ID='2'")
    rw.commit()
    rw.close()
    report = reconcile(
        SqliteReadOnlyAdapter(str(src), {"BAL"}), landing, SOURCE,
        watermarks=wm, deep=True, key_columns=keys,
    )
    assert report.total_soft_deleted == 1
    raw = raw_table_name(SOURCE, "BAL")
    row = landing.con.execute(
        f'SELECT _d2a_deleted_at FROM "{raw}" WHERE ITEM_ID=? AND WH_ID=?',
        ("A", "2"),
    ).fetchone()
    assert row[0] is not None


def test_backfill_uses_configured_runtime_keys(tmp_path: Path, monkeypatch):
    """通过 CLI 路径同款逻辑验证:resolve + ensure + upsert 使用配置键。"""
    from data2agent.middle.extract.adapters.base import resolve_runtime_keys

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
        INSERT INTO ITEM VALUES (2, 'SKU2', '2026-07-12');
        """
    )
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"ITEM"})
    # 先建立 CODE 主键 raw 表
    incremental_sync(
        adapter, landing, SOURCE,
        watermarks={"ITEM": "UPDATE_TIME"},
        key_columns={"ITEM": ["CODE"]},
    )
    info = resolve_runtime_keys(
        adapter.table_info("ITEM"), ["CODE"], require_keys=True)
    adapter.validate_runtime_keys(info)
    landing.ensure_raw_table(SOURCE, info)
    assert landing.raw_table_primary_key(SOURCE, "ITEM") == ["CODE"]
    # 回补区间不应因主键不匹配失败
    n = 0
    for batch in adapter.read_segment(info, "UPDATE_TIME", "2026-07-01", "2026-07-13"):
        n += landing.upsert_rows(SOURCE, info, batch, "bf1")
    assert n == 2


def test_http_backfill_is_rejected_without_creating_middle_raw(
    tmp_path: Path, monkeypatch, capsys,
):
    """旧 backfill 是本地 upsert，HTTP 中间机必须 fail closed。"""
    from data2agent.middle.extract.__main__ import main

    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.execute(
        "CREATE TABLE ITEM (CODE TEXT PRIMARY KEY, UPDATE_TIME TEXT NOT NULL)")
    con.commit()
    con.close()
    state_db = tmp_path / "middle-state.sqlite"
    config = tmp_path / "connect.yaml"
    config.write_text(
        "deployment_mode: production\n"
        f"state_db: {state_db}\n"
        "sources:\n"
        "  demo:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    tables:\n"
        "      ITEM:\n"
        "        mode: incremental\n"
        "        watermark: UPDATE_TIME\n"
        "        key_columns: [CODE]\n"
        "    sink:\n"
        "      type: http\n"
        "      url: http://localhost:8850\n"
        "    spool:\n"
        "      policy: strict_stream\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", [
        "data2agent.middle.extract", "backfill", "--config", str(config),
        "--source", "demo", "--table", "ITEM", "--from", "2026-01-01",
        "--to", "2026-02-01",
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "禁止在中间机创建 Raw 表" in capsys.readouterr().err
    state = LandingStore(state_db)
    try:
        raw_tables = state.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw\\_%' ESCAPE '\\'"
        ).fetchall()
        assert raw_tables == []
    finally:
        state.con.close()


def test_status_views_expose_watermark_scalar_not_cursor_json(tmp_path: Path):
    """对外状态只返回水位时间,不透传含业务键的 JSON 游标。"""
    from data2agent.middle.extract.adapters.base import encode_keyset_cursor
    from data2agent.middle.admin.status import build_status
    from data2agent.shared.config import ConnectConfig

    landing = LandingStore(tmp_path / "landing.sqlite")
    raw = encode_keyset_cursor("2026-07-11 09:00:00", ["SKU-001", "W1"])
    landing.con.execute(
        "INSERT INTO d2a_sync_state "
        "(source, table_name, watermark_col, high_water, last_run_at, last_batch_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (SOURCE, "ITEM", "UPDATE_TIME", raw, "2026-07-11T09:00:00", "b1"),
    )
    landing.con.commit()

    view = landing.list_sync_watermarks(SOURCE)
    assert len(view) == 1
    assert view[0]["high_water"] == "2026-07-11 09:00:00"
    assert "SKU-001" not in str(view[0]["high_water"])
    assert not str(view[0]["high_water"]).startswith("{")

    # 内部游标仍保留键值
    cur = landing.get_sync_cursor(SOURCE, "ITEM")
    assert cur is not None
    assert cur[1] == ["SKU-001", "W1"]

    # 最近三次成功同步水位都未推进；更早一轮有推进。状态页应给出
    # 稳定的停滞证据，但仍不得透传复合游标里的业务键。
    for ordinal, (before, after) in enumerate((
        ("2026-07-10 09:00:00", "2026-07-11 09:00:00"),
        ("2026-07-11 09:00:00", "2026-07-11 09:00:00"),
        ("2026-07-11 09:00:00", "2026-07-11 09:00:00"),
        ("2026-07-11 09:00:00", "2026-07-11 09:00:00"),
    ), start=1):
        run_id = landing.start_run(SOURCE, "sync")
        landing.add_step(
            run_id, ordinal, "table", "ITEM", status="ok",
            finished_at=f"2026-07-{10 + ordinal:02d}T09:00:00",
            watermark_before=json.dumps(before),
            watermark_after=json.dumps(after),
        )
        landing.finish_run(run_id, tables=1, rows=0, status="ok")

    cfg = ConnectConfig.model_validate({
        "landing": str(tmp_path / "landing.sqlite"),
        "sources": {
            SOURCE: {
                "adapter": "sqlite_readonly",
                "path": str(tmp_path / "src.sqlite"),
                "tables": {"ITEM": {"mode": "incremental", "watermark": "UPDATE_TIME"}},
            }
        },
    })
    status = build_status(cfg)
    watermark = status["sources"][0]["watermarks"][0]
    hw = watermark["high_water"]
    assert hw == "2026-07-11 09:00:00"
    assert "SKU" not in hw
    assert watermark["value_type"] == "datetime"
    assert watermark["recent_advance"] == {
        "kind": "duration_seconds", "value": 0}
    assert watermark["unchanged_successive_runs"] == 3
    assert watermark["stalled"] is True
    assert watermark["last_advance_at"] == "2026-07-11T09:00:00"


def test_status_sync_schedule_ignores_later_reconcile_run(tmp_path: Path):
    from datetime import datetime

    from data2agent.middle.admin.status import build_status
    from data2agent.shared.config import ConnectConfig

    state_db = tmp_path / "middle-state.sqlite"
    store = LandingStore(state_db)
    sync_run = store.start_run(SOURCE, "sync")
    reconcile_run = store.start_run(SOURCE, "reconcile")
    store.con.execute(
        "UPDATE d2a_sync_run SET started_at = ? WHERE id = ?",
        ("2026-07-11T08:00:00+00:00", sync_run),
    )
    store.con.execute(
        "UPDATE d2a_sync_run SET started_at = ? WHERE id = ?",
        ("2026-07-11T10:00:00+00:00", reconcile_run),
    )
    store.con.commit()
    store.con.close()
    cfg = ConnectConfig.model_validate({
        "state_db": str(state_db),
        "sources": {
            SOURCE: {
                "adapter": "sqlite_readonly",
                "path": str(tmp_path / "erp.sqlite"),
                "sync_every": "4h",
                "tables": {"ITEM": {
                    "mode": "incremental", "watermark": "UPDATE_TIME",
                    "key_columns": ["ID"],
                }},
            },
        },
    })

    status = build_status(cfg, now=datetime.fromisoformat(
        "2026-07-11T11:00:00+00:00"))
    source = status["sources"][0]
    assert datetime.fromisoformat(source["last_run_at"]).timestamp() == (
        datetime.fromisoformat("2026-07-11T08:00:00+00:00").timestamp()
    )
    assert datetime.fromisoformat(source["next_sync_at"]).timestamp() == (
        datetime.fromisoformat("2026-07-11T12:00:00+00:00").timestamp()
    )


def test_apply_configured_keys_rejects_duplicates():
    from data2agent.middle.extract.adapters.base import (
        TableInfo, apply_configured_keys, RuntimeKeyError,
    )
    info = TableInfo(
        name="T", columns=[("CODE", "text"), ("W", "text")], pk=["CODE"],
        key_source="database_pk",
    )
    with pytest.raises(RuntimeKeyError, match="重复列"):
        apply_configured_keys(info, ["CODE", "CODE"])
