"""真实 Overview API 集成测试(M3-T04):空库 / 正常库 / 告警 / 无副作用。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.shared.store.dataset_publish import build_dataset  # noqa: E402
from data2agent.middle.extract.increment import incremental_sync  # noqa: E402
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore  # noqa: E402
from tests.helpers import whitelist_from_pack  # noqa: E402
from data2agent.platform.console.app import create_app  # noqa: E402
from data2agent.platform.console.contracts import OverviewResponse  # noqa: E402
from data2agent.shared.metamodel.loader import load_pack  # noqa: E402
from data2agent.shared.metamodel.versioning import (  # noqa: E402
    DatasetVersionRecord,
    ObjectVersionRecord,
    object_layer_fully_published,
)
from tests.fixtures.e10.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
ALL_OBJECTS = [
    "Customer", "DeadStockAttribution", "DeadStockItem", "DuplicateMaterialCandidate",
    "EcnChangeEvidence", "Material", "MaterialBomUsage", "MaterialOrderEvidence",
    "MaterialSubstituteCandidate", "ProductionLossEvidence", "PurchaseOverbuyEvidence",
    "Quotation", "SalesOrder", "SalesOrderLine", "SpecialConditionEvidence",
]


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE), audit_hook=hook)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    return landing


def _client(landing: LandingStore) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates"))


def _insert_dataset(con, *, version, source, status, built_at, published_at=None,
                    object_manifest=None, **extra):
    cols = ["dataset_version", "source", "template_version", "status", "built_at"]
    vals = [version, source, "0.1.0", status, built_at]
    if published_at is not None:
        cols.append("published_at")
        vals.append(published_at)
    if object_manifest is not None:
        cols.append("object_manifest")
        vals.append(json.dumps(object_manifest, ensure_ascii=False))
    for key in ("previous_dataset_version", "error"):
        if key in extra:
            cols.append(key)
            vals.append(extra[key])
    placeholders = ", ".join("?" * len(cols))
    con.execute(
        f"INSERT INTO d2a_dataset_version ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )


def _insert_published_objects(con, *, dataset_version: str, objects: list[str],
                              built_at: str = "2026-07-21T10:00:00"):
    for i, name in enumerate(objects):
        con.execute(
            "INSERT INTO d2a_object_version "
            "(dataset_version, object, object_version, binding_hash, row_count, "
            "status, built_at, published_at) "
            "VALUES (?, ?, ?, ?, 10, 'published', ?, ?)",
            (
                dataset_version,
                name,
                f"obj-{dataset_version}-{name}",
                "sha256:" + f"{i:x}".rjust(64, "0"),
                built_at,
                built_at,
            ),
        )


def test_overview_real_aggregation(env):
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    s = body.summary
    assert s.raw_rows is not None and s.raw_rows > 0
    assert s.object_rows is not None and s.object_rows > 0
    assert s.materialized_objects == 15
    assert s.template_objects == 15
    assert s.quarantine_pending == 0
    assert s.last_run_at is not None and s.last_run_at.tzinfo is not None
    assert s.data_updated_at is not None
    # 版本:app/template 真实;已原子发布后 dataset/object 指向同一 published 版本
    assert body.versions.app is not None
    assert body.versions.template
    assert body.versions.dataset is not None
    assert body.versions.object == body.versions.dataset
    # binding:当前模板全部 draft → info 告警,且 mapping 不显示为 healthy
    assert body.binding_summary.draft == 20
    kinds = {a.id for a in body.alerts}
    assert "binding-draft" in kinds
    # 最近运行带类型;趋势桶 <= 24 且时间有序
    assert {r.run_type for r in body.recent_runs} >= {"sync", "apply"}
    buckets = [p.bucket for p in body.sync_trend]
    assert len(buckets) <= 24
    assert buckets == sorted(buckets)
    assert sum(p.runs for p in body.sync_trend) >= 1


def test_overview_reads_published_dataset_versions(env):
    """有 published 元数据且冻结清单与对象层齐全时 overview 填真实版本。"""
    published = env.get_published_dataset(SOURCE)
    assert published is not None
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    assert body.versions.dataset == published.dataset_version
    assert body.versions.object == published.dataset_version


def test_overview_dataset_version_follows_default_source(tmp_path):
    """Dashboard 版本从固定 default_source(=digiwin_e10)读取发布数据集。"""
    landing = LandingStore(tmp_path / "landing.sqlite")
    _insert_dataset(
        landing.con, version="ds-main", source="digiwin_e10",
        status="published", built_at="2026-07-21T12:00:00",
        published_at="2026-07-21T12:00:00", object_manifest=["Customer"])
    _insert_published_objects(
        landing.con, dataset_version="ds-main", objects=["Customer"],
        built_at="2026-07-21T12:00:00")
    landing.con.commit()
    client = TestClient(create_app(landing.db_path, str(ROOT / "templates")))
    body = OverviewResponse.model_validate(client.get("/api/overview").json())
    assert body.versions.dataset == "ds-main"
    assert body.versions.object == "ds-main"


def test_overview_failed_objects_do_not_count_as_published_layer(env):
    """仅有 failed 对象记录时,Dashboard 不得宣称对象层已发布。"""
    con = env.con
    # 退役 fixture 已发布版本,改插不完整对象层
    old = env.get_published_dataset(SOURCE)
    assert old is not None
    con.execute(
        "UPDATE d2a_dataset_version SET status = 'retired' WHERE dataset_version = ?",
        (old.dataset_version,),
    )
    _insert_dataset(
        con,
        version="ds-pub",
        source=SOURCE,
        status="published",
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:05:00",
        object_manifest=["Customer"],
    )
    con.execute(
        "INSERT INTO d2a_object_version "
        "(dataset_version, object, object_version, binding_hash, row_count, "
        "status, built_at) "
        "VALUES ('ds-pub', 'Customer', 'obj-fail-1', ?, 0, 'failed', "
        "'2026-07-21T10:00:00')",
        ("sha256:" + "f" * 64,),
    )
    con.commit()
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    assert body.versions.dataset == "ds-pub"
    assert body.versions.object is None


def test_overview_mixed_object_status_is_not_fully_published(env):
    """同一数据集混合 published/failed 时不得展示统一对象层版本。"""
    con = env.con
    old = env.get_published_dataset(SOURCE)
    assert old is not None
    con.execute(
        "UPDATE d2a_dataset_version SET status = 'retired' WHERE dataset_version = ?",
        (old.dataset_version,),
    )
    _insert_dataset(
        con,
        version="ds-mixed",
        source=SOURCE,
        status="published",
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:05:00",
        object_manifest=ALL_OBJECTS,
    )
    _insert_published_objects(
        con,
        dataset_version="ds-mixed",
        objects=["Customer", "Material", "Quotation", "SalesOrderLine"],
    )
    con.execute(
        "INSERT INTO d2a_object_version "
        "(dataset_version, object, object_version, binding_hash, row_count, "
        "status, built_at) "
        "VALUES ('ds-mixed', 'SalesOrder', 'obj-so-fail', ?, 0, 'failed', "
        "'2026-07-21T10:00:00')",
        ("sha256:" + "a" * 64,),
    )
    con.commit()
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    assert body.versions.dataset == "ds-mixed"
    assert body.versions.object is None


def test_overview_missing_expected_object_is_not_fully_published(env):
    """冻结清单中缺失对象时不得展示统一对象层版本。"""
    con = env.con
    old = env.get_published_dataset(SOURCE)
    assert old is not None
    con.execute(
        "UPDATE d2a_dataset_version SET status = 'retired' WHERE dataset_version = ?",
        (old.dataset_version,),
    )
    _insert_dataset(
        con,
        version="ds-gap",
        source=SOURCE,
        status="published",
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:05:00",
        object_manifest=ALL_OBJECTS,
    )
    _insert_published_objects(
        con,
        dataset_version="ds-gap",
        objects=["Customer", "Material", "Quotation", "SalesOrder"],
    )
    con.commit()
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    assert body.versions.dataset == "ds-gap"
    assert body.versions.object is None


def test_overview_extra_failed_object_is_not_fully_published(env):
    """清单内对象均 published,但额外存在 failed 对象时仍不得展示。"""
    con = env.con
    old = env.get_published_dataset(SOURCE)
    assert old is not None
    con.execute(
        "UPDATE d2a_dataset_version SET status = 'retired' WHERE dataset_version = ?",
        (old.dataset_version,),
    )
    _insert_dataset(
        con,
        version="ds-extra",
        source=SOURCE,
        status="published",
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:05:00",
        object_manifest=ALL_OBJECTS,
    )
    _insert_published_objects(con, dataset_version="ds-extra", objects=ALL_OBJECTS)
    con.execute(
        "INSERT INTO d2a_object_version "
        "(dataset_version, object, object_version, binding_hash, row_count, "
        "status, built_at) "
        "VALUES ('ds-extra', 'LegacyExtra', 'obj-extra-fail', ?, 0, 'failed', "
        "'2026-07-21T10:00:00')",
        ("sha256:" + "b" * 64,),
    )
    con.commit()
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    assert body.versions.dataset == "ds-extra"
    assert body.versions.object is None


def test_overview_missing_manifest_is_fail_closed(env):
    """无冻结清单时即使有 published 对象行也不得展示对象层版本。"""
    con = env.con
    old = env.get_published_dataset(SOURCE)
    assert old is not None
    con.execute(
        "UPDATE d2a_dataset_version SET status = 'retired' WHERE dataset_version = ?",
        (old.dataset_version,),
    )
    _insert_dataset(
        con,
        version="ds-nomanifest",
        source=SOURCE,
        status="published",
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:05:00",
        object_manifest=None,
    )
    _insert_published_objects(
        con, dataset_version="ds-nomanifest", objects=["Customer"])
    con.commit()
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    assert body.versions.dataset == "ds-nomanifest"
    assert body.versions.object is None


def test_object_layer_fully_published_ignores_current_template():
    """完整性只看冻结清单与对象行,与当前模板/绑定无关。"""
    dataset = DatasetVersionRecord(
        dataset_version="ds-old",
        source="unknown_source_without_bindings",
        template_version="0.0.9",
        status="published",
        built_at="2026-01-01T00:00:00",
        published_at="2026-01-01T00:00:00",
        object_manifest=json.dumps(["OnlyObject"]),
    )
    rows = [
        ObjectVersionRecord(
            dataset_version="ds-old",
            object="OnlyObject",
            object_version="obj-1",
            binding_hash="sha256:" + "c" * 64,
            row_count=1,
            status="published",
            built_at="2026-01-01T00:00:00",
            published_at="2026-01-01T00:00:00",
        )
    ]
    assert object_layer_fully_published(dataset, rows) is True
    # 无清单 → fail-closed
    bare = dataset.model_copy(update={"object_manifest": None})
    assert object_layer_fully_published(bare, rows) is False


def test_overview_empty_db_is_empty_not_healthy(tmp_path):
    landing = LandingStore(tmp_path / "empty.sqlite")
    body = OverviewResponse.model_validate(_client(landing).get("/api/overview").json())
    assert body.summary.raw_rows == 0          # 无 raw 表是事实 0
    assert body.summary.object_rows is None    # 未物化为 null,不是 0
    assert body.summary.materialized_objects == 0
    assert body.recent_runs == []
    assert body.sync_trend == []
    assert body.summary.last_run_at is None


def test_overview_alerts_for_quarantine_and_draft(env):
    env.con.execute(
        "INSERT INTO d2a_quarantine (source, object, keys_json, reason, created_at) "
        "VALUES (?, 'Customer', '{}', 'bad', '2026-07-18T12:00:00')", (SOURCE,))
    env.con.commit()
    body = OverviewResponse.model_validate(_client(env).get("/api/overview").json())
    kinds = {a.id for a in body.alerts}
    assert "quarantine-pending" in kinds
    assert "binding-draft" in kinds


def test_overview_has_no_side_effects(env):
    before_q = env.con.execute("SELECT COUNT(*) FROM d2a_quarantine").fetchone()[0]
    before_runs = env.con.execute("SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0]
    _client(env).get("/api/overview")
    assert env.con.execute("SELECT COUNT(*) FROM d2a_quarantine").fetchone()[0] == before_q
    assert env.con.execute("SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0] == before_runs


def test_overview_raw_failure_is_null_not_partial(tmp_path):
    """任一源 raw 查询失败 → raw_rows 为 null;部分源的合计不得冒充总数。"""
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    # 正常 raw 表
    landing.con.execute(
        'CREATE TABLE "raw_digiwin_e10__T1" '
        '("K" TEXT PRIMARY KEY, "_d2a_extracted_at" TEXT, "_d2a_deleted_at" TEXT)')
    landing.con.execute(
        'INSERT INTO "raw_digiwin_e10__T1" VALUES (\'k0\', \'2026-07-18T11:30:00\', NULL)')
    # 坏 raw 表(缺 _d2a_deleted_at 列,查询必炸)
    landing.con.execute('CREATE TABLE "raw_digiwin_e10__BROKEN" ("K" TEXT PRIMARY KEY)')
    # 让 source 在 sync_state 中可见
    landing.con.execute(
        "INSERT INTO d2a_sync_state (source, table_name, watermark_col) "
        "VALUES ('digiwin_e10', 'T1', 'COL')")
    landing.con.commit()
    client = TestClient(create_app(landing.db_path, str(ROOT / "templates")))
    body = OverviewResponse.model_validate(client.get("/api/overview").json())
    assert body.summary.raw_rows is None  # 不是 1 行
    assert any("查询失败" in a.reason for a in body.alerts)
