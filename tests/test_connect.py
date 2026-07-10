"""抽取框架 E1 测试:安全强制(白名单/只读)、原样落地、幂等 upsert、审计。"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.base import ReadOnlyViolation, TableInfo, WhitelistViolation
from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.sync import full_sync, whitelist_from_pack
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


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


def _adapter(source_db, pack, landing=None, **kw):
    hook = None
    if landing is not None:
        hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    return SqliteReadOnlyAdapter(str(source_db), whitelist_from_pack(pack, SOURCE),
                                 audit_hook=hook, **kw)


def test_whitelist_derived_from_bindings(pack):
    assert whitelist_from_pack(pack, SOURCE) == {
        "CUSTOMER", "CURRENCY", "ITEM", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"}


def test_whitelist_enforced(source_db, pack):
    adapter = _adapter(source_db, pack)
    with pytest.raises(WhitelistViolation):
        adapter.table_info("sqlite_master")


def test_readonly_guard(source_db, pack):
    adapter = _adapter(source_db, pack)
    with pytest.raises(ReadOnlyViolation):
        adapter._audited_fetch("DELETE FROM CUSTOMER")
    with pytest.raises(ReadOnlyViolation):
        adapter._audited_fetch("SELECT 1; DROP TABLE CUSTOMER")  # 拒绝多语句


def test_full_sync_lands_everything(source_db, pack, landing):
    report = full_sync(_adapter(source_db, pack, landing), landing, SOURCE)
    src = sqlite3.connect(source_db)
    for t in report.tables:
        (n,) = src.execute(f'SELECT COUNT(*) FROM "{t.table}"').fetchone()
        assert landing.count(SOURCE, t.table) == n == t.rows
    # 元数据列已填
    row = landing.con.execute(
        f'SELECT * FROM "{raw_table_name(SOURCE, "CUSTOMER")}" LIMIT 1').fetchone()
    assert row["_d2a_batch_id"] and row["_d2a_row_hash"] and row["_d2a_deleted_at"] is None
    # 审计与运行汇总
    (audits,) = landing.con.execute("SELECT COUNT(*) FROM d2a_audit_log").fetchone()
    assert audits >= len(report.tables)
    run = landing.con.execute("SELECT * FROM d2a_sync_run WHERE id = ?",
                              (report.run_id,)).fetchone()
    assert run["status"] == "ok" and run["rows"] == report.total_rows


def test_sync_idempotent(source_db, pack, landing):
    full_sync(_adapter(source_db, pack, landing), landing, SOURCE)
    before = landing.count(SOURCE, "SALES_ORDER")
    full_sync(_adapter(source_db, pack, landing), landing, SOURCE)
    assert landing.count(SOURCE, "SALES_ORDER") == before, "重跑必须幂等"


def test_upsert_reflects_source_change(source_db, pack, landing):
    full_sync(_adapter(source_db, pack, landing), landing, SOURCE)
    rw = sqlite3.connect(source_db)
    rw.execute("UPDATE CUSTOMER SET CUSTOMER_NAME = '改名测试' WHERE Id = 1")
    rw.commit()
    full_sync(_adapter(source_db, pack, landing), landing, SOURCE)
    row = landing.con.execute(
        f'SELECT CUSTOMER_NAME FROM "{raw_table_name(SOURCE, "CUSTOMER")}" WHERE Id = 1'
    ).fetchone()
    assert row["CUSTOMER_NAME"] == "改名测试"


def test_batching(source_db, pack, landing):
    adapter = _adapter(source_db, pack, landing, batch_size=50)
    report = full_sync(adapter, landing, SOURCE)
    quotation = next(t for t in report.tables if t.table == "QUOTATION")
    assert quotation.rows == 180 and quotation.batches == 4  # 50*3 + 30


def test_landing_requires_pk(landing):
    info = TableInfo(name="NO_PK", columns=[("A", "text")], pk=[])
    with pytest.raises(ValueError, match="无主键"):
        landing.ensure_raw_table(SOURCE, info)


def test_empty_whitelist_rejected(source_db):
    with pytest.raises(ValueError, match="白名单为空"):
        SqliteReadOnlyAdapter(str(source_db), set())
