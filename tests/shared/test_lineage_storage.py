"""v0.3 M4-T04: lineage 存储与迁移。

门禁:新旧库重复迁移;不变量(PK/UNIQUE/CHECK/FK/freeze/immutability trigger);
hash 冲突让构建失败;BLOB/长值 ValueEvidence;只读打开;
insert/delete/count/query 与 update_object_lineage_meta 正确。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from data2agent.shared.store.field_lineage import (
    LINEAGE_SCHEMA_VERSION,
    FieldLineageInputRow,
    FieldLineageNode,
    canonical_object_key_json,
    dumps_json,
    dumps_value_evidence,
    encode_value_evidence,
    object_key_token,
)
from data2agent.shared.store.landing import LandingStore
from data2agent.shared.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord

SOURCE = "test_src"
OBJ = "SalesOrderLine"
KEYS = ["order_no", "line_no"]


# ---- helpers ----------------------------------------------------------------


def _make_landing(tmp_path: Path) -> LandingStore:
    return LandingStore(tmp_path / "landing.sqlite")


def _get_obj_version(
    landing: LandingStore, dataset_version: str, object_name: str,
) -> ObjectVersionRecord | None:
    for rec in landing.list_object_versions(dataset_version):
        if rec.object == object_name:
            return rec
    return None


def _seed_building(
    landing: LandingStore,
    dataset_version: str = "ds-001",
    object_name: str = OBJ,
    object_version: str = "obj-001",
) -> None:
    """插入 dataset + building object version,为 lineage 写入提供 FK 父行。"""
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version=dataset_version,
            source=SOURCE,
            template_version="tv-001",
            status="building",
            built_at="2026-07-22T10:00:00",
            object_manifest=f'["{object_name}"]',
            template_snapshot="{}",
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version=dataset_version,
            object=object_name,
            object_version=object_version,
            binding_hash="sha256:" + "ab" * 32,
            row_count=0,
            build_table=f"objv_{object_name}_001",
            status="building",
            built_at="2026-07-22T10:00:00",
        )
    )


def _node(
    *,
    dataset_version: str = "ds-001",
    object_version: str = "obj-001",
    object_name: str = OBJ,
    key_values: dict | None = None,
    property: str = "status",
    result_value: object = "ok",
    trace_status: str = "available",
    unavailable_reason: str | None = None,
    transform_kind: str = "direct",
    transform_steps: list | None = None,
    source: str = SOURCE,
    map_batch_id: str = "batch-001",
    binding_hash: str = "sha256:" + "ab" * 32,
    binding_status: str = "verified",
    template_version: str = "tv-001",
) -> FieldLineageNode:
    kv = key_values or {"order_no": "SO-001", "line_no": 10}
    key_json = canonical_object_key_json(KEYS, kv)
    key_hash = object_key_token(KEYS, kv)
    return FieldLineageNode(
        dataset_version=dataset_version,
        object_version=object_version,
        object=object_name,
        object_key_json=key_json,
        object_key_hash=key_hash,
        property=property,
        result_value_json=dumps_value_evidence(result_value),
        trace_status=trace_status,
        unavailable_reason=unavailable_reason,
        transform_kind=transform_kind,
        transform_steps_json=dumps_json(transform_steps or []),
        source=source,
        map_batch_id=map_batch_id,
        binding_hash=binding_hash,
        binding_status=binding_status,
        template_version=template_version,
    )


def _input(
    *,
    dataset_version: str = "ds-001",
    object_name: str = OBJ,
    key_values: dict | None = None,
    property: str = "status",
    input_ordinal: int = 0,
    role: str = "value",
    source: str | None = SOURCE,
    source_table: str | None = "SALES_ORDER",
    source_pk: dict | None = None,
    source_column: str | None = "STATUS",
    source_value: object = "O",
    extract_batch_id: str | None = "batch-001",
    join: dict | None = None,
) -> FieldLineageInputRow:
    kv = key_values or {"order_no": "SO-001", "line_no": 10}
    key_json = canonical_object_key_json(KEYS, kv)
    return FieldLineageInputRow(
        dataset_version=dataset_version,
        object=object_name,
        object_key_json=key_json,
        property=property,
        input_ordinal=input_ordinal,
        role=role,
        source=source,
        source_table=source_table,
        source_pk_json=dumps_json(source_pk or {"Id": 1}),
        source_column=source_column,
        source_value_json=dumps_value_evidence(source_value),
        extract_batch_id=extract_batch_id,
        join_json=dumps_json(join) if join else None,
    )


# ---- 迁移幂等 ---------------------------------------------------------------


def test_new_db_creates_lineage_tables(tmp_path):
    landing = _make_landing(tmp_path)
    tables = {
        r[0]
        for r in landing.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "d2a_field_lineage" in tables
    assert "d2a_field_lineage_input" in tables


def test_migration_idempotent(tmp_path):
    """重复打开同一库不报错、不丢表。"""
    db = tmp_path / "landing.sqlite"
    LandingStore(db)
    landing2 = LandingStore(db)
    tables = {
        r[0]
        for r in landing2.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "d2a_field_lineage" in tables
    assert "d2a_field_lineage_input" in tables


def test_old_db_migration_adds_columns(tmp_path):
    """模拟旧库(无 lineage 列)打开后幂等补列。"""
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

    landing = LandingStore(db)
    cols = {
        r[1]
        for r in landing.con.execute("PRAGMA table_info(d2a_object_version)")
    }
    assert "lineage_schema_version" in cols
    assert "lineage_field_count" in cols
    assert "purged_at" in cols

    # 再开一次仍幂等
    landing2 = LandingStore(db)
    cols2 = {
        r[1]
        for r in landing2.con.execute("PRAGMA table_info(d2a_object_version)")
    }
    assert cols == cols2


def test_readonly_open_succeeds(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.insert_field_lineage([_node()], [_input()])
    landing.con.close()

    ro = LandingStore.open_readonly(tmp_path / "landing.sqlite")
    rows = ro.get_field_lineage_by_key_hash(
        "ds-001", OBJ, object_key_token(KEYS, {"order_no": "SO-001", "line_no": 10})
    )
    assert len(rows) == 1
    ro.con.close()


# ---- 不变量约束 ---------------------------------------------------------------


def test_duplicate_node_pk_rejected(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    node = _node()
    landing.insert_field_lineage([node], [])
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([node], [])


def test_hash_collision_unique_rejected(tmp_path):
    """同 dataset/object/hash/property 但不同 key_json → UNIQUE 冲突。"""
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    kv = {"order_no": "SO-001", "line_no": 10}
    key_json = canonical_object_key_json(KEYS, kv)
    key_hash = object_key_token(KEYS, kv)

    n1 = _node(key_values=kv)
    # 伪造同 hash 不同 key_json(实际不可能,但约束必须 fail-closed)
    n2 = FieldLineageNode(
        dataset_version="ds-001",
        object_version="obj-001",
        object=OBJ,
        object_key_json='[["order_no","FAKE"],["line_no",99]]',
        object_key_hash=key_hash,
        property="status",
        result_value_json=dumps_value_evidence("x"),
        trace_status="available",
        unavailable_reason=None,
        transform_kind="direct",
        transform_steps_json="[]",
        source=SOURCE,
        map_batch_id="b",
        binding_hash="h",
        binding_status="verified",
        template_version="tv",
    )
    landing.insert_field_lineage([n1], [])
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([n2], [])


def test_check_trace_status(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    bad = _node(trace_status="maybe")
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([bad], [])


def test_check_transform_kind(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    bad = _node(transform_kind="magic")
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([bad], [])


def test_check_input_role(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.insert_field_lineage([_node()], [])
    bad_input = _input(role="bad_role")
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([], [bad_input])


def test_check_input_ordinal_non_negative(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.insert_field_lineage([_node()], [])
    bad_input = _input(input_ordinal=-1)
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([], [bad_input])


def test_fk_input_requires_node(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.con.execute("PRAGMA foreign_keys = ON")
    orphan = _input()
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([], [orphan])


def test_fk_node_requires_object_version(tmp_path):
    landing = _make_landing(tmp_path)
    # 不 seed building → 无 object_version 父行
    landing.con.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        landing.insert_field_lineage([_node()], [])


def test_immutable_node_trigger(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.insert_field_lineage([_node()], [])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        landing.con.execute(
            "UPDATE d2a_field_lineage SET result_value_json = '{}' "
            "WHERE dataset_version = 'ds-001'"
        )


def test_immutable_input_trigger(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.insert_field_lineage([_node()], [_input()])
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        landing.con.execute(
            "UPDATE d2a_field_lineage_input SET source_column = 'X' "
            "WHERE dataset_version = 'ds-001'"
        )


# ---- freeze trigger ----------------------------------------------------------


def test_freeze_lineage_meta_after_building(tmp_path):
    """离开 building 后 lineage_schema_version / lineage_field_count 不可变。"""
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.update_object_lineage_meta(
        "ds-001", OBJ,
        lineage_schema_version=LINEAGE_SCHEMA_VERSION,
        lineage_field_count=5,
    )
    # 标记 built → 离开 building
    landing.con.execute(
        "UPDATE d2a_object_version SET status = 'built' "
        "WHERE dataset_version = 'ds-001' AND object = ?",
        (OBJ,),
    )
    landing.con.commit()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        landing.con.execute(
            "UPDATE d2a_object_version SET lineage_field_count = 99 "
            "WHERE dataset_version = 'ds-001' AND object = ?",
            (OBJ,),
        )


def test_freeze_lineage_schema_version_after_building(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.update_object_lineage_meta(
        "ds-001", OBJ,
        lineage_schema_version=LINEAGE_SCHEMA_VERSION,
        lineage_field_count=3,
    )
    landing.con.execute(
        "UPDATE d2a_object_version SET status = 'built' "
        "WHERE dataset_version = 'ds-001' AND object = ?",
        (OBJ,),
    )
    landing.con.commit()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        landing.con.execute(
            "UPDATE d2a_object_version SET lineage_schema_version = 999 "
            "WHERE dataset_version = 'ds-001' AND object = ?",
            (OBJ,),
        )


# ---- update_object_lineage_meta -----------------------------------------------


def test_update_lineage_meta_building_only(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.update_object_lineage_meta(
        "ds-001", OBJ,
        lineage_schema_version=LINEAGE_SCHEMA_VERSION,
        lineage_field_count=10,
    )
    rec = _get_obj_version(landing, "ds-001", OBJ)
    assert rec is not None
    assert rec.lineage_schema_version == LINEAGE_SCHEMA_VERSION
    assert rec.lineage_field_count == 10


def test_update_lineage_meta_fails_for_non_building(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.con.execute(
        "UPDATE d2a_object_version SET status = 'built' "
        "WHERE dataset_version = 'ds-001' AND object = ?",
        (OBJ,),
    )
    landing.con.commit()
    with pytest.raises(ValueError, match="无法写入"):
        landing.update_object_lineage_meta(
            "ds-001", OBJ,
            lineage_schema_version=LINEAGE_SCHEMA_VERSION,
            lineage_field_count=5,
        )


def test_update_lineage_meta_negative_count_rejected(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    with pytest.raises(ValueError, match="不得为负"):
        landing.update_object_lineage_meta(
            "ds-001", OBJ,
            lineage_schema_version=LINEAGE_SCHEMA_VERSION,
            lineage_field_count=-1,
        )


def test_update_lineage_meta_missing_object(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    with pytest.raises(ValueError, match="无法写入"):
        landing.update_object_lineage_meta(
            "ds-001", "NoSuchObject",
            lineage_schema_version=LINEAGE_SCHEMA_VERSION,
            lineage_field_count=1,
        )


# ---- insert / count / query round-trip ----------------------------------------


def test_insert_and_count_roundtrip(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    n1 = _node(property="status", result_value="ok")
    n2 = _node(property="customer", result_value="C001")
    i1 = _input(property="status", input_ordinal=0)
    landing.insert_field_lineage([n1, n2], [i1])
    assert landing.count_field_lineage("ds-001", OBJ) == 2


def test_query_by_key_hash_returns_all_properties(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    kv = {"order_no": "SO-001", "line_no": 10}
    n1 = _node(key_values=kv, property="status")
    n2 = _node(key_values=kv, property="customer")
    landing.insert_field_lineage([n1, n2], [])
    token = object_key_token(KEYS, kv)
    rows = landing.get_field_lineage_by_key_hash("ds-001", OBJ, token)
    assert len(rows) == 2
    props = {r["property"] for r in rows}
    assert props == {"status", "customer"}


def test_query_by_key_hash_property_filter(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    kv = {"order_no": "SO-001", "line_no": 10}
    n1 = _node(key_values=kv, property="status")
    n2 = _node(key_values=kv, property="customer")
    landing.insert_field_lineage([n1, n2], [])
    token = object_key_token(KEYS, kv)
    rows = landing.get_field_lineage_by_key_hash(
        "ds-001", OBJ, token, property_name="status"
    )
    assert len(rows) == 1
    assert rows[0]["property"] == "status"


def test_query_by_key_hash_no_match(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.insert_field_lineage([_node()], [])
    rows = landing.get_field_lineage_by_key_hash(
        "ds-001", OBJ, "0" * 64
    )
    assert rows == []


def test_query_inputs_by_key_hash(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    kv = {"order_no": "SO-001", "line_no": 10}
    node = _node(key_values=kv, property="status")
    i0 = _input(key_values=kv, property="status", input_ordinal=0, role="value")
    i1 = _input(
        key_values=kv, property="status", input_ordinal=1,
        role="derived_condition", source_column="COND",
    )
    landing.insert_field_lineage([node], [i0, i1])
    token = object_key_token(KEYS, kv)
    rows = landing.get_field_lineage_inputs_by_key_hash("ds-001", OBJ, token)
    assert len(rows) == 2
    assert [r["input_ordinal"] for r in rows] == [0, 1]
    assert [r["role"] for r in rows] == ["value", "derived_condition"]


def test_query_inputs_by_key_hash_property_filter(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    kv = {"order_no": "SO-001", "line_no": 10}
    n1 = _node(key_values=kv, property="status")
    n2 = _node(key_values=kv, property="customer")
    i1 = _input(key_values=kv, property="status", input_ordinal=0)
    i2 = _input(key_values=kv, property="customer", input_ordinal=0)
    landing.insert_field_lineage([n1, n2], [i1, i2])
    token = object_key_token(KEYS, kv)
    rows = landing.get_field_lineage_inputs_by_key_hash(
        "ds-001", OBJ, token, property_name="customer"
    )
    assert len(rows) == 1
    assert rows[0]["property"] == "customer"


def test_composite_key_distinct_records(tmp_path):
    """同一 order_no 不同 line_no 是不同记录。"""
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    kv1 = {"order_no": "SO-001", "line_no": 10}
    kv2 = {"order_no": "SO-001", "line_no": 20}
    n1 = _node(key_values=kv1, property="status")
    n2 = _node(key_values=kv2, property="status")
    landing.insert_field_lineage([n1, n2], [])
    assert landing.count_field_lineage("ds-001", OBJ) == 2
    t1 = object_key_token(KEYS, kv1)
    t2 = object_key_token(KEYS, kv2)
    assert t1 != t2
    assert len(landing.get_field_lineage_by_key_hash("ds-001", OBJ, t1)) == 1
    assert len(landing.get_field_lineage_by_key_hash("ds-001", OBJ, t2)) == 1


# ---- delete -------------------------------------------------------------------


def test_delete_by_dataset_and_object(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.insert_field_lineage(
        [_node(property="status"), _node(property="customer")],
        [_input(property="status")],
    )
    deleted = landing.delete_field_lineage("ds-001", OBJ)
    assert deleted == 2
    assert landing.count_field_lineage("ds-001", OBJ) == 0
    # inputs 也被清理
    kv = {"order_no": "SO-001", "line_no": 10}
    token = object_key_token(KEYS, kv)
    assert landing.get_field_lineage_inputs_by_key_hash("ds-001", OBJ, token) == []


def test_delete_by_dataset_only(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    # 在同一 dataset 下追加第二个 object version
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-001",
            object="Customer",
            object_version="obj-002",
            binding_hash="sha256:" + "cd" * 32,
            row_count=0,
            build_table="objv_Customer_002",
            status="building",
            built_at="2026-07-22T10:00:00",
        )
    )
    landing.insert_field_lineage([_node()], [])
    cust_key = canonical_object_key_json(["customer_code"], {"customer_code": "C1"})
    cust_hash = object_key_token(["customer_code"], {"customer_code": "C1"})
    landing.insert_field_lineage(
        [FieldLineageNode(
            dataset_version="ds-001",
            object_version="obj-002",
            object="Customer",
            object_key_json=cust_key,
            object_key_hash=cust_hash,
            property="name",
            result_value_json=dumps_value_evidence("Alice"),
            trace_status="available",
            unavailable_reason=None,
            transform_kind="direct",
            transform_steps_json="[]",
            source=SOURCE,
            map_batch_id="b",
            binding_hash="h",
            binding_status="verified",
            template_version="tv",
        )],
        [],
    )
    deleted = landing.delete_field_lineage("ds-001")
    assert deleted == 2
    assert landing.count_field_lineage("ds-001", OBJ) == 0
    assert landing.count_field_lineage("ds-001", "Customer") == 0


def test_delete_nonexistent_returns_zero(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    assert landing.delete_field_lineage("ds-001", OBJ) == 0


# ---- ValueEvidence --------------------------------------------------------------


def test_value_evidence_null():
    ev = encode_value_evidence(None)
    assert ev["kind"] == "null"
    assert ev["value"] is None


def test_value_evidence_bool():
    ev = encode_value_evidence(True)
    assert ev["kind"] == "scalar"
    assert ev["value"] is True


def test_value_evidence_int():
    ev = encode_value_evidence(42)
    assert ev["kind"] == "scalar"
    assert ev["value"] == 42


def test_value_evidence_float():
    ev = encode_value_evidence(3.14)
    assert ev["kind"] == "scalar"
    assert ev["value"] == 3.14


def test_value_evidence_nan_truncated():
    ev = encode_value_evidence(float("nan"))
    assert ev["kind"] == "truncated"
    assert ev["sha256"] is not None


def test_value_evidence_inf_truncated():
    ev = encode_value_evidence(float("inf"))
    assert ev["kind"] == "truncated"


def test_value_evidence_datetime():
    dt = datetime(2026, 7, 22, 10, 30, 0)
    ev = encode_value_evidence(dt)
    assert ev["kind"] == "scalar"
    assert ev["value"] == "2026-07-22T10:30:00"


def test_value_evidence_date():
    d = date(2026, 7, 22)
    ev = encode_value_evidence(d)
    assert ev["kind"] == "scalar"
    assert ev["value"] == "2026-07-22"


def test_value_evidence_decimal():
    ev = encode_value_evidence(Decimal("123.45"))
    assert ev["kind"] == "scalar"
    assert ev["value"] == 123.45


def test_value_evidence_bytes():
    raw = b"\x00\x01\x02" * 100
    ev = encode_value_evidence(raw)
    assert ev["kind"] == "bytes"
    assert ev["value"] is None
    assert ev["length"] == 300
    assert ev["sha256"] is not None


def test_value_evidence_short_string():
    ev = encode_value_evidence("hello")
    assert ev["kind"] == "scalar"
    assert ev["value"] == "hello"


def test_value_evidence_long_string_truncated():
    long_text = "x" * 100_000
    ev = encode_value_evidence(long_text)
    assert ev["kind"] == "truncated"
    assert ev["value"] is None
    assert ev["length"] == 100_000
    assert len(ev["preview"]) == 512
    assert ev["sha256"] is not None


def test_value_evidence_json_roundtrip():
    """dumps → json.loads 可还原。"""
    for val in [None, True, 42, 3.14, "hello", datetime(2026, 1, 1)]:
        raw = dumps_value_evidence(val)
        parsed = json.loads(raw)
        assert parsed["kind"] in ("null", "scalar", "bytes", "truncated")


def test_blob_node_roundtrip(tmp_path):
    """BLOB 值写入节点后可查询,不复制全文。"""
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    blob_val = b"\xde\xad" * 50_000
    node = _node(property="attachment", result_value=blob_val)
    landing.insert_field_lineage([node], [])
    token = object_key_token(KEYS, {"order_no": "SO-001", "line_no": 10})
    rows = landing.get_field_lineage_by_key_hash(
        "ds-001", OBJ, token, property_name="attachment"
    )
    assert len(rows) == 1
    ev = json.loads(rows[0]["result_value_json"])
    assert ev["kind"] == "bytes"
    assert ev["length"] == 100_000
    assert ev["value"] is None


# ---- object_version lineage 列 -------------------------------------------------


def test_object_version_lineage_columns_default_null(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    rec = _get_obj_version(landing, "ds-001", OBJ)
    assert rec is not None
    assert rec.lineage_schema_version is None
    assert rec.lineage_field_count is None


def test_object_version_lineage_columns_persist(tmp_path):
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    landing.update_object_lineage_meta(
        "ds-001", OBJ,
        lineage_schema_version=LINEAGE_SCHEMA_VERSION,
        lineage_field_count=42,
    )
    # 重新打开验证持久化
    landing2 = LandingStore(tmp_path / "landing.sqlite")
    rec = _get_obj_version(landing2, "ds-001", OBJ)
    assert rec is not None
    assert rec.lineage_schema_version == LINEAGE_SCHEMA_VERSION
    assert rec.lineage_field_count == 42


# ---- 事务回滚 -------------------------------------------------------------------


def test_failed_insert_rolls_back_nodes_and_inputs(tmp_path):
    """insert_field_lineage 中部分失败时全部回滚(commit=False 由调用方控制)。"""
    landing = _make_landing(tmp_path)
    _seed_building(landing)
    good = _node(property="status")
    bad = _node(property="status")  # 同 PK → 冲突
    landing.insert_field_lineage([good], [])
    with pytest.raises(sqlite3.IntegrityError):
        landing.con.execute("SAVEPOINT t04")
        try:
            landing.insert_field_lineage([bad], [], commit=False)
        except Exception:
            landing.con.execute("ROLLBACK TO t04")
            raise
    # 原有数据不受影响
    assert landing.count_field_lineage("ds-001", OBJ) == 1
