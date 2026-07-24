"""v0.3 M4-T05: 正式 apply 原子写入 lineage。

门禁:熔断/写失败不留候选或 lineage;good 行每属性一节点;
Preview 零写入;遗留 apply 无 context 不写 lineage;
build_dataset 产出的 lineage 绑定 dataset/object/template/binding/batch。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.field_lineage import (
    ApplyVersionContext,
    canonical_object_key_json,
    object_key_token,
)
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.mapping_apply import apply_object, apply_objects
from tests.helpers import whitelist_from_pack
from data2agent.metamodel.loader import load_pack
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord
from tests.fixtures.e10.seed import build, write_db

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


def _lineage_object(landing: LandingStore, dataset_version: str) -> ObjectVersionRecord:
    return next(
        obj for obj in landing.list_object_versions(dataset_version)
        if obj.lineage_field_count > 0
    )


# ---- build_dataset 产出 lineage ------------------------------------------------


def test_build_dataset_writes_lineage(landing, pack):
    """build_dataset 成功后每个对象都有 lineage 节点和元数据。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    assert result.dataset_version is not None
    ds_version = result.dataset_version

    objs = landing.list_object_versions(ds_version)
    assert len(objs) > 0

    for obj in objs:
        assert obj.status == "built"
        assert obj.lineage_schema_version == 1
        assert obj.lineage_field_count is not None
        assert obj.lineage_field_count >= 0

        # 完整性:field_count == row_count × 模板属性数
        tpl = next(t for t in pack.objects if t.object == obj.object)
        expected = obj.row_count * len(tpl.properties)
        assert obj.lineage_field_count == expected

        # 物理节点数与元数据一致
        actual = landing.count_field_lineage(ds_version, obj.object)
        assert actual == obj.lineage_field_count


def test_build_dataset_lineage_bound_to_version(landing, pack):
    """lineage 节点绑定正确的 dataset/object/template/binding/batch。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    ds_version = result.dataset_version
    assert ds_version is not None

    obj = _lineage_object(landing, ds_version)

    rows = landing.con.execute(
        "SELECT * FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND object = ? LIMIT 5",
        (ds_version, obj.object),
    ).fetchall()
    assert len(rows) > 0
    for r in rows:
        assert r["dataset_version"] == ds_version
        assert r["object_version"] == obj.object_version
        assert r["template_version"] == pack.version
        assert r["binding_hash"] == obj.binding_hash
        assert r["map_batch_id"]  # 非空
        assert r["binding_status"] in ("draft", "verified", "disabled")
        assert r["source"] == SOURCE


def test_build_dataset_lineage_has_inputs(landing, pack):
    """direct 字段有 value 输入边;derived 字段有 derived_condition 输入边。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    ds_version = result.dataset_version
    assert ds_version is not None

    # 检查至少有一些输入边
    (input_count,) = landing.con.execute(
        "SELECT COUNT(*) FROM d2a_field_lineage_input "
        "WHERE dataset_version = ?",
        (ds_version,),
    ).fetchone()
    assert input_count > 0

    # 检查 value 角色的输入边
    (value_count,) = landing.con.execute(
        "SELECT COUNT(*) FROM d2a_field_lineage_input "
        "WHERE dataset_version = ? AND role = 'value'",
        (ds_version,),
    ).fetchone()
    assert value_count > 0


