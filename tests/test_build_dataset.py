"""v0.3 M2-T05: build_dataset 候选数据集编排。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.sync import whitelist_from_pack
from data2agent.metamodel.dataset_publish_contract import (
    evaluate_publish,
    is_dataset_ready,
    make_build_table,
)
from data2agent.metamodel.loader import load_pack
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord
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
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    return landing


def test_first_build_multi_object_ready(landing, pack):
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    assert result.status == "building"
    assert result.ready is True
    assert result.published is False
    assert result.previous_dataset_version is None
    assert landing.get_published_dataset(SOURCE) is None

    ds = landing.get_dataset_version(result.dataset_version)
    objs = landing.list_object_versions(result.dataset_version)
    assert ds is not None and ds.template_snapshot
    assert is_dataset_ready(ds, objs)
    assert all(o.status == "built" and o.build_table for o in objs)
    decision = evaluate_publish(
        candidate=ds, objects=objs, current_published=None,
    )
    assert decision.outcome == "execute"


def test_build_leaves_existing_published_untouched(landing, pack):
    pub_table = make_build_table(SOURCE, "Customer", "deadbeef0001")
    landing.con.execute(
        f'CREATE TABLE "{pub_table}" (customer_code TEXT PRIMARY KEY)'
    )
    landing.con.execute(f'INSERT INTO "{pub_table}" VALUES ("KEEP")')
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-pub",
            source=SOURCE,
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-pub",
            object="Customer",
            object_version="obj-keep",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table=pub_table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    assert result.previous_dataset_version == "ds-pub"
    assert landing.get_published_dataset(SOURCE).dataset_version == "ds-pub"
    (code,) = landing.con.execute(
        f'SELECT customer_code FROM "{pub_table}"'
    ).fetchone()
    assert code == "KEEP"


def test_any_object_failure_marks_dataset_failed(landing, pack):
    # Force Quotation breaker: >5% null business keys
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" '
        "SET DOC_NO = NULL WHERE Id <= 15"
    )
    landing.con.commit()

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "failed"
    assert result.status == "failed"
    assert result.ready is False
    ds = landing.get_dataset_version(result.dataset_version)
    objs = landing.list_object_versions(result.dataset_version)
    assert ds.status == "failed"
    assert any(o.object == "Quotation" and o.status == "failed" for o in objs)
    assert all(o.status == "failed" for o in objs)
    assert all(o.build_table is None for o in objs)
    candidate_tables = landing.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'objv_%'"
    ).fetchall()
    assert candidate_tables == []
    assert landing.get_published_dataset(SOURCE) is None
    decision = evaluate_publish(
        candidate=ds, objects=objs, current_published=None,
    )
    assert decision.outcome == "conflict"


def test_failure_does_not_touch_existing_published(landing, pack):
    pub_table = make_build_table(SOURCE, "Customer", "cafebabef00d")
    landing.con.execute(
        f'CREATE TABLE "{pub_table}" (customer_code TEXT PRIMARY KEY)'
    )
    landing.con.execute(f'INSERT INTO "{pub_table}" VALUES ("KEEP")')
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-pub",
            source=SOURCE,
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-pub",
            object="Customer",
            object_version="obj-keep",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table=pub_table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" '
        "SET DOC_NO = NULL WHERE Id <= 15"
    )
    landing.con.commit()

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "failed"
    assert landing.get_published_dataset(SOURCE).dataset_version == "ds-pub"
    (code,) = landing.con.execute(
        f'SELECT customer_code FROM "{pub_table}"'
    ).fetchone()
    assert code == "KEEP"
    pub = landing.get_dataset_version("ds-pub")
    assert pub is not None and pub.status == "published"
    assert pub.template_snapshot == pack.model_dump_json()


def test_empty_enabled_manifest_conflict(landing, pack):
    result = build_dataset(landing, pack, "no_such_source", auto_publish=False)
    assert result.outcome == "conflict"
    assert result.reason_code == "empty_manifest"
    assert result.dataset_version is None
    rows, total = landing.list_dataset_versions(source="no_such_source")
    assert total == 0 and rows == []


def test_enabled_binding_empty_field_map_rejected(landing, pack):
    mutated = pack.model_copy(deep=True)
    for tpl in mutated.objects:
        for binding in tpl.bindings:
            if binding.source == SOURCE and binding.enabled:
                binding.field_map = {}
                break
        else:
            continue
        break
    result = build_dataset(landing, mutated, SOURCE, auto_publish=False)
    assert result.outcome == "conflict"
    assert result.reason_code == "empty_field_map"
    assert result.dataset_version is None
    _, total = landing.list_dataset_versions(source=SOURCE)
    assert total == 0


def test_new_build_freezes_new_template_snapshot(landing, pack):
    old_snap = pack.model_dump_json()
    pub_table = make_build_table(SOURCE, "Customer", "feedface0001")
    landing.con.execute(
        f'CREATE TABLE "{pub_table}" (customer_code TEXT PRIMARY KEY)'
    )
    landing.con.execute(f'INSERT INTO "{pub_table}" VALUES ("KEEP")')
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-pub",
            source=SOURCE,
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot=old_snap,
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-pub",
            object="Customer",
            object_version="obj-keep",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table=pub_table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )

    new_pack = pack.model_copy(update={"version": "9.9.9"})
    result = build_dataset(landing, new_pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    candidate = landing.get_dataset_version(result.dataset_version)
    assert candidate is not None
    assert candidate.template_version == "9.9.9"
    assert candidate.template_snapshot == new_pack.model_dump_json()
    published = landing.get_dataset_version("ds-pub")
    assert published is not None
    assert published.template_version == pack.version
    assert published.template_snapshot == old_snap


def test_stale_building_recovered_on_next_build(landing, pack):
    orphan = make_build_table(SOURCE, "Customer", "abcdef012345")
    landing.con.execute(
        f'CREATE TABLE "{orphan}" (customer_code TEXT PRIMARY KEY)'
    )
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-stale",
            source=SOURCE,
            template_version=pack.version,
            status="building",
            built_at="2026-07-21T09:00:00",
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-stale",
            object="Customer",
            object_version="obj-stale",
            binding_hash="sha256:" + "cd" * 32,
            row_count=0,
            build_table=orphan,
            status="building",
            built_at="2026-07-21T09:00:00",
        )
    )

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    assert result.dataset_version != "ds-stale"
    stale = landing.get_dataset_version("ds-stale")
    assert stale is not None and stale.status == "failed"
    stale_objs = landing.list_object_versions("ds-stale")
    assert all(o.status == "failed" for o in stale_objs)
    assert all(o.build_table is None for o in stale_objs)
    exists = landing.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (orphan,),
    ).fetchone()
    assert exists is None


def test_active_build_run_blocks_new_candidate(landing, pack):
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-active",
            source=SOURCE,
            template_version=pack.version,
            status="building",
            built_at="2026-07-21T09:00:00",
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-active",
            object="Customer",
            object_version="obj-active",
            binding_hash="sha256:" + "ef" * 32,
            row_count=0,
            build_table=None,
            status="building",
            built_at="2026-07-21T09:00:00",
        )
    )
    run_id = landing.start_run(SOURCE, "apply")
    landing.set_run_dataset_version(run_id, "ds-active")

    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "conflict"
    assert result.reason_code == "active_build"
    assert result.dataset_version is None
    active = landing.get_dataset_version("ds-active")
    assert active is not None and active.status == "building"
    rows, total = landing.list_dataset_versions(source=SOURCE, status="building")
    assert total == 1 and rows[0].dataset_version == "ds-active"


def test_auto_publish_via_build_dataset(landing, pack):
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.outcome == "ok"
    assert result.published is True
    assert result.status == "published"
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == result.dataset_version
    objs = landing.list_object_versions(result.dataset_version)
    assert all(o.status == "published" for o in objs)


def test_building_claim_atomic_survives_concurrent_recover(landing, pack, monkeypatch):
    """building 与 running Run 同事务提交后,并发 recover 只能看到 active_build。"""
    from data2agent.connect import dataset_publish as dp

    recover_codes: list[str | None] = []
    saw_active_run: list[bool] = []
    real_claim = dp._claim_building_candidate

    def claim_then_recover(*args, **kwargs):
        run_id = real_claim(*args, **kwargs)
        other = LandingStore(landing.db_path)
        try:
            version = kwargs["dataset_version"]
            saw_active_run.append(dp._has_active_build_run(other, version))
            recover_codes.append(dp._recover_stale_building(other, SOURCE))
        finally:
            other.con.close()
        return run_id

    monkeypatch.setattr(dp, "_claim_building_candidate", claim_then_recover)
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok"
    assert result.status == "building"
    ds = landing.get_dataset_version(result.dataset_version)
    assert ds is not None and ds.status == "building"
    assert saw_active_run == [True]
    assert recover_codes == ["active_build"]
