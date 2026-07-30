"""元数据发现协议、工厂、候选键/水位与扫描缓存。"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from data2agent.shared.config import SourceConfig, TableExtractConfig, load_config
from data2agent.middle.extract.metadata import (
    ColumnMeta,
    MetadataDiscoveryUnsupported,
    MetadataError,
    ScanStore,
    TableSummary,
    build_discoverer,
    discoverer_default_schema,
    extraction_plan_keys,
    in_extraction_plan,
    map_odbc_error,
    schema_fingerprint,
    suggest_watermark_candidates,
)
import data2agent.middle.extract.discoverers  # noqa: F401


def _write_sample_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE ITEM (
            ITEM_ID TEXT PRIMARY KEY,
            CODE TEXT NOT NULL UNIQUE,
            UPDATE_TIME TEXT,
            NOTE TEXT
        );
        CREATE TABLE ITEM_WAREHOUSE (
            ITEM_ID TEXT NOT NULL,
            WAREHOUSE_ID TEXT NOT NULL,
            QTY REAL,
            PRIMARY KEY (ITEM_ID, WAREHOUSE_ID)
        );
        CREATE TABLE DUP_DEMO (
            A TEXT,
            B TEXT
        );
        INSERT INTO ITEM VALUES ('1','SKU1','2026-07-01','x');
        INSERT INTO ITEM VALUES ('2','SKU2','2026-07-02',NULL);
        INSERT INTO ITEM_WAREHOUSE VALUES ('1','W1',1.0);
        INSERT INTO DUP_DEMO VALUES ('a','b');
        INSERT INTO DUP_DEMO VALUES ('a','b');
        INSERT INTO DUP_DEMO VALUES ('a',NULL);
        """
    )
    con.close()


@pytest.fixture()
def sqlite_source(tmp_path: Path) -> tuple[Path, SourceConfig]:
    db = tmp_path / "src.sqlite"
    _write_sample_db(db)
    scfg = SourceConfig(
        adapter="sqlite_readonly",
        path=str(db),
        tables={},
    )
    return db, scfg


def test_suggest_watermark_by_name_and_type():
    cols = [
        ColumnMeta("UPDATE_TIME", 1, "datetime", True),
        ColumnMeta("NOTE", 2, "nvarchar", True),
        ColumnMeta("AMOUNT", 3, "decimal", True),
    ]
    assert suggest_watermark_candidates(cols) == ["UPDATE_TIME"]


def test_schema_fingerprint_stable():
    cols = [ColumnMeta("ID", 1, "int", False)]
    a = schema_fingerprint(cols, ["ID"], [])
    b = schema_fingerprint(cols, ["ID"], [])
    assert a == b and a.startswith("sha256:")


def test_build_discoverer_unsupported():
    scfg = SourceConfig.model_construct(
        adapter="no_such_adapter", path="x", tables={})
    with pytest.raises(MetadataDiscoveryUnsupported) as ei:
        build_discoverer(scfg)
    assert ei.value.code == "metadata_discovery_unsupported"


def test_sqlite_discoverer_lists_and_details(sqlite_source):
    _db, scfg = sqlite_source
    d = build_discoverer(scfg)
    try:
        schemas = d.list_schemas()
        assert schemas == ["main"]
        tables, total = d.list_tables()
        assert total >= 3
        names = {t.name for t in tables}
        assert {"ITEM", "ITEM_WAREHOUSE", "DUP_DEMO"} <= names
        item = d.get_table("main", "ITEM")
        assert item.primary_key == ("ITEM_ID",)
        assert "UPDATE_TIME" in item.watermark_candidates
        assert any(k.columns == ("CODE",) for k in item.unique_keys)
        wh = d.get_table("main", "ITEM_WAREHOUSE")
        assert wh.primary_key == ("ITEM_ID", "WAREHOUSE_ID")
    finally:
        d.close()


