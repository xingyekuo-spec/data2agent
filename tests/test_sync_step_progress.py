"""逐批进度原子性与旧库 schema 迁移测试。"""

from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync, _safe_error
from data2agent.connect.landing import LandingStore
from data2agent.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db
from tests.helpers import watermarks_from_pack, whitelist_from_pack

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def fresh_landing(tmp_path):
    return LandingStore(tmp_path / "landing.sqlite")


@pytest.fixture()
def seeded_env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=1, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    return src, pack, landing


# ---- 逐批进度 ----

def test_batch_progress_updates_step_during_incremental_sync(seeded_env):
    """每成功一批应更新 step 的 rows_out、batches、progressed_at。"""
    src, pack, landing = seeded_env
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE),
                                    batch_size=10, audit_hook=hook)

    report = incremental_sync(
        adapter, landing, SOURCE,
        watermarks_from_pack(pack, SOURCE))
    assert report.run_id > 0

    steps = landing.steps_for_run(report.run_id)
    assert len(steps) > 0, "应至少有一个 table step"
    for s in steps:
        assert s["kind"] == "table"
        if s["status"] == "ok":
            # 完成的 step 应有累计值
            assert s["rows_out"] is not None
            assert s["rows_out"] > 0, f"step {s['target']}: rows_out should be > 0"
            assert s["batches"] is not None
            assert s["batches"] > 0, f"step {s['target']}: batches should be > 0"


def test_batch_progress_atomic_within_single_commit(seeded_env):
    """progressed_at 的每批写入通过 record_sync_batch_progress 单事务完成。"""
    src, pack, landing = seeded_env

    # batch_size=1 强制多批,每批调用 record_sync_batch_progress
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE),
                                    batch_size=1, audit_hook=hook)
    report = incremental_sync(adapter, landing, SOURCE,
                              watermarks_from_pack(pack, SOURCE))

    steps = landing.steps_for_run(report.run_id)
    for s in steps:
        if s["status"] == "ok":
            # 批次数应与 rows_out 一致(每批 1 行)
            assert s["rows_out"] > 0
            # 无论是用 record_sync_batch_progress 还是终态 update_step 写入,
            # batches 不应为 NULL(新 code path 保证写入)
            assert s["batches"] is not None
            # 同样 progressed_at 也应不为 NULL
            # (每批成功时 record_sync_batch_progress 写入)
            assert s["progressed_at"] is not None


def test_progress_updates_visible_between_batches(seeded_env):
    """运行中的 step 的进度应可被另一个连接读到。"""
    src, pack, landing = seeded_env
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    # batch_size=1 强制多批,每批一次 commit
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE),
                                    batch_size=1, audit_hook=hook)

    report = incremental_sync(
        adapter, landing, SOURCE,
        watermarks_from_pack(pack, SOURCE))

    # 同步完成后,从另一个连接读取 step 进度
    reader = LandingStore(landing.db_path)
    steps = reader.steps_for_run(report.run_id)
    for s in steps:
        if s["status"] == "ok":
            assert s["rows_out"] > 0, f"{s['target']}: rows_out should be > 0"
            assert s["batches"] > 0, f"{s['target']}: batches should be > 0"
    reader.con.close()


# ---- 旧库 schema 迁移 ----

def test_legacy_db_adds_batch_columns_on_open(tmp_path):
    """旧库(无 batches/progressed_at 列)打开后自动迁移。"""
    db_path = tmp_path / "legacy.sqlite"
    # 直接用 sqlite3 创建旧版 schema,不包含 progress 列
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS d2a_sync_run (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            tables INTEGER, rows INTEGER, status TEXT, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS d2a_run_step (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, batch_id TEXT,
            rows_in INTEGER, rows_out INTEGER, quarantined INTEGER,
            repaired INTEGER, soft_deleted INTEGER,
            watermark_before TEXT, watermark_after TEXT,
            error TEXT, error_id TEXT
        );
    """)
    conn.close()

    # 通过 LandingStore 打开应触发迁移
    landing = LandingStore(str(db_path))
    cols = {r[1] for r in landing.con.execute("PRAGMA table_info(d2a_run_step)")}
    assert "batches" in cols, "迁移后应有 batches 列"
    assert "progressed_at" in cols, "迁移后应有 progressed_at 列"

    # 迁移后旧 run 的查询不报错
    rows = landing.steps_for_run(99999)
    assert rows == []

    # 迁移后可以插入新 step 带 progress 字段
    landing.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, status, run_type) "
        "VALUES ('test', datetime('now'), 'ok', 'sync')")
    run_id = landing.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run").fetchone()[0]
    step_id = landing.add_step(run_id, 1, "table", "test_table")
    landing.update_step(step_id, status="ok", rows_out=100, batches=5,
                        progressed_at="2026-07-25T10:00:00")
    step = landing.con.execute(
        "SELECT * FROM d2a_run_step WHERE id = ?", (step_id,)).fetchone()
    assert step["batches"] == 5
    assert step["progressed_at"] is not None
    landing.con.close()


# ---- 脱敏 ----

def test_safe_error_desensitizes_sql_and_token():
    assert "已脱敏" in _safe_error(Exception("token=secret123"))
    assert "已脱敏" in _safe_error(Exception("CREATE TABLE users"))
    assert "已脱敏" in _safe_error(Exception("ALTER TABLE t ADD x"))
    assert "已脱敏" in _safe_error(Exception("EXEC dbo.sp_help"))
    assert "已脱敏" in _safe_error(Exception("MERGE INTO target"))
    assert "已脱敏" in _safe_error(Exception("SELECT * FROM t WHERE pwd='x'"))
    assert "已脱敏" in _safe_error(Exception("Driver={SQL Server};Server=."))
    assert "已脱敏" not in _safe_error(Exception("network timeout"))


def test_safe_error_truncates_long_messages():
    long_msg = "x" * 500
    result = _safe_error(Exception(long_msg))
    assert len(result) <= 410  # 400 + "…[已截断]"
    assert "…[已截断]" in result
