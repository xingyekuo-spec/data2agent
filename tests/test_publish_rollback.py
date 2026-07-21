"""v0.3 M2-T06: 原子 publish/rollback 与保留策略。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import (
    build_dataset,
    publish_dataset,
    resolve_published_snapshot,
    rollback_dataset,
)
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.sync import whitelist_from_pack
from data2agent.metamodel.dataset_publish_contract import make_build_table
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


def _stage(landing, pack):
    result = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert result.outcome == "ok" and result.ready
    return result


def test_first_publish_activates_candidate(landing, pack):
    staged = _stage(landing, pack)
    result = publish_dataset(landing, staged.dataset_version)
    assert result.outcome == "ok"
    assert result.executed is True
    assert result.dataset_version == staged.dataset_version

    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == staged.dataset_version
    assert pub.status == "published"
    assert pub.published_at
    objs = landing.list_object_versions(staged.dataset_version)
    assert all(o.status == "published" and o.published_at for o in objs)
    snap = resolve_published_snapshot(landing, SOURCE)
    assert snap.dataset_version == staged.dataset_version
    assert set(snap.objects) == set(o.object for o in objs)


def test_publish_idempotent_when_already_current(landing, pack):
    staged = _stage(landing, pack)
    first = publish_dataset(landing, staged.dataset_version)
    assert first.executed is True
    second = publish_dataset(landing, staged.dataset_version)
    assert second.outcome == "idempotent"
    assert second.executed is False
    assert second.dataset_version == staged.dataset_version
    assert landing.get_published_dataset(SOURCE).dataset_version == staged.dataset_version


def test_stale_previous_candidate_conflict_409(landing, pack):
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True

    # Build v2 while v1 is current (previous frozen to v1).
    v2 = _stage(landing, pack)
    assert v2.previous_dataset_version == v1.dataset_version

    # Simulate another version becoming current after v2 was staged.
    landing.update_dataset_lifecycle(
        v1.dataset_version, status="retired",
    )
    # Insert a sneaky published version that is not v2's previous.
    sneaky_table = make_build_table(SOURCE, "Customer", "aaaa1111bbbb")
    landing.con.execute(
        f'CREATE TABLE "{sneaky_table}" (customer_code TEXT PRIMARY KEY)'
    )
    landing.con.execute(f'INSERT INTO "{sneaky_table}" VALUES ("X")')
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-sneaky",
            source=SOURCE,
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T12:00:00",
            published_at="2026-07-21T12:00:00",
            previous_dataset_version=v1.dataset_version,
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-sneaky",
            object="Customer",
            object_version="obj-sneaky",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table=sneaky_table,
            status="published",
            built_at="2026-07-21T12:00:00",
            published_at="2026-07-21T12:00:00",
        )
    )

    result = publish_dataset(landing, v2.dataset_version)
    assert result.outcome == "conflict"
    assert result.reason_code == "stale_previous"
    assert result.http_status == 409
    assert landing.get_published_dataset(SOURCE).dataset_version == "ds-sneaky"
    assert landing.get_dataset_version(v2.dataset_version).status == "building"


def test_fault_inject_mid_txn_keeps_old_published(landing, pack):
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    v2 = _stage(landing, pack)

    calls = {"n": 0}
    orig = LandingStore.update_dataset_lifecycle

    def flaky(self, dataset_version, *, status, **kwargs):
        result = orig(self, dataset_version, status=status, **kwargs)
        calls["n"] += 1
        # After retiring the old published dataset, blow up before candidate activates.
        if status == "retired" and dataset_version == v1.dataset_version:
            raise RuntimeError("injected fault after retire")
        return result

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(LandingStore, "update_dataset_lifecycle", flaky)
    try:
        result = publish_dataset(landing, v2.dataset_version)
        assert result.outcome == "error"
        assert result.http_status == 500
        assert result.error_id
    finally:
        monkeypatch.undo()

    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == v1.dataset_version
    assert pub.status == "published"
    assert landing.get_dataset_version(v2.dataset_version).status == "building"
    snap = resolve_published_snapshot(landing, SOURCE)
    assert snap.dataset_version == v1.dataset_version
    for o in landing.list_object_versions(v1.dataset_version):
        assert o.status == "published"


def test_one_step_rollback_and_reverse(landing, pack):
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    v2 = _stage(landing, pack)
    assert publish_dataset(landing, v2.dataset_version).executed is True

    rolled = rollback_dataset(landing, v1.dataset_version)
    assert rolled.outcome == "ok"
    assert rolled.executed is True
    assert rolled.dataset_version == v1.dataset_version
    pub = landing.get_published_dataset(SOURCE)
    assert pub.dataset_version == v1.dataset_version
    assert pub.previous_dataset_version == v2.dataset_version
    assert landing.get_dataset_version(v2.dataset_version).status == "retired"
    snap = resolve_published_snapshot(landing, SOURCE)
    assert snap.dataset_version == v1.dataset_version

    reverse = rollback_dataset(landing, v2.dataset_version)
    assert reverse.executed is True
    assert reverse.dataset_version == v2.dataset_version
    pub2 = landing.get_published_dataset(SOURCE)
    assert pub2.dataset_version == v2.dataset_version
    assert pub2.previous_dataset_version == v1.dataset_version


def test_rollback_idempotent_when_target_already_current(landing, pack):
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    again = rollback_dataset(landing, v1.dataset_version)
    assert again.outcome == "idempotent"
    assert again.executed is False
    assert again.dataset_version == v1.dataset_version


def test_rollback_without_previous_conflicts(landing, pack):
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    result = rollback_dataset(landing, "ds-nobody")
    assert result.outcome in ("not_found", "conflict")
    assert result.http_status in (404, 409)
    # Explicit no-previous path: invent a retired peer that is not previous.
    peer_table = make_build_table(SOURCE, "Customer", "ccccdddd1111")
    landing.con.execute(
        f'CREATE TABLE "{peer_table}" (customer_code TEXT PRIMARY KEY)'
    )
    landing.con.execute(f'INSERT INTO "{peer_table}" VALUES ("P")')
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-peer",
            source=SOURCE,
            template_version=pack.version,
            status="retired",
            built_at="2026-07-21T08:00:00",
            published_at="2026-07-21T08:00:00",
            object_manifest='["Customer"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-peer",
            object="Customer",
            object_version="obj-peer",
            binding_hash="sha256:" + "cd" * 32,
            row_count=1,
            build_table=peer_table,
            status="retired",
            built_at="2026-07-21T08:00:00",
            published_at="2026-07-21T08:00:00",
        )
    )
    bad = rollback_dataset(landing, "ds-peer")
    assert bad.outcome == "conflict"
    assert bad.reason_code == "not_direct_previous"
    assert bad.http_status == 409
    assert landing.get_published_dataset(SOURCE).dataset_version == v1.dataset_version


def test_gc_keeps_current_and_previous_purges_older(landing, pack):
    versions = []
    for _ in range(3):
        staged = _stage(landing, pack)
        assert publish_dataset(landing, staged.dataset_version).executed is True
        versions.append(staged.dataset_version)

    v1, v2, v3 = versions
    pub = landing.get_published_dataset(SOURCE)
    assert pub.dataset_version == v3
    assert pub.previous_dataset_version == v2

    # v1 should be GC'd; v2 (previous) and v3 (current) kept.
    for obj in landing.list_object_versions(v1):
        assert obj.build_table is None
        assert obj.purged_at is not None
    for ver in (v2, v3):
        for obj in landing.list_object_versions(ver):
            assert obj.build_table
            assert obj.purged_at is None
            exists = landing.con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (obj.build_table,),
            ).fetchone()
            assert exists is not None

    snap = resolve_published_snapshot(landing, SOURCE)
    assert snap.dataset_version == v3


def test_gc_failure_does_not_undo_publish(landing, pack, monkeypatch):
    v1 = _stage(landing, pack)
    assert publish_dataset(landing, v1.dataset_version).executed is True
    v2 = _stage(landing, pack)
    assert publish_dataset(landing, v2.dataset_version).executed is True
    v3 = _stage(landing, pack)

    def boom(*_a, **_k):
        raise RuntimeError("gc explode")

    monkeypatch.setattr(
        "data2agent.connect.dataset_publish._gc_retired_physical_tables", boom,
    )
    result = publish_dataset(landing, v3.dataset_version)
    assert result.outcome == "ok"
    assert result.executed is True
    assert landing.get_published_dataset(SOURCE).dataset_version == v3.dataset_version
    snap = resolve_published_snapshot(landing, SOURCE)
    assert snap.dataset_version == v3.dataset_version


def test_auto_publish_via_build_dataset(landing, pack):
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.outcome == "ok"
    assert result.published is True
    assert result.status == "published"
    assert result.ready is False or result.dataset_version
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == result.dataset_version
    snap = resolve_published_snapshot(landing, SOURCE)
    assert snap.dataset_version == result.dataset_version
