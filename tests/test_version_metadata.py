"""v0.3 M1:版本身份与数据集元数据基础。"""

import sqlite3

import pytest

from data2agent.shared.store.landing import LandingStore
from data2agent.shared.metamodel.schema import SourceBinding
from data2agent.shared.metamodel.versioning import (
    DatasetVersionRecord,
    ObjectVersionRecord,
    binding_hash,
    canonical_binding_json,
)


def _binding(**updates) -> SourceBinding:
    data = {
        "source": "digiwin_e10",
        "tables": ["SALES_ORDER", "CURRENCY"],
        "status": "draft",
        "key_map": {"order_no": "SALES_ORDER.DOC_NO"},
        "field_map": {
            "currency": "CURRENCY.CODE (join SALES_ORDER.CURRENCY_ID)",
            "amount": "SALES_ORDER.AMOUNT",
        },
        "derived": {
            "state": {
                "rules": [
                    {"when": {"INVALID_STATE": "Y"}, "value": "已作废"},
                    {"when": {"CLOSE_STATE": "C"}, "value": "已结案"},
                ],
                "default": "已接单",
            }
        },
        "watermark": "SALES_ORDER.LAST_MODIFIED_DATE",
        "notes": "人读说明",
    }
    data.update(updates)
    return SourceBinding(**data)


def test_binding_hash_is_canonical_and_ignores_notes():
    original = _binding()
    reordered = _binding(
        key_map={"order_no": "SALES_ORDER.DOC_NO"},
        field_map={
            "amount": "SALES_ORDER.AMOUNT",
            "currency": "CURRENCY.CODE (join SALES_ORDER.CURRENCY_ID)",
        },
        notes="重新排版后的现场说明",
    )

    assert canonical_binding_json(original) == canonical_binding_json(reordered)
    assert binding_hash(original) == binding_hash(reordered)
    assert binding_hash(original).startswith("sha256:")
    assert len(binding_hash(original)) == len("sha256:") + 64


@pytest.mark.parametrize(
    "change",
    [
        {"tables": ["SALES_ORDER"]},
        {"status": "verified"},
        {"key_map": {"order_no": "SALES_ORDER.ALT_DOC_NO"}},
        {"field_map": {"amount": "SALES_ORDER.NET_AMOUNT"}},
        {"watermark": "SALES_ORDER.UPDATED_AT"},
        {
            "derived": {
                "state": {
                    "rules": [
                        {"when": {"CLOSE_STATE": "C"}, "value": "已结案"},
                        {"when": {"INVALID_STATE": "Y"}, "value": "已作废"},
                    ],
                    "default": "已接单",
                }
            }
        },
    ],
)
def test_binding_hash_changes_when_runtime_semantics_change(change):
    assert binding_hash(_binding()) != binding_hash(_binding(**change))


def test_version_metadata_schema_is_idempotent(tmp_path):
    path = tmp_path / "landing.sqlite"
    first = LandingStore(path)
    first.con.execute('CREATE TABLE "obj_existing" (id TEXT PRIMARY KEY)')
    first.con.execute('INSERT INTO "obj_existing" VALUES ("kept")')
    first.con.commit()
    first.con.close()

    second = LandingStore(path)
    tables = {
        row[0]
        for row in second.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"d2a_dataset_version", "d2a_object_version"} <= tables
    assert second.con.execute('SELECT id FROM "obj_existing"').fetchone()[0] == "kept"


def test_version_metadata_constraints(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    con = store.con
    con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at, published_at) "
        "VALUES ('ds-1', 'digiwin_e10', '0.1.0', 'published', "
        "'2026-07-21T10:00:00', '2026-07-21T10:00:00')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO d2a_dataset_version "
            "(dataset_version, source, template_version, status, built_at, published_at) "
            "VALUES ('ds-2', 'digiwin_e10', '0.1.0', 'published', "
            "'2026-07-21T10:01:00', '2026-07-21T10:01:00')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO d2a_dataset_version "
            "(dataset_version, source, template_version, status, built_at) "
            "VALUES ('ds-bad', 'other', '0.1.0', 'unknown', "
            "'2026-07-21T10:02:00')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO d2a_dataset_version "
            "(dataset_version, source, template_version, status, built_at) "
            "VALUES ('ds-nopub', 'other', '0.1.0', 'published', "
            "'2026-07-21T10:03:00')"
        )

    object_row = (
        "ds-1",
        "Customer",
        "obj-1",
        "sha256:" + "a" * 64,
        10,
        "built",
        "2026-07-21T10:00:00",
    )
    con.execute(
        "INSERT INTO d2a_object_version "
        "(dataset_version, object, object_version, binding_hash, row_count, status, built_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        object_row,
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO d2a_object_version "
            "(dataset_version, object, object_version, binding_hash, row_count, status, built_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            object_row,
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO d2a_object_version "
            "(dataset_version, object, object_version, binding_hash, row_count, status, built_at) "
            "VALUES ('ds-1', 'Bad', 'obj-bad', 'sha256:x', -1, 'built', "
            "'2026-07-21T10:00:00')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO d2a_object_version "
            "(dataset_version, object, object_version, binding_hash, row_count, status, built_at) "
            "VALUES ('missing-dataset', 'Orphan', 'obj-orphan', 'sha256:x', 1, "
            "'built', '2026-07-21T10:00:00')"
        )

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO d2a_object_version "
            "(dataset_version, object, object_version, binding_hash, row_count, "
            "status, built_at) "
            "VALUES ('ds-1', 'SalesOrder', 'obj-nopub', 'sha256:y', 1, "
            "'published', '2026-07-21T10:00:00')"
        )


