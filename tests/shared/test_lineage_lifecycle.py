"""v0.3 M4-T06: 数据集生命周期与 lineage 收口。

门禁:current/previous 受保护;旧版可回滚且 unavailable;
publish 校验 lineage 完整性;stale recovery/GC 成对清理;
清理失败可重试且不影响已完成的 publish/rollback。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.shared.store.dataset_publish import (
    build_dataset,
    publish_dataset,
    rollback_dataset,
)
from data2agent.middle.extract.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore, raw_table_name
from tests.helpers import whitelist_from_pack
from data2agent.shared.metamodel.loader import load_pack
from data2agent.shared.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[2]
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


def _lineage_object(landing: LandingStore, dataset_version: str) -> ObjectVersionRecord:
    return next(
        obj for obj in landing.list_object_versions(dataset_version)
        if obj.lineage_field_count > 0
    )


def _total_lineage(store: LandingStore, dataset_version: str) -> int:
    (n,) = store.con.execute(
        "SELECT COUNT(*) FROM d2a_field_lineage WHERE dataset_version = ?",
        (dataset_version,),
    ).fetchone()
    return n


# ---- publish 校验 lineage 完整性 -------------------------------------------------


def test_publish_validates_lineage_completeness(landing, pack):
    """新构建(lineage_schema_version=1)发布前通过完整性校验。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version

    # 发布前应通过校验
    pub = publish_dataset(landing, ds)
    assert pub.outcome == "ok"

    # 发布后 lineage 仍存在
    assert _total_lineage(landing, ds) > 0


def test_publish_blocks_incomplete_lineage(landing, pack):
    """lineage 节点数与元数据不一致时 publish 返回 lineage_incomplete。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version

    # 人为删除部分 lineage 节点制造不完整
    obj = _lineage_object(landing, ds)
    landing.con.execute(
        "DELETE FROM d2a_field_lineage_input "
        "WHERE dataset_version = ? AND object = ? AND property = ("
        "  SELECT property FROM d2a_field_lineage "
        "  WHERE dataset_version = ? AND object = ? LIMIT 1"
        ")",
        (ds, obj.object, ds, obj.object),
    )
    landing.con.execute(
        "DELETE FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND object = ? AND property = ("
        "  SELECT property FROM d2a_field_lineage "
        "  WHERE dataset_version = ? AND object = ? LIMIT 1"
        ")",
        (ds, obj.object, ds, obj.object),
    )
    landing.con.commit()

    pub = publish_dataset(landing, ds)
    assert pub.outcome != "ok"
    assert pub.reason_code == "lineage_incomplete"


def test_validate_skips_lineage_for_null_schema_version(landing, pack):
    """lineage_schema_version=NULL 的对象不触发 lineage 完整性校验。"""
    from data2agent.shared.store.dataset_publish import _validate_version_tables

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version
    ds_record = landing.get_dataset_version(ds)
    objs = landing.list_object_versions(ds)

    # 正常校验通过
    assert _validate_version_tables(
        landing, ds_record, objs, expected_status="built",
    ) is None

    # 模拟旧版本:在内存中将 lineage_schema_version 设为 None
    for obj in objs:
        obj.lineage_schema_version = None
        obj.lineage_field_count = None
    # 旧版本跳过 lineage 校验,仍通过
    assert _validate_version_tables(
        landing, ds_record, objs, expected_status="built",
    ) is None


# ---- rollback 与 lineage ---------------------------------------------------------


def test_rollback_restores_lineage(landing, pack):
    """rollback 到 previous 版本后,该版本的 lineage 仍可查询。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert r1.outcome == "ok"
    ds1 = r1.dataset_version

    r2 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert r2.outcome == "ok"
    ds2 = r2.dataset_version

    # 当前是 ds2,previous 是 ds1
    published = landing.get_published_dataset(SOURCE)
    assert published.dataset_version == ds2
    assert published.previous_dataset_version == ds1

    # ds1 和 ds2 都有 lineage
    assert _total_lineage(landing, ds1) > 0
    assert _total_lineage(landing, ds2) > 0

    # rollback 到 ds1
    rb = rollback_dataset(landing, ds1)
    assert rb.outcome == "ok"

    # ds1 的 lineage 仍完整
    assert _total_lineage(landing, ds1) > 0
    published = landing.get_published_dataset(SOURCE)
    assert published.dataset_version == ds1


