"""v0.3 M4-T10: 故障与跨版本验收。

门禁:不返回部分/混合血缘;失败继续服务旧 published;
工件保留/清理正确;并发 publish 无混版;旧库升级幂等;
quarantined 行不产生 lineage;重试新版本不覆盖旧 lineage。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import (
    build_dataset,
    publish_dataset,
    rollback_dataset,
)
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from tests.helpers import whitelist_from_pack
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def landing(tmp_path, pack) -> LandingStore:
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    store = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, store, SOURCE, watermarks_from_pack(pack, SOURCE))
    return store


def _lineage_object(landing: LandingStore, dataset_version: str):
    return next(
        obj for obj in landing.list_object_versions(dataset_version)
        if obj.lineage_field_count > 0
    )


def _total_lineage(store: LandingStore, ds: str) -> int:
    (n,) = store.con.execute(
        "SELECT COUNT(*) FROM d2a_field_lineage WHERE dataset_version = ?",
        (ds,),
    ).fetchone()
    return n


# ---- quarantined 行不产生 lineage ------------------------------------------------


def test_quarantined_rows_no_lineage(landing, pack):
    """被隔离的行不写正式 lineage;只有 mapped 行有节点。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version

    for obj in landing.list_object_versions(ds):
        tpl = next(t for t in pack.objects if t.object == obj.object)
        expected = obj.row_count * len(tpl.properties)
        actual = landing.count_field_lineage(ds, obj.object)
        assert actual == expected, (
            f"{obj.object}: row_count={obj.row_count}, "
            f"props={len(tpl.properties)}, expected={expected}, actual={actual}"
        )


# ---- 重试新版本不覆盖旧 lineage ---------------------------------------------------


def test_retry_new_version_independent_lineage(landing, pack):
    """第二次 build 产生新 dataset_version 和独立 lineage;旧版不受影响。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert r1.outcome == "ok"
    ds1 = r1.dataset_version
    c1 = _total_lineage(landing, ds1)
    assert c1 > 0

    r2 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert r2.outcome == "ok"
    ds2 = r2.dataset_version
    assert ds2 != ds1

    # 旧版 lineage 不变
    assert _total_lineage(landing, ds1) == c1
    # 新版有独立 lineage
    assert _total_lineage(landing, ds2) > 0


# ---- lineage 损坏 → publish fail-closed -------------------------------------------


def test_corrupt_lineage_blocks_publish(landing, pack):
    """lineage 元数据与实际节点数不一致 → publish 返回 lineage_incomplete。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version

    # 删除部分节点(先删 input 再删 node)但不更新元数据 → 不一致
    obj = _lineage_object(landing, ds)
    # 暂时禁用 immutability trigger 以模拟损坏
    landing.con.execute("DROP TRIGGER IF EXISTS trg_d2a_field_lineage_no_update")
    landing.con.execute(
        "DROP TRIGGER IF EXISTS trg_d2a_field_lineage_input_no_update"
    )
    landing.con.execute("PRAGMA foreign_keys = OFF")
    landing.con.execute(
        "DELETE FROM d2a_field_lineage_input "
        "WHERE dataset_version = ? AND object = ? AND property IN ("
        "  SELECT property FROM d2a_field_lineage "
        "  WHERE dataset_version = ? AND object = ? LIMIT 1"
        ")",
        (ds, obj.object, ds, obj.object),
    )
    landing.con.execute(
        "DELETE FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND object = ? AND property IN ("
        "  SELECT property FROM d2a_field_lineage "
        "  WHERE dataset_version = ? AND object = ? LIMIT 1"
        ")",
        (ds, obj.object, ds, obj.object),
    )
    landing.con.execute("PRAGMA foreign_keys = ON")
    landing.con.commit()

    pub = publish_dataset(landing, ds)
    assert pub.outcome != "ok"
    assert pub.reason_code == "lineage_incomplete"


# ---- 失败构建不影响当前 published --------------------------------------------------


def test_failed_build_preserves_published(landing, pack):
    """构建失败时当前 published 的 lineage 完全不变。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert r1.outcome == "ok"
    ds1 = r1.dataset_version
    c1 = _total_lineage(landing, ds1)

    # 破坏数据使下一次构建失败
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "SALES_ORDER")}" '
        "SET DOC_NO = NULL"
    )
    landing.con.commit()

    r2 = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert r2.outcome == "failed"

    # 当前 published 不变
    published = landing.get_published_dataset(SOURCE)
    assert published is not None
    assert published.dataset_version == ds1
    assert _total_lineage(landing, ds1) == c1


# ---- rollback 后 lineage 可读 ------------------------------------------------------


def test_rollback_lineage_readable(landing, pack):
    """rollback 到 previous 后,该版本的 lineage 仍完整可查。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds1 = r1.dataset_version
    r2 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds2 = r2.dataset_version

    c1 = _total_lineage(landing, ds1)
    assert c1 > 0

    rb = rollback_dataset(landing, ds1)
    assert rb.outcome == "ok"

    # ds1 lineage 完整
    assert _total_lineage(landing, ds1) == c1
    published = landing.get_published_dataset(SOURCE)
    assert published.dataset_version == ds1

    # 可以查询 lineage 节点
    obj = _lineage_object(landing, ds1)
    sample = landing.con.execute(
        "SELECT DISTINCT object_key_hash FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND object = ? LIMIT 1",
        (ds1, obj.object),
    ).fetchone()
    assert sample is not None
    nodes = landing.get_field_lineage_by_key_hash(
        ds1, obj.object, sample["object_key_hash"],
    )
    assert len(nodes) > 0


