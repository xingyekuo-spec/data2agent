"""部分选表场景的 apply 容错:输入未就绪对象跳过,不卡死 generation 屏障。

背景:首次部署通常只选几张基线表,而模板包多数对象依赖未同步表或
场景预计算器。旧行为是整包构建失败 → generation 永久 committed →
新同步被 409 拒绝。现行为:缺输入表的对象跳过(下轮自动补齐),
其余对象正常发布,generation 正常办结。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import incremental_sync
from data2agent.shared.metamodel.loader import load_pack
from data2agent.shared.store.dataset_publish import build_dataset
from data2agent.shared.store.generation_apply import GenerationApplyLease
from data2agent.shared.store.landing import LandingStore
from tests.fixtures.e10.seed import build, write_db
from tests.helpers import watermarks_from_pack, whitelist_from_pack

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


def _landing_with_tables(tmp_path, pack, tables: set[str]) -> LandingStore:
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(
        adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE),
        only_tables=tables,
    )
    return landing


def test_partial_inputs_skip_unready_objects_and_publish(tmp_path, pack):
    landing = _landing_with_tables(tmp_path, pack, {"CUSTOMER", "CURRENCY"})
    try:
        result = build_dataset(landing, pack, SOURCE, auto_publish=True)
        assert result.outcome == "ok", result.error
        assert result.published is True

        built = {r.object for r in result.results}
        assert "Customer" in built  # 直接映射对象:输入齐备,正常发布

        skipped = {s["object"]: s for s in result.skipped}
        # 缺 SALES_ORDER/SALES_ORDER_D 的对象被跳过并说明缺哪张表
        assert "SalesOrder" in skipped
        assert "SALES_ORDER" in skipped["SalesOrder"]["missing_tables"]
        # 缺 ITEM_WAREHOUSE 的预计算链全部级联跳过
        assert "DeadStockItem" in skipped
        assert "ITEM_WAREHOUSE" in skipped["DeadStockItem"]["missing_tables"]
        assert "DeadStockAttribution" in skipped
        # 跳过不算失败:已发布版本只含就绪对象
        published = landing.get_published_dataset(SOURCE)
        assert published is not None
        objs = landing.list_object_versions(published.dataset_version)
        assert {o.object for o in objs} == built
    finally:
        landing.con.close()


def test_all_objects_not_ready_returns_not_ready(tmp_path, pack):
    # 同步一张不在任何绑定里的表:所有对象输入未就绪
    landing = _landing_with_tables(tmp_path, pack, {"BIN"})
    try:
        result = build_dataset(landing, pack, SOURCE, auto_publish=True)
        assert result.outcome == "not_ready"
        assert result.reason_code == "inputs_not_ready"
        assert result.published is False
        assert len(result.skipped) > 0
        assert landing.get_published_dataset(SOURCE) is None
    finally:
        landing.con.close()


def test_apply_with_partial_inputs_releases_generation_barrier(tmp_path, pack):
    """核心回归:部分选表时 apply 成功办结,新同步不再被 409 拒绝。"""
    landing = _landing_with_tables(tmp_path, pack, {"CUSTOMER", "CURRENCY"})
    try:
        landing.begin_ingest_generation(SOURCE, "g-partial", [])
        landing.complete_ingest_generation(SOURCE, "g-partial")

        lease = GenerationApplyLease.claim(landing, SOURCE)
        assert lease is not None
        result = build_dataset(landing, pack, SOURCE, auto_publish=True)
        success = result.outcome == "not_ready" or (
            result.outcome == "ok" and result.published
        )
        lease.finish(landing, success=success)
        assert success

        # 屏障已解除:下一代 run-begin 不再被拒绝
        nxt = landing.begin_ingest_generation(SOURCE, "g-next", [])
        assert nxt is not None
    finally:
        landing.con.close()


def test_objects_build_automatically_after_inputs_arrive(tmp_path, pack):
    """下轮同步补齐输入表后,此前跳过的对象自动构建。"""
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    try:
        adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
        watermarks = watermarks_from_pack(pack, SOURCE)
        incremental_sync(adapter, landing, SOURCE, watermarks,
                         only_tables={"CUSTOMER", "CURRENCY"})
        first = build_dataset(landing, pack, SOURCE, auto_publish=True)
        assert "SalesOrder" in {s["object"] for s in first.skipped}

        incremental_sync(adapter, landing, SOURCE, watermarks)  # 全量选表
        second = build_dataset(landing, pack, SOURCE, auto_publish=True)
        assert second.outcome == "ok", second.error
        assert second.skipped == []
        built = {r.object for r in second.results}
        assert {"SalesOrder", "DeadStockItem"} <= built
    finally:
        landing.con.close()
