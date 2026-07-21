"""v0.3 M2-T03: PublishedDatasetSnapshot 严格解析。"""

from __future__ import annotations

import json

import pytest

from data2agent.connect.dataset_publish import (
    PublishedSnapshotError,
    resolve_published_snapshot,
)
from data2agent.connect.landing import LandingStore
from data2agent.metamodel.dataset_publish_contract import make_build_table
from data2agent.metamodel.schema import ObjectTemplate, Property, TemplatePack
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord


def _pack(*names: str) -> TemplatePack:
    objects = [
        ObjectTemplate(
            object=name,
            display_name=name,
            domain="销售",
            source_of_truth="t",
            keys=["id"],
            properties=[Property(name="id", type="string")],
        )
        for name in names
    ]
    return TemplatePack(version="0.1.0", objects=objects, metrics=[])


def _seed_published(
    store: LandingStore,
    *,
    source: str,
    version: str,
    objects: list[str],
    pack: TemplatePack | None = None,
    rows: dict[str, list[tuple]] | None = None,
    build_tables: dict[str, str] | None = None,
) -> dict[str, str]:
    pack = pack or _pack(*objects)
    tables: dict[str, str] = {}
    for name in objects:
        table = (build_tables or {}).get(name) or make_build_table(
            source, name, f"{abs(hash(name + version)):012x}"[:12]
        )
        tables[name] = table
        store.con.execute(
            f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY, val TEXT)'
        )
        for rid, val in (rows or {}).get(name, [("1", "a")]):
            store.con.execute(
                f'INSERT INTO "{table}" (id, val) VALUES (?, ?)', (rid, val)
            )
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version=version,
            source=source,
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest=json.dumps(objects, ensure_ascii=False),
            template_snapshot=pack.model_dump_json(),
        )
    )
    for name in objects:
        n = store.con.execute(f'SELECT COUNT(*) FROM "{tables[name]}"').fetchone()[0]
        store.insert_object_version(
            ObjectVersionRecord(
                dataset_version=version,
                object=name,
                object_version=f"{version}-{name}",
                binding_hash="sha256:" + "ab" * 32,
                row_count=n,
                build_table=tables[name],
                status="published",
                built_at="2026-07-21T10:00:00",
                published_at="2026-07-21T10:05:00",
            )
        )
    return tables


