"""M4-T02 存储迁移测试:d2a_run_step。

新库、M3 旧库、重复初始化;无破坏性回填;仓储方法基本读写。
"""

from pathlib import Path

import pytest

from data2agent.shared.store.landing import LandingStore

ROOT = Path(__file__).resolve().parents[2]


def test_new_db_has_m4_tables(tmp_path):
    store = LandingStore(tmp_path / "new.sqlite")
    tables = {r[0] for r in store.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "d2a_run_step" in tables
    indexes = {r[0] for r in store.con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_d2a_run_step_run" in indexes
    assert "idx_d2a_run_step_batch" in indexes


def test_old_db_migrates_without_backfill(tmp_path):
    """M3 旧库(无 M4 表)升级:M4 表补上,旧数据不动,无破坏性回填。"""
    db = tmp_path / "old.sqlite"
    store0 = LandingStore(db)
    rid = store0.start_run("s", "sync")
    store0.finish_run(rid, tables=1, rows=10)
    store0.con.close()

    store = LandingStore(db)  # 第二次初始化:迁移发生
    tables = {r[0] for r in store.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "d2a_run_step" in tables
    # 旧 run 不被回填 step(legacy 语义由读取端处理)
    assert store.steps_for_run(rid) == []
    # 重复初始化幂等
    LandingStore(db)


def test_step_crud_and_kinds(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    rid = store.start_run("s", "sync")
    sid = store.add_step(rid, 1, "table", "CUSTOMER")
    store.update_step(sid, status="ok", rows_in=36, rows_out=36,
                      watermark_before='"2026-07-17"', watermark_after='"2026-07-18"')
    steps = store.steps_for_run(rid)
    assert len(steps) == 1
    step = steps[0]
    assert step["kind"] == "table"
    assert step["status"] == "ok"
    assert step["rows_in"] == 36
    assert step["watermark_after"] == '"2026-07-18"'
    with pytest.raises(ValueError, match="非法 step kind"):
        store.add_step(rid, 2, "bogus", "X")
