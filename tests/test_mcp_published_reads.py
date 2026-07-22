"""v0.3 M2-T08: MCP/指标读取统一消费 published snapshot。"""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset, resolve_published_snapshot
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.sync import whitelist_from_pack
from data2agent.mcp_server.core import MASK, QueryService
from data2agent.mcp_server.evidence import EvidenceContext
from data2agent.mcp_server.metrics_impl import registry
from data2agent.metamodel.dataset_publish_contract import make_build_table
from data2agent.metamodel.loader import load_pack
from data2agent.metamodel.schema import ObjectTemplate, Property, TemplatePack
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


def _ctx(session_id: str = "test_session_mcp_published_0001") -> EvidenceContext:
    return EvidenceContext(
        principal="test:mcp-published",
        session_id=session_id,
        channel="demo",
    )


def _sync_landing(dirpath: Path) -> tuple[LandingStore, object]:
    src = dirpath / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(dirpath / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    return landing, pack


def _publish(dirpath: Path) -> Path:
    landing, pack = _sync_landing(dirpath)
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published and result.dataset_version
    return dirpath / "landing.sqlite"


def _seed_minimal_customer(
    store: LandingStore,
    *,
    source: str,
    version: str,
    rows: list[tuple[str, str]],
    pack: TemplatePack | None = None,
    binding_hash: str = "sha256:" + "ab" * 32,
) -> str:
    pack = pack or TemplatePack(
        version="0.1.0",
        objects=[
            ObjectTemplate(
                object="Customer",
                display_name="客户",
                domain="销售",
                source_of_truth="t",
                keys=["customer_code"],
                properties=[
                    Property(name="customer_code", type="string"),
                    Property(name="name", type="string"),
                    Property(
                        name="contact", type="string", sensitive=True,
                    ),
                ],
                bindings=[],
            )
        ],
        metrics=[],
    )
    table = make_build_table(source, "Customer", f"{abs(hash(source + version)):012x}"[:12])
    store.con.execute(
        f'CREATE TABLE "{table}" ('
        "customer_code TEXT PRIMARY KEY, name TEXT, contact TEXT)"
    )
    for code, name in rows:
        store.con.execute(
            f'INSERT INTO "{table}" (customer_code, name, contact) VALUES (?, ?, ?)',
            (code, name, f"{code}@ex.com"),
        )
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version=version,
            source=source,
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
            dataset_version=version,
            object="Customer",
            object_version=f"{version}-Customer",
            binding_hash=binding_hash,
            row_count=len(rows),
            build_table=table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    return table


# ---- Gate 1: no published + legacy obj_* → not_published, no legacy read ----


def test_mcp_rejects_legacy_obj_without_published(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.con.execute(
        'CREATE TABLE "obj_Customer" ('
        "customer_code TEXT, name TEXT, contact TEXT)"
    )
    store.con.execute(
        'INSERT INTO "obj_Customer" VALUES ("LEGACY", "ShouldNotSee", "x@y.z")'
    )
    store.con.commit()

    svc = QueryService(store.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx())
    with pytest.raises(ValueError, match="not_published") as exc:
        svc.query_objects("Customer", limit=1)
    err = str(exc.value)
    assert "obj_" not in err
    assert "objv_" not in err
    assert "SELECT" not in err.upper()

    with pytest.raises(ValueError, match="not_published") as exc2:
        svc.query_metrics("gross_margin_rate")
    assert "obj_" not in str(exc2.value)


def test_query_execution_errors_do_not_leak_schema(tmp_path, monkeypatch):
    """Unexpected SQLite errors must not leak table/SQL fragments to MCP clients."""
    db = _publish(tmp_path)
    leak = "objv_should_not_appear_in_error_zzzz"
    monkeypatch.setattr(
        QueryService,
        "_object_sql",
        lambda self, *a, **k: (f'SELECT 1 FROM "{leak}" LIMIT 1', []),
    )
    svc = QueryService(db, ROOT / "templates", source=SOURCE, default_context=_ctx())
    with pytest.raises(ValueError, match="execution_failed") as exc:
        svc.query_objects("Customer", limit=1)
    err = str(exc.value)
    assert leak not in err
    assert "no such table" not in err.lower()
    assert "SELECT" not in err.upper()
    assert "objv_" not in err


# ---- Gate 2: multi-object metric uses one snapshot ----


def test_metric_uses_one_snapshot_for_all_dependencies(tmp_path):
    db = _publish(tmp_path)
    svc = QueryService(db, ROOT / "templates", source=SOURCE, default_context=_ctx())
    impl = registry(SOURCE)["gross_margin_rate"]
    assert impl is not None
    assert impl.depends_on == frozenset(
        {"SalesOrder", "SalesOrderLine", "Material", "Customer"}
    )

    store = LandingStore(db)
    snap = resolve_published_snapshot(store, SOURCE)
    res = svc.query_metrics("gross_margin_rate", group_by="月", limit=5)
    assert res["implemented"] is True
    meta = res["meta"]
    assert meta["dataset_version"] == snap.dataset_version
    assert meta["template_version"] == snap.template_version
    assert set(meta["binding_hashes"]) == set(impl.depends_on)
    for obj in impl.depends_on:
        assert meta["binding_hashes"][obj] == snap.objects[obj].binding_hash


# ---- Gate 3: template disk upgrade + failed rebuild → old published still served ----


def test_mcp_serves_frozen_template_after_failed_rebuild(tmp_path):
    templates = tmp_path / "templates"
    shutil.copytree(ROOT / "templates", templates)
    landing, pack = _sync_landing(tmp_path)
    first = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert first.published
    old_version = first.dataset_version

    svc = QueryService(landing.db_path, templates, source=SOURCE, default_context=_ctx())
    before = svc.query_objects("Customer", limit=3)
    assert before["meta"]["dataset_version"] == old_version
    assert before["meta"]["masked_fields"] == ["contact"]
    assert all(r["contact"] == MASK for r in before["rows"])

    # Destructive disk upgrade: drop sensitive flag and rename a property desc.
    cust_yaml = templates / "objects" / "customer.yaml"
    text = cust_yaml.read_text(encoding="utf-8")
    text = text.replace("sensitive: true", "sensitive: false")
    text = text.replace("联系方式", "联系方式(已升级)")
    cust_yaml.write_text(text, encoding="utf-8")

    # Break rebuild by wiping raw so mapping fails / empty.
    for row in landing.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'"
    ).fetchall():
        landing.con.execute(f'DROP TABLE "{row["name"]}"')
    landing.con.commit()

    upgraded = load_pack(templates)
    second = build_dataset(landing, upgraded, SOURCE, auto_publish=True)
    assert not second.published
    assert landing.get_published_dataset(SOURCE).dataset_version == old_version

    after = svc.query_objects("Customer", limit=3)
    assert after["meta"]["dataset_version"] == old_version
    assert after["meta"]["template_version"] == before["meta"]["template_version"]
    # Frozen snapshot still treats contact as sensitive.
    assert after["meta"]["masked_fields"] == ["contact"]
    assert all(r["contact"] == MASK for r in after["rows"])

    margin = svc.query_metrics("gross_margin_rate", limit=3)
    assert margin["meta"]["dataset_version"] == old_version
    assert margin["implemented"] is True
    assert margin["rows"]


def test_mcp_serves_object_removed_from_disk_template(tmp_path):
    """磁盘模板删除对象后,仍可按冻结快照查询 published 对象。"""
    landing, pack = _sync_landing(tmp_path)
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    version = result.dataset_version

    svc = QueryService(landing.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx())
    # 模拟升级后磁盘包不再声明 Customer(目录无此项),查询仍走冻结快照。
    svc.pack = svc.pack.model_copy(
        update={"objects": [o for o in svc.pack.objects if o.object != "Customer"]},
    )
    assert "Customer" not in svc.pack.object_names()
    rows = svc.query_objects("Customer", limit=2)
    assert rows["meta"]["dataset_version"] == version
    assert rows["rows"]
    assert rows["meta"]["masked_fields"] == ["contact"]


def test_mcp_serves_metric_removed_from_disk_template(tmp_path):
    """磁盘模板删除指标后,仍可按冻结快照查询 published 指标。"""
    landing, pack = _sync_landing(tmp_path)
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    version = result.dataset_version

    svc = QueryService(landing.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx())
    svc.pack = svc.pack.model_copy(
        update={
            "metrics": [m for m in svc.pack.metrics if m.metric != "gross_margin_rate"],
        },
    )
    assert all(m.metric != "gross_margin_rate" for m in svc.pack.metrics)
    margin = svc.query_metrics("gross_margin_rate", limit=2)
    assert margin["meta"]["dataset_version"] == version
    assert margin["implemented"] is True
    assert margin["rows"]


# ---- Gate 4: response metadata matches physical tables used ----


def test_query_meta_includes_version_and_binding_hashes(tmp_path):
    db = _publish(tmp_path)
    store = LandingStore(db)
    snap = resolve_published_snapshot(store, SOURCE)
    svc = QueryService(db, ROOT / "templates", source=SOURCE, default_context=_ctx())

    obj = svc.query_objects("Customer", limit=2)
    assert obj["meta"]["dataset_version"] == snap.dataset_version
    assert obj["meta"]["template_version"] == snap.template_version
    assert obj["meta"]["binding_hashes"] == {
        "Customer": snap.objects["Customer"].binding_hash,
    }
    assert "obj_Customer" not in json.dumps(obj, ensure_ascii=False)

    logged = svc._query_log[obj["meta"]["query_id"]]
    assert logged["dataset_version"] == snap.dataset_version

    metric = svc.query_metrics("quote_response_hours", group_by="客户", limit=3)
    assert metric["meta"]["dataset_version"] == snap.dataset_version
    assert set(metric["meta"]["binding_hashes"]) == {"Quotation", "Customer"}
    for name, h in metric["meta"]["binding_hashes"].items():
        assert h == snap.objects[name].binding_hash


# ---- Gate 5: same object names across sources stay isolated ----


def test_mcp_isolates_sources_with_same_object_names(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    table_a = _seed_minimal_customer(
        store,
        source="src_a",
        version="ds-a",
        rows=[("A1", "from-a")],
        binding_hash="sha256:" + "aa" * 32,
    )
    table_b = _seed_minimal_customer(
        store,
        source="src_b",
        version="ds-b",
        rows=[("B1", "from-b")],
        binding_hash="sha256:" + "bb" * 32,
    )
    assert table_a != table_b

    # Disk catalog only needs Customer; QueryService still loads real pack,
    # but published snapshot carries the minimal frozen template used for reads.
    svc_a = QueryService(
        store.db_path, ROOT / "templates", source="src_a",
        default_context=_ctx("test_session_mcp_src_a_0001"),
    )
    svc_b = QueryService(
        store.db_path, ROOT / "templates", source="src_b",
        default_context=_ctx("test_session_mcp_src_b_0001"),
    )

    # Snapshot packs lack digiwin bindings; object must still resolve via frozen pack.
    # Use a templates root that includes Customer so catalog/unknown checks pass.
    res_a = svc_a.query_objects("Customer", limit=10)
    res_b = svc_b.query_objects("Customer", limit=10)
    assert {r["customer_code"] for r in res_a["rows"]} == {"A1"}
    assert {r["customer_code"] for r in res_b["rows"]} == {"B1"}
    assert res_a["rows"][0]["name"] == "from-a"
    assert res_b["rows"][0]["name"] == "from-b"
    assert res_a["meta"]["dataset_version"] == "ds-a"
    assert res_b["meta"]["dataset_version"] == "ds-b"
    assert res_a["meta"]["binding_hashes"]["Customer"] == "sha256:" + "aa" * 32
    assert res_b["meta"]["binding_hashes"]["Customer"] == "sha256:" + "bb" * 32
    assert all(r["contact"] == MASK for r in res_a["rows"])
    assert all(r["contact"] == MASK for r in res_b["rows"])