def test_sqlite_key_and_watermark_checks(sqlite_source):
    _db, scfg = sqlite_source
    d = build_discoverer(scfg)
    try:
        ok = d.check_key("main", "ITEM", ["ITEM_ID"])
        assert ok.ok and ok.code == "ready"
        bad = d.check_key("main", "DUP_DEMO", ["A", "B"])
        assert not bad.ok and bad.code == "key_not_unique"
        missing = d.check_key("main", "ITEM", ["NOPE"])
        assert missing.code == "key_missing"
        wm = d.check_watermark("main", "ITEM", "UPDATE_TIME")
        assert wm.ok and wm.candidate is True
        wm_missing = d.check_watermark("main", "ITEM", "NOPE")
        assert wm_missing.code == "watermark_missing"
    finally:
        d.close()


def test_scan_store_uses_utc_timestamps_not_monotonic():
    store = ScanStore(ttl_seconds=60, max_scans=4, max_active_scans=2)
    rec = store.try_begin("s1")
    summary = rec.summary()
    created = datetime.strptime(summary["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    assert created.year >= 2020
    store.complete(rec.scan_id, [], {})
    done = store.get(rec.scan_id)
    assert done is not None
    finished = datetime.strptime(
        done.summary()["finished_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    assert finished.year >= 2020


def test_scan_store_active_slot_and_completed_not_shadowed():
    store = ScanStore(ttl_seconds=60, max_scans=8, max_active_scans=1)
    first = store.try_begin("s1")
    store.complete(first.scan_id, [
        TableSummary("dbo", "ITEM", "table", 1, ("ID",), (), ()),
    ], {})
    assert store.latest_completed_for_source("s1").scan_id == first.scan_id

    second = store.try_begin("s1")
    # running 新扫描不得遮蔽旧 completed
    assert store.latest_completed_for_source("s1").scan_id == first.scan_id
    assert store.latest_for_source("s1").scan_id == second.scan_id

    with pytest.raises(MetadataError) as ei:
        store.try_begin("s1")
    assert ei.value.code == "scan_busy"

    store.fail(second.scan_id, "boom", "x")
    assert store.latest_completed_for_source("s1").scan_id == first.scan_id


def test_scan_store_ttl_evicts_inactive_only():
    store = ScanStore(ttl_seconds=0.05, max_scans=2, max_active_scans=2)
    a = store.try_begin("s1")
    store.complete(a.scan_id, [], {})
    b = store.try_begin("s1")
    store.complete(b.scan_id, [], {})
    time.sleep(0.06)
    store.purge_expired()
    assert store.get(a.scan_id) is None
    assert store.get(b.scan_id) is None


def test_extraction_plan_respects_schema():
    tables = {
        "ITEM": TableExtractConfig(mode="full_refresh", schema="dbo"),
    }
    planned = extraction_plan_keys(tables, default_schema="dbo")
    assert in_extraction_plan("dbo", "ITEM", planned)
    assert not in_extraction_plan("audit", "ITEM", planned)


def test_map_odbc_error_codes():
    assert map_odbc_error(RuntimeError("Login failed for user")).code == "connection_failed"
    assert map_odbc_error(RuntimeError("HYT00 Timeout expired")).code == "timeout"
    assert map_odbc_error(RuntimeError(
        "[Microsoft][ODBC Driver 18 for SQL Server]TCP 提供程序: 等待的操作过时。"
        " (258); 登录超时已过期",
    )).code == "timeout"
    assert map_odbc_error(RuntimeError("permission denied on object")).code == "permission_denied"
    err = map_odbc_error(RuntimeError(
        "Driver error: Server=10.0.0.7;Database=ERP;pwd=secret"))
    assert err.code == "connection_failed"
    assert err.message == "数据库访问失败"
    assert "10.0.0.7" not in err.message
    assert "ERP" not in err.message
    assert "secret" not in err.message


def test_discoverer_default_schema_from_registry():
    assert discoverer_default_schema("sqlite_readonly") == "main"
    assert discoverer_default_schema("mssql_readonly") == "dbo"
    with pytest.raises(MetadataDiscoveryUnsupported):
        discoverer_default_schema("nope_adapter")


def test_list_tables_keeps_catalog_without_get_table_enrichment(sqlite_source):
    """目录列举不得因详情失败而静默丢表。"""
    _db, scfg = sqlite_source
    d = build_discoverer(scfg)
    try:
        tables, total = d.list_tables()
        assert total >= 3
        assert {t.name for t in tables} >= {"ITEM", "ITEM_WAREHOUSE", "DUP_DEMO"}
        assert all(t.primary_key == () for t in tables)
    finally:
        d.close()


def test_sqlite_excludes_partial_unique_indexes(tmp_path: Path):
    db = tmp_path / "partial.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE T (
            ID TEXT PRIMARY KEY,
            CODE TEXT NOT NULL,
            ACTIVE INT NOT NULL
        );
        CREATE UNIQUE INDEX ux_code_pair ON T(ID, CODE);
        CREATE UNIQUE INDEX ux_code_active ON T(CODE) WHERE ACTIVE = 1;
        INSERT INTO T VALUES ('1', 'A', 1);
        INSERT INTO T VALUES ('2', 'A', 0);
        """
    )
    con.close()
    scfg = SourceConfig(adapter="sqlite_readonly", path=str(db), tables={})
    d = build_discoverer(scfg)
    try:
        assert d.default_schema() == "main"
        detail = d.get_table("main", "T")
        uniq = [k for k in detail.unique_keys if k.kind == "unique_index"]
        names = {k.name for k in uniq}
        assert "ux_code_pair" in names
        assert "ux_code_active" not in names
    finally:
        d.close()


def test_empty_tables_config_can_build_discoverer(tmp_path: Path):
    db = tmp_path / "src.sqlite"
    _write_sample_db(db)
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "templates": "templates",
            "landing": str(tmp_path / "landing.sqlite"),
            "sources": {
                "digiwin_e10": {
                    "adapter": "sqlite_readonly",
                    "path": str(db),
                    "tables": {},
                }
            },
        }),
        encoding="utf-8",
    )
    loaded = load_config(cfg)
    scfg = loaded.sources["digiwin_e10"]
    assert scfg.table_whitelist() == set()
    d = build_discoverer(scfg)
    try:
        tables, _ = d.list_tables()
        assert any(t.name == "ITEM" for t in tables)
    finally:
        d.close()


def test_metadata_module_has_no_mssql_system_sql():
    text = Path("data2agent/middle/extract/metadata.py").read_text(encoding="utf-8")
    assert "INFORMATION_SCHEMA" not in text
    assert "sys.tables" not in text
    assert "mssql_readonly" not in text
    assert "sqlite_readonly" not in text


def test_app_default_schema_does_not_hardcode_adapter_branch():
    text = Path("data2agent/middle/admin/app.py").read_text(encoding="utf-8")
    assert "discoverer_default_schema" in text
    assert 'scfg.adapter == "sqlite_readonly"' not in text
    assert 'adapter == "sqlite_readonly" else "dbo"' not in text


def test_mssql_pagination_sql_2008r2_compatible():
    """SQL Server 2008 R2 不支持 OFFSET/FETCH(2012+):
    发现器列表分页与适配器全量分页必须用 ROW_NUMBER。"""
    import inspect
    from data2agent.middle.extract.discoverers.mssql import MssqlMetadataDiscoverer
    from data2agent.middle.extract.adapters.mssql import MssqlReadOnlyAdapter

    disc_src = inspect.getsource(MssqlMetadataDiscoverer.list_tables)
    assert "ROW_NUMBER()" in disc_src
    assert "OFFSET ?" not in disc_src and "FETCH NEXT" not in disc_src

    adapter = MssqlReadOnlyAdapter.__new__(MssqlReadOnlyAdapter)
    from data2agent.shared.store.table import TableInfo
    info = TableInfo(name="SALES_ORDER",
                     columns=[("ID", "int"), ("CODE", "text")], pk=["ID"])
    sql = adapter._page_sql(info, 5000, 10000)
    assert "ROW_NUMBER()" in sql
    assert " OFFSET " not in sql and "FETCH NEXT" not in sql
    assert "TOP 5000" in sql and "_d2a_rn > 10000" in sql
    # 子查询必须带别名(SQL Server 要求派生表有别名)
    assert " AS _d2a_p " in sql
