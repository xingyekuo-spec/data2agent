"""d2a_sync_run.run_type 兼容迁移与结构化运行类型测试(M3-T02)。"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_apply import apply_objects
from data2agent.connect.reconcile import reconcile
from tests.helpers import whitelist_from_pack
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"

_OLD_RUN_DDL = """
CREATE TABLE d2a_sync_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    tables INTEGER, rows INTEGER, status TEXT, detail TEXT
);
"""


def _run_types(store: LandingStore) -> list[str | None]:
    return [r[0] for r in store.con.execute(
        "SELECT run_type FROM d2a_sync_run ORDER BY id")]


def test_old_db_migrates_and_keeps_null(tmp_path):
    """旧库(无 run_type 列)升级:列补上,旧记录保持 NULL,不回填猜测。"""
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(_OLD_RUN_DDL)
    con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, status) "
        "VALUES ('s', '2026-01-01 00:00:00', 'ok')")
    con.commit()
    con.close()

    store = LandingStore(db)
    cols = {r[1] for r in store.con.execute("PRAGMA table_info(d2a_sync_run)")}
    assert "run_type" in cols
    assert _run_types(store) == [None]

    # 重复初始化幂等,不报错、不改动旧记录
    store2 = LandingStore(db)
    assert _run_types(store2) == [None]


def test_start_run_writes_type_and_validates(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    rid = store.start_run(SOURCE, "sync")
    assert store.con.execute(
        "SELECT run_type FROM d2a_sync_run WHERE id = ?", (rid,)).fetchone()[0] == "sync"
    validation_id = store.start_run(SOURCE, "validation")
    assert store.con.execute(
        "SELECT run_type FROM d2a_sync_run WHERE id = ?", (validation_id,)
    ).fetchone()[0] == "validation"
    with pytest.raises(ValueError, match="未知 run_type"):
        store.start_run(SOURCE, "bogus")


def test_three_flows_write_correct_run_type(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE), audit_hook=hook)

    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    apply_objects(landing, pack, SOURCE)
    reconcile(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))

    assert _run_types(landing) == ["sync", "apply", "reconcile"]