def test_build_dataset_lineage_key_hash_queryable(landing, pack):
    """通过 key_token 可以查询到 lineage 节点。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    ds_version = result.dataset_version
    assert ds_version is not None

    obj = _lineage_object(landing, ds_version)
    tpl = next(t for t in pack.objects if t.object == obj.object)

    # 从 lineage 自身取一个 key_hash(避免 SQLite 类型亲和性差异)
    sample = landing.con.execute(
        "SELECT DISTINCT object_key_hash FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND object = ? LIMIT 1",
        (ds_version, obj.object),
    ).fetchone()
    assert sample is not None
    token = sample["object_key_hash"]

    nodes = landing.get_field_lineage_by_key_hash(ds_version, obj.object, token)
    assert len(nodes) == len(tpl.properties)

    inputs = landing.get_field_lineage_inputs_by_key_hash(
        ds_version, obj.object, token,
    )
    assert len(inputs) > 0


# ---- 熔断不留 lineage -----------------------------------------------------------


def test_breaker_trips_no_lineage(landing, pack):
    """熔断时不创建候选表、不写 lineage、不标记 object built。"""
    # 破坏数据使某对象隔离率超阈值
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "SALES_ORDER")}" '
        "SET DOC_NO = NULL"
    )
    landing.con.commit()

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "failed"
    assert result.dataset_version is not None

    # 失败数据集中所有对象都不应残留 lineage(含 pending_ok 清理)
    for obj in landing.list_object_versions(result.dataset_version):
        count = landing.count_field_lineage(result.dataset_version, obj.object)
        assert count == 0, (
            f"{obj.object}(status={obj.status}) 残留 {count} 条 lineage"
        )


# ---- 遗留 apply 无 lineage -------------------------------------------------------


def test_legacy_apply_no_lineage(landing, pack):
    """apply_objects(无 version_context)不写 lineage。"""
    report = apply_objects(landing, pack, SOURCE)
    assert not report.aborted
    for r in report.results:
        if r.build_table:
            assert landing.count_field_lineage("ds-legacy", r.object) == 0


def test_apply_object_without_context_no_lineage(landing, pack):
    """单对象 apply 无 context 时不写 lineage,行为与 M2 一致。"""
    from data2agent.metamodel.dataset_publish_contract import make_build_table
    import uuid

    tpl = pack.objects[0]
    table = make_build_table(SOURCE, tpl.object, uuid.uuid4().hex[:12])
    result = apply_object(
        landing, tpl, SOURCE, build_table=table,
    )
    assert result.status == "ok"
    assert result.mapped > 0
    # 无 lineage 写入(没有 dataset_version 可查)
    (n,) = landing.con.execute(
        "SELECT COUNT(*) FROM d2a_field_lineage"
    ).fetchone()
    assert n == 0


# ---- 候选表与 lineage 原子性 ------------------------------------------------------


def test_build_dataset_second_build_independent(landing, pack):
    """第二次 build 产生新 dataset_version 和独立 lineage。"""
    r1 = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert r1.outcome == "ok"

    # 发布第一个
    from data2agent.connect.dataset_publish import publish_dataset
    pub = publish_dataset(landing, r1.dataset_version)
    assert pub.outcome == "ok"

    r2 = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert r2.outcome == "ok"
    assert r2.dataset_version != r1.dataset_version

    # 两个版本的 lineage 独立
    c1 = sum(
        landing.count_field_lineage(r1.dataset_version, o.object)
        for o in landing.list_object_versions(r1.dataset_version)
    )
    c2 = sum(
        landing.count_field_lineage(r2.dataset_version, o.object)
        for o in landing.list_object_versions(r2.dataset_version)
    )
    assert c1 > 0
    assert c2 > 0


# ---- transform_kind 和 steps 正确性 -----------------------------------------------


def test_lineage_transform_kind_and_steps(landing, pack):
    """lineage 节点的 transform_kind 和 steps 与实际转换一致。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    ds_version = result.dataset_version
    assert ds_version is not None

    rows = landing.con.execute(
        "SELECT * FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND trace_status = 'available' LIMIT 20",
        (ds_version,),
    ).fetchall()
    assert len(rows) > 0

    for r in rows:
        assert r["transform_kind"] in ("direct", "derived", "unmapped")
        steps = json.loads(r["transform_steps_json"])
        assert isinstance(steps, list)
        if r["transform_kind"] == "direct":
            kinds = {s["kind"] for s in steps}
            assert "read" in kinds
        elif r["transform_kind"] == "derived":
            kinds = {s["kind"] for s in steps}
            assert kinds & {"derived_rule", "derived_default"}


def test_lineage_unmapped_property(landing, pack):
    """未映射属性有 unmapped 节点和 property_unmapped reason。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    ds_version = result.dataset_version
    assert ds_version is not None

    rows = landing.con.execute(
        "SELECT * FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND transform_kind = 'unmapped' LIMIT 5",
        (ds_version,),
    ).fetchall()
    # 可能存在也可能不存在未映射属性;如果存在则 reason 正确
    for r in rows:
        assert r["unavailable_reason"] == "property_unmapped"
        assert r["trace_status"] == "unavailable"


# ---- result_value 与候选表一致 ----------------------------------------------------


def test_lineage_result_matches_candidate(landing, pack):
    """lineage result_value 与候选表实际值一致。"""
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    ds_version = result.dataset_version
    assert ds_version is not None

    obj = _lineage_object(landing, ds_version)
    tpl = next(t for t in pack.objects if t.object == obj.object)

    # 从 lineage 取一个 key_hash 和对应的 key_json
    sample = landing.con.execute(
        "SELECT DISTINCT object_key_hash, object_key_json "
        "FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND object = ? LIMIT 1",
        (ds_version, obj.object),
    ).fetchone()
    assert sample is not None
    token = sample["object_key_hash"]
    key_pairs = json.loads(sample["object_key_json"])
    key_values = {p[0]: p[1] for p in key_pairs}

    # 用业务键从候选表取对应行
    where = " AND ".join(f'"{k}" = ?' for k in tpl.keys)
    vals = [key_values[k] for k in tpl.keys]
    cand_rows = landing.con.execute(
        f'SELECT * FROM "{obj.build_table}" WHERE {where}', vals,
    ).fetchall()
    assert len(cand_rows) == 1
    cand = cand_rows[0]

    nodes = landing.get_field_lineage_by_key_hash(ds_version, obj.object, token)
    for node in nodes:
        prop = node["property"]
        ev = json.loads(node["result_value_json"])
        if node["trace_status"] == "available":
            cand_val = cand[prop]
            if ev["kind"] == "scalar":
                assert ev["value"] == cand_val or str(ev["value"]) == str(cand_val)
            elif ev["kind"] == "null":
                assert cand_val is None