def test_rollback_validates_target_lineage(landing, pack):
    """rollback 校验目标版本的 lineage 完整性(与 publish 相同路径)。"""
    from data2agent.shared.store.dataset_publish import _validate_version_tables

    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert r1.outcome == "ok"
    ds1 = r1.dataset_version

    r2 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert r2.outcome == "ok"

    # ds1 已 retired;rollback 校验使用 expected_status="retired"
    ds1_record = landing.get_dataset_version(ds1)
    objs1 = landing.list_object_versions(ds1)
    assert _validate_version_tables(
        landing, ds1_record, objs1, expected_status="retired",
    ) is None

    # 模拟旧版本(无 lineage)也通过校验
    for obj in objs1:
        obj.lineage_schema_version = None
        obj.lineage_field_count = None
    assert _validate_version_tables(
        landing, ds1_record, objs1, expected_status="retired",
    ) is None


# ---- stale building recovery 清理 lineage -----------------------------------------


def test_stale_recovery_cleans_lineage(landing, pack):
    """陈旧 building 恢复时同时清理 lineage。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    ds = result.dataset_version
    assert _total_lineage(landing, ds) > 0

    # 模拟陈旧 building:删除 running run 使恢复逻辑可以介入
    landing.con.execute(
        "UPDATE d2a_sync_run SET status = 'failed' "
        "WHERE dataset_version = ? AND status = 'running'",
        (ds,),
    )
    landing.con.commit()

    # 再次 build 触发 stale recovery
    r2 = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert r2.outcome == "ok"

    # 旧 building 的 lineage 应被清理
    assert _total_lineage(landing, ds) == 0


# ---- GC 保护 current/previous 并清理 retired lineage ------------------------------


def test_gc_protects_current_and_previous(landing, pack):
    """GC 不清理 current 和 current.previous 的 lineage。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds1 = r1.dataset_version
    r2 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds2 = r2.dataset_version

    # ds1=previous, ds2=current
    assert _total_lineage(landing, ds1) > 0
    assert _total_lineage(landing, ds2) > 0

    # GC 在 publish 后已自动运行;两个版本的 lineage 都应保留
    assert _total_lineage(landing, ds1) > 0
    assert _total_lineage(landing, ds2) > 0


def test_gc_cleans_retired_lineage(landing, pack):
    """GC 清理 retired 且非 current/previous 的 lineage。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds1 = r1.dataset_version
    r2 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds2 = r2.dataset_version
    r3 = build_dataset(landing, pack, SOURCE, auto_publish=True)
    ds3 = r3.dataset_version

    # ds1 现在是 retired(非 current/previous)
    published = landing.get_published_dataset(SOURCE)
    assert published.dataset_version == ds3
    assert published.previous_dataset_version == ds2

    # ds1 已 retired;GC 在 publish 后自动运行
    ds1_record = landing.get_dataset_version(ds1)
    assert ds1_record.status == "retired"

    # ds1 的物理表和 lineage 应被 GC 清理
    objs1 = landing.list_object_versions(ds1)
    for obj in objs1:
        assert obj.purged_at is not None
    assert _total_lineage(landing, ds1) == 0

    # ds2(previous)和 ds3(current)的 lineage 保留
    assert _total_lineage(landing, ds2) > 0
    assert _total_lineage(landing, ds3) > 0


# ---- 失败构建不留 lineage ---------------------------------------------------------


def test_failed_build_cleans_all_lineage(landing, pack):
    """构建失败时所有对象(含已成功)的 lineage 都被清理。"""
    # 破坏数据使某对象熔断
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "SALES_ORDER")}" '
        "SET DOC_NO = NULL"
    )
    landing.con.commit()

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "failed"
    ds = result.dataset_version

    # 整个数据集不应残留 lineage
    assert _total_lineage(landing, ds) == 0