def test_resolve_published_snapshot_happy_path(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    tables = _seed_published(
        store,
        source="src_a",
        version="ds-1",
        objects=["Customer", "Order"],
        rows={"Customer": [("1", "c1"), ("2", "c2")], "Order": [("1", "o1")]},
    )
    snap = resolve_published_snapshot(store, "src_a")
    assert snap.source == "src_a"
    assert snap.dataset_version == "ds-1"
    assert snap.template_version == "0.1.0"
    assert snap.template_pack.object_names() >= {"Customer", "Order"}
    assert set(snap.objects) == {"Customer", "Order"}
    assert snap.objects["Customer"].physical_table == tables["Customer"]
    assert snap.objects["Customer"].row_count == 2
    assert snap.objects["Order"].row_count == 1


def test_resolve_rejects_empty_and_missing_published(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    with pytest.raises(PublishedSnapshotError) as exc:
        resolve_published_snapshot(store, "src_a")
    assert exc.value.reason_code == "not_published"
    assert "obj_" not in str(exc.value)
    assert "objv_" not in str(exc.value)


def test_resolve_rejects_legacy_obj_without_published_meta(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.con.execute('CREATE TABLE "obj_Customer" (id TEXT)')
    store.con.execute('INSERT INTO "obj_Customer" VALUES ("x")')
    store.con.commit()
    with pytest.raises(PublishedSnapshotError) as exc:
        resolve_published_snapshot(store, "src_a")
    assert exc.value.reason_code == "not_published"


def test_resolve_rejects_corrupt_template_snapshot(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    table = make_build_table("src_a", "Customer", "aabbccddeeff")
    store.con.execute(f'CREATE TABLE "{table}" (id TEXT)')
    store.con.execute(f'INSERT INTO "{table}" VALUES ("1")')
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src_a",
            template_version="0.1.0",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot="{not-json",
        )
    )
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-1",
            object="Customer",
            object_version="obj-1",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table=table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    with pytest.raises(PublishedSnapshotError) as exc:
        resolve_published_snapshot(store, "src_a")
    assert exc.value.reason_code == "snapshot_corrupt"


def test_resolve_rejects_illegal_build_table_and_missing_table(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    pack = _pack("Customer")
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src_a",
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-1",
            object="Customer",
            object_version="obj-1",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table="obj_Customer",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    with pytest.raises(PublishedSnapshotError) as exc:
        resolve_published_snapshot(store, "src_a")
    assert exc.value.reason_code == "snapshot_corrupt"

    store2 = LandingStore(tmp_path / "landing2.sqlite")
    _seed_published(store2, source="src_a", version="ds-1", objects=["Customer"])
    # drop physical table after seed
    row = store2.list_object_versions("ds-1")[0]
    store2.con.execute(f'DROP TABLE "{row.build_table}"')
    store2.con.commit()
    with pytest.raises(PublishedSnapshotError) as exc2:
        resolve_published_snapshot(store2, "src_a")
    assert exc2.value.reason_code == "snapshot_corrupt"


def test_resolve_rejects_manifest_mismatch_and_rowcount(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    _seed_published(
        store, source="src_a", version="ds-1", objects=["Customer", "Order"]
    )
    # extra object row
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-1",
            object="Extra",
            object_version="obj-extra",
            binding_hash="sha256:" + "cd" * 32,
            row_count=0,
            build_table=make_build_table("src_a", "Extra", "112233445566"),
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    with pytest.raises(PublishedSnapshotError):
        resolve_published_snapshot(store, "src_a")

    store2 = LandingStore(tmp_path / "landing2.sqlite")
    tables2 = _seed_published(
        store2, source="src_a", version="ds-2", objects=["Customer"]
    )
    store2.con.execute(f'INSERT INTO "{tables2["Customer"]}" VALUES ("9", "x")')
    store2.con.commit()
    with pytest.raises(PublishedSnapshotError) as exc:
        resolve_published_snapshot(store2, "src_a")
    assert exc.value.reason_code == "snapshot_corrupt"


def test_resolve_rejects_missing_object_in_manifest(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    pack = _pack("Customer", "Order")
    table = make_build_table("src_a", "Customer", "aabbccddeeff")
    store.con.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY)')
    store.con.execute(f'INSERT INTO "{table}" VALUES ("1")')
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-1",
            source="src_a",
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer", "Order"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    store.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-1",
            object="Customer",
            object_version="obj-1",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table=table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    with pytest.raises(PublishedSnapshotError) as exc:
        resolve_published_snapshot(store, "src_a")
    assert exc.value.reason_code == "snapshot_corrupt"


def test_resolve_isolates_sources(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    tables_a = _seed_published(
        store,
        source="src_a",
        version="ds-a",
        objects=["Customer"],
        rows={"Customer": [("a", "from-a")]},
    )
    tables_b = _seed_published(
        store,
        source="src_b",
        version="ds-b",
        objects=["Customer"],
        rows={"Customer": [("b", "from-b")]},
    )
    snap_a = resolve_published_snapshot(store, "src_a")
    snap_b = resolve_published_snapshot(store, "src_b")
    assert snap_a.dataset_version == "ds-a"
    assert snap_b.dataset_version == "ds-b"
    assert snap_a.objects["Customer"].physical_table == tables_a["Customer"]
    assert snap_b.objects["Customer"].physical_table == tables_b["Customer"]
    assert snap_a.objects["Customer"].physical_table != snap_b.objects[
        "Customer"
    ].physical_table