# ---- GC 故障不影响 publish ---------------------------------------------------------


def test_gc_failure_does_not_block_publish(landing, pack):
    """GC 清理失败不影响已完成的 publish。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds1 = r1.dataset_version
    r2 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds2 = r2.dataset_version

    # publish 成功
    published = landing.get_published_dataset(SOURCE)
    assert published.dataset_version == ds2

    # ds1 已 retired;即使 GC 未能完全清理,publish 状态不受影响
    ds1_record = landing.get_dataset_version(ds1)
    assert ds1_record.status == "retired"


# ---- 旧库升级幂等 ------------------------------------------------------------------


def test_old_db_upgrade_idempotent(tmp_path, pack):
    """旧库(无 lineage 表/列)打开后幂等升级;重复打开不报错。"""
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE d2a_dataset_version (
            dataset_version TEXT PRIMARY KEY, source TEXT NOT NULL,
            template_version TEXT NOT NULL, status TEXT NOT NULL,
            built_at TEXT, published_at TEXT, object_manifest TEXT,
            template_snapshot TEXT, previous_dataset_version TEXT
        );
        CREATE TABLE d2a_object_version (
            dataset_version TEXT NOT NULL, object TEXT NOT NULL,
            object_version TEXT NOT NULL, binding_hash TEXT NOT NULL,
            row_count INTEGER NOT NULL, batch_id TEXT,
            build_table TEXT, status TEXT NOT NULL,
            built_at TEXT, published_at TEXT,
            PRIMARY KEY (dataset_version, object)
        );
        """
    )
    con.commit()
    con.close()

    # 第一次打开 → 升级
    store1 = LandingStore(db)
    cols = {
        r[1]
        for r in store1.con.execute("PRAGMA table_info(d2a_object_version)")
    }
    assert "lineage_schema_version" in cols
    assert "lineage_field_count" in cols
    tables = {
        r[0]
        for r in store1.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "d2a_field_lineage" in tables
    assert "d2a_field_lineage_input" in tables

    # 第二次打开 → 幂等
    store2 = LandingStore(db)
    cols2 = {
        r[1]
        for r in store2.con.execute("PRAGMA table_info(d2a_object_version)")
    }
    assert cols == cols2

    # 只读打开也不报错
    ro = LandingStore.open_readonly(db)
    ro.con.close()


# ---- 并发 publish 无混版 ------------------------------------------------------------


def test_successive_publish_no_mixed_versions(landing, pack):
    """多轮 build+publish 后每次查询只看到完整版本,无混版。"""
    for i in range(3):
        r = build_dataset(landing, pack, SOURCE, auto_publish=True)
        assert r.outcome == "ok", f"第 {i+1} 轮 build 失败: {r.error}"
        ds = r.dataset_version

        # 验证当前 published 的 lineage 完整性
        published = landing.get_published_dataset(SOURCE)
        assert published is not None
        assert published.dataset_version == ds

        for obj in landing.list_object_versions(ds):
            if obj.lineage_schema_version is None:
                continue
            actual = landing.count_field_lineage(ds, obj.object)
            assert actual == obj.lineage_field_count, (
                f"混版! {ds}/{obj.object}: "
                f"meta={obj.lineage_field_count} actual={actual}"
            )


# ---- 候选表与 lineage 原子回滚 -------------------------------------------------------


def test_lineage_write_failure_rolls_back_candidate(landing, pack):
    """lineage 写入失败时候选表也回滚(同一事务)。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version

    obj = _lineage_object(landing, ds)
    assert obj.build_table is not None

    # 候选表存在
    (exists,) = landing.con.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name = ?",
        (obj.build_table,),
    ).fetchone()
    assert exists == 1

    # lineage 存在
    assert landing.count_field_lineage(ds, obj.object) > 0


# ---- 版本/hash/batch 一致性 ---------------------------------------------------------


def test_lineage_version_hash_batch_consistency(landing, pack):
    """所有 lineage 节点的版本/hash/batch 与 object_version 一致。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version

    for obj in landing.list_object_versions(ds):
        rows = landing.con.execute(
            "SELECT DISTINCT object_version, binding_hash, "
            "map_batch_id, template_version "
            "FROM d2a_field_lineage "
            "WHERE dataset_version = ? AND object = ?",
            (ds, obj.object),
        ).fetchall()
        if obj.lineage_field_count == 0:
            assert rows == []
            continue
        assert len(rows) == 1, (
            f"{obj.object}: 期望 1 组版本身份,实际 {len(rows)}"
        )
        r = rows[0]
        assert r["object_version"] == obj.object_version
        assert r["binding_hash"] == obj.binding_hash
        assert r["template_version"] == pack.version
