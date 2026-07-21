"""v0.3 M2-T02: 存储迁移、不变量与类型化版本 CRUD。"""

from __future__ import annotations

import sqlite3

import pytest

from data2agent.connect.landing import LandingStore
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord

_OLD_DATASET_DDL = """
CREATE TABLE d2a_dataset_version (
    dataset_version TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    template_version TEXT NOT NULL,
    status TEXT NOT NULL,
    built_at TEXT NOT NULL,
    published_at TEXT,
    previous_dataset_version TEXT,
    error TEXT,
    object_manifest TEXT
);
CREATE TABLE d2a_object_version (
    dataset_version TEXT NOT NULL,
    object TEXT NOT NULL,
    object_version TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    batch_id TEXT,
    build_table TEXT,
    status TEXT NOT NULL,
    built_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (dataset_version, object)
);
CREATE TABLE d2a_sync_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    tables INTEGER, rows INTEGER, status TEXT, detail TEXT,
    run_type TEXT, steps_recorded INTEGER
);
"""


def test_m2_columns_indexes_present_on_fresh_db(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    ds_cols = {r[1] for r in store.con.execute("PRAGMA table_info(d2a_dataset_version)")}
    obj_cols = {r[1] for r in store.con.execute("PRAGMA table_info(d2a_object_version)")}
    run_cols = {r[1] for r in store.con.execute("PRAGMA table_info(d2a_sync_run)")}
    assert "template_snapshot" in ds_cols
    assert "purged_at" in obj_cols
    assert "dataset_version" in run_cols
    indexes = {
        r[0]
        for r in store.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_d2a_dataset_one_published" in indexes
    assert "idx_d2a_dataset_one_building" in indexes


def test_old_db_migrates_m2_columns_without_backfill(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript(_OLD_DATASET_DDL)
    con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at, published_at, "
        "object_manifest) "
        "VALUES ('ds-1', 'src', '0.1.0', 'published', '2026-07-21T10:00:00', "
        "'2026-07-21T10:00:00', '[\"Customer\"]')"
    )
    con.execute(
        "INSERT INTO d2a_object_version "
        "(dataset_version, object, object_version, binding_hash, row_count, "
        "build_table, status, built_at, published_at) "
        "VALUES ('ds-1', 'Customer', 'obj-1', 'sha256:x', 1, 'obj_Customer', "
        "'published', '2026-07-21T10:00:00', '2026-07-21T10:00:00')"
    )
    con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, status, run_type) "
        "VALUES ('src', '2026-07-21T09:00:00', 'ok', 'apply')"
    )
    con.execute('CREATE TABLE "obj_Customer" (id TEXT)')
    con.commit()
    con.close()

    store = LandingStore(db)
    ds = store.get_dataset_version("ds-1")
    assert ds is not None
    assert ds.template_snapshot is None
    assert ds.object_manifest == '["Customer"]'
    objs = store.list_object_versions("ds-1")
    assert objs[0].purged_at is None
    assert objs[0].build_table == "obj_Customer"
    run_dv = store.con.execute(
        "SELECT dataset_version FROM d2a_sync_run"
    ).fetchone()[0]
    assert run_dv is None
    tables = {
        r[0]
        for r in store.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "obj_Customer" in tables

    # 幂等
    LandingStore(db)
    assert store.get_dataset_version("ds-1").template_snapshot is None


def test_one_building_per_source_enforced(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-a",
            source="src",
            template_version="0.1.0",
            status="building",
            built_at="2026-07-21T10:00:00",
            object_manifest='["Customer"]',
            template_snapshot='{"version":"0.1.0"}',
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_dataset_version(
            DatasetVersionRecord(
                dataset_version="ds-b",
                source="src",
                template_version="0.1.0",
                status="building",
                built_at="2026-07-21T11:00:00",
                object_manifest='["Customer"]',
                template_snapshot='{"version":"0.1.0"}',
            )
        )


def test_frozen_dataset_fields_reject_update(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src",
            template_version="0.1.0",
            status="building",
            built_at="2026-07-21T10:00:00",
            object_manifest='["Customer"]',
            template_snapshot='{"version":"0.1.0"}',
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.con.execute(
            "UPDATE d2a_dataset_version SET template_version = '9.9.9' "
            "WHERE dataset_version = 'ds-1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.con.execute(
            "UPDATE d2a_dataset_version SET object_manifest = '[\"X\"]' "
            "WHERE dataset_version = 'ds-1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.con.execute(
            "UPDATE d2a_dataset_version SET template_snapshot = '{}' "
            "WHERE dataset_version = 'ds-1'"
        )
    # 生命周期字段仍可更新
    store.update_dataset_lifecycle(
        "ds-1", status="failed", error="build failed",
    )
    assert store.get_dataset_version("ds-1").status == "failed"


def test_object_purge_tombstone_allowed_other_build_table_rewrite_rejected(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src",
            template_version="0.1.0",
            status="retired",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot='{"version":"0.1.0"}',
        )
    )
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-1",
            object="Customer",
            object_version="obj-1",
            binding_hash="sha256:" + "a" * 64,
            row_count=2,
            build_table="objv_aa_bb_cc",
            status="retired",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.con.execute(
            "UPDATE d2a_object_version SET build_table = 'objv_xx_yy_zz' "
            "WHERE object_version = 'obj-1'"
        )
    store.purge_object_build_table("ds-1", "Customer", purged_at="2026-07-21T12:00:00")
    row = store.list_object_versions("ds-1")[0]
    assert row.build_table is None
    assert row.purged_at == "2026-07-21T12:00:00"


def test_set_run_dataset_version_validates_existence(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src",
            template_version="0.1.0",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot='{"version":"0.1.0"}',
        )
    )
    rid = store.start_run("src", "publish")
    store.set_run_dataset_version(rid, "ds-1")
    assert store.con.execute(
        "SELECT dataset_version FROM d2a_sync_run WHERE id = ?", (rid,)
    ).fetchone()[0] == "ds-1"
    with pytest.raises(ValueError, match="不存在"):
        store.set_run_dataset_version(rid, "missing")


def test_dirty_multi_building_db_still_opens(tmp_path):
    """脏旧库同 source 多个 building 不得阻断 LandingStore 启动。"""
    db = tmp_path / "dirty.sqlite"
    con = sqlite3.connect(db)
    con.executescript(_OLD_DATASET_DDL)
    for version in ("ds-a", "ds-b"):
        con.execute(
            "INSERT INTO d2a_dataset_version "
            "(dataset_version, source, template_version, status, built_at, "
            "object_manifest) "
            "VALUES (?, 'src', '0.1.0', 'building', '2026-07-21T10:00:00', "
            "'[\"Customer\"]')",
            (version,),
        )
    con.commit()
    con.close()

    store = LandingStore(db)
    indexes = {
        r[0]
        for r in store.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_d2a_dataset_one_building" not in indexes
    assert "idx_d2a_dataset_building_lookup" in indexes
    rows, total = store.list_dataset_versions(source="src", status="building")
    assert total == 2
    assert {r.dataset_version for r in rows} == {"ds-a", "ds-b"}


def test_purge_from_published_is_rejected(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src",
            template_version="0.1.0",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot='{"version":"0.1.0"}',
        )
    )
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-1",
            object="Customer",
            object_version="obj-1",
            binding_hash="sha256:" + "a" * 64,
            row_count=2,
            build_table="objv_aa_bb_cc",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    with pytest.raises(ValueError):
        store.purge_object_build_table(
            "ds-1", "Customer", purged_at="2026-07-21T12:00:00",
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.con.execute(
            "UPDATE d2a_object_version "
            "SET status = 'retired', build_table = NULL, "
            "purged_at = '2026-07-21T12:00:00' "
            "WHERE object_version = 'obj-1'"
        )
    store.con.rollback()
    row = store.list_object_versions("ds-1")[0]
    assert row.status == "published"
    assert row.build_table == "objv_aa_bb_cc"
    assert row.purged_at is None


def test_update_object_build_result_from_building(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src",
            template_version="0.1.0",
            status="building",
            built_at="2026-07-21T10:00:00",
            object_manifest='["Customer"]',
            template_snapshot='{"version":"0.1.0"}',
        )
    )
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-1",
            object="Customer",
            object_version="obj-1",
            binding_hash="sha256:" + "a" * 64,
            row_count=0,
            build_table=None,
            status="building",
            built_at="2026-07-21T10:00:00",
        )
    )
    store.update_object_build_result(
        "ds-1",
        "Customer",
        status="built",
        row_count=12,
        build_table="objv_aa_bb_cc",
        batch_id="b1",
    )
    row = store.list_object_versions("ds-1")[0]
    assert row.status == "built"
    assert row.row_count == 12
    assert row.build_table == "objv_aa_bb_cc"
    assert row.batch_id == "b1"
    with pytest.raises(ValueError):
        store.update_object_build_result(
            "ds-1",
            "Customer",
            status="built",
            row_count=13,
            build_table="objv_xx_yy_zz",
        )