def _insert_dataset(con, *, version, source, status, built_at, **extra):
    cols = ["dataset_version", "source", "template_version", "status", "built_at"]
    vals = [version, source, "0.1.0", status, built_at]
    for key in ("published_at", "previous_dataset_version", "error", "object_manifest"):
        if key in extra:
            cols.append(key)
            vals.append(extra[key])
    placeholders = ", ".join("?" * len(cols))
    con.execute(
        f"INSERT INTO d2a_dataset_version ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def _insert_object(
    con,
    *,
    dataset_version,
    object_name,
    object_version,
    status,
    built_at,
    published_at=None,
):
    if status in ("published", "retired") and published_at is None:
        published_at = built_at
    con.execute(
        "INSERT INTO d2a_object_version "
        "(dataset_version, object, object_version, binding_hash, row_count, "
        "status, built_at, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dataset_version,
            object_name,
            object_version,
            "sha256:" + "b" * 64,
            3,
            status,
            built_at,
            published_at,
        ),
    )


def test_version_queries_empty_store_has_no_fabricated_versions(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    rows, total = store.list_dataset_versions(limit=50, offset=0)
    assert rows == [] and total == 0
    assert store.get_dataset_version("anything") is None
    assert store.get_published_dataset("digiwin_e10") is None
    assert store.list_object_versions("anything") == []


def test_version_queries_building_published_and_history(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    con = store.con
    _insert_dataset(
        con,
        version="ds-old",
        source="digiwin_e10",
        status="retired",
        built_at="2026-07-20T09:00:00",
        published_at="2026-07-20T09:05:00",
    )
    _insert_dataset(
        con,
        version="ds-pub",
        source="digiwin_e10",
        status="published",
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:05:00",
        previous_dataset_version="ds-old",
    )
    _insert_dataset(
        con,
        version="ds-building",
        source="digiwin_e10",
        status="building",
        built_at="2026-07-21T11:00:00",
    )
    _insert_dataset(
        con,
        version="ds-other",
        source="other_erp",
        status="published",
        built_at="2026-07-21T12:00:00",
        published_at="2026-07-21T12:01:00",
    )
    _insert_object(
        con,
        dataset_version="ds-pub",
        object_name="Customer",
        object_version="obj-cust-1",
        status="published",
        built_at="2026-07-21T10:00:00",
    )
    _insert_object(
        con,
        dataset_version="ds-pub",
        object_name="SalesOrder",
        object_version="obj-so-1",
        status="published",
        built_at="2026-07-21T10:01:00",
    )
    con.commit()

    rows, total = store.list_dataset_versions(limit=50, offset=0)
    assert total == 4
    assert [r.dataset_version for r in rows] == [
        "ds-other",
        "ds-building",
        "ds-pub",
        "ds-old",
    ]
    assert all(isinstance(r, DatasetVersionRecord) for r in rows)

    filtered, filtered_total = store.list_dataset_versions(
        source="digiwin_e10", status="published", limit=10, offset=0
    )
    assert filtered_total == 1
    assert filtered[0].dataset_version == "ds-pub"
    assert filtered[0].previous_dataset_version == "ds-old"

    published = store.get_published_dataset("digiwin_e10")
    assert published is not None
    assert published.dataset_version == "ds-pub"
    assert published.status == "published"

    detail = store.get_dataset_version("ds-building")
    assert detail is not None
    assert detail.status == "building"
    assert detail.published_at is None

    objects = store.list_object_versions("ds-pub")
    assert [o.object for o in objects] == ["SalesOrder", "Customer"]
    assert all(isinstance(o, ObjectVersionRecord) for o in objects)
    assert objects[0].object_version == "obj-so-1"
    assert objects[0].binding_hash.startswith("sha256:")

    page, page_total = store.list_dataset_versions(limit=2, offset=1)
    assert page_total == 4
    assert [r.dataset_version for r in page] == ["ds-building", "ds-pub"]
