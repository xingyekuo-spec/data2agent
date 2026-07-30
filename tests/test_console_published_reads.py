"""v0.3 M2-T09: Console 读取统一消费 published snapshot(与 MCP 同规则)。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.shared.config import load_config  # noqa: E402
from data2agent.shared.store.dataset_publish import (  # noqa: E402
    build_dataset,
    resolve_published_snapshot,
)
from data2agent.middle.extract.increment import incremental_sync  # noqa: E402
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore  # noqa: E402
from tests.helpers import whitelist_from_pack  # noqa: E402
from data2agent.platform.console.app import create_app  # noqa: E402
from data2agent.platform.console.contracts import (  # noqa: E402
    OverviewResponse,
    PipelineResponse,
)
from data2agent.platform.mcp_server.core import QueryService  # noqa: E402
from data2agent.shared.store.evidence import EvidenceContext  # noqa: E402
from data2agent.shared.metamodel.loader import load_pack  # noqa: E402
from tests.fixtures.e10.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TOKEN = "console-pub-secret"


def _ctx() -> EvidenceContext:
    return EvidenceContext(
        principal="test:console-published",
        session_id="test_session_console_published_0001",
        channel="demo",
    )


def _sync(dirpath: Path) -> tuple[LandingStore, object]:
    src = dirpath / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(dirpath / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    return landing, pack


def _publish(dirpath: Path) -> tuple[LandingStore, object, str]:
    landing, pack = _sync(dirpath)
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published and result.dataset_version
    return landing, pack, result.dataset_version


def _client(landing: LandingStore, *, token: str | None = None,
            cfg=None) -> TestClient:
    return TestClient(create_app(
        landing.db_path, ROOT / "templates", config=cfg, token=token))


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _authed_client(landing: LandingStore) -> TestClient:
    return _client(landing, token=TOKEN)


def _pipeline_cfg(tmp_path: Path, landing: LandingStore, src: Path) -> object:
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    sync_every: 30m\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n",
        encoding="utf-8",
    )
    return load_config(cfg_file)


# ---- Gate 1: Overview 统计来自 published snapshot ----


def test_overview_uses_published_snapshot_rows_and_times(tmp_path):
    landing, _pack, version = _publish(tmp_path)
    snap = resolve_published_snapshot(landing, SOURCE)
    expected_rows = sum(e.row_count for e in snap.objects.values())

    body = OverviewResponse.model_validate(
        _client(landing).get("/api/overview").json())
    assert body.versions.dataset == version
    assert body.versions.object == version
    assert body.summary.object_rows == expected_rows
    assert body.summary.materialized_objects == len(snap.objects)
    assert body.summary.data_updated_at is not None
    assert body.summary.data_updated_at.tzinfo is not None
    # 口径说明不得再宣称读 obj_*
    notes = {n.name: n for n in body.count_notes}
    assert "obj_*" not in notes["object_rows"].semantics
    assert "obj_*" not in notes["object_rows"].source


# ---- Gate 2: 无 published + 遗留 obj_* → Console 对象路径安全拒绝 ----


def test_console_rejects_legacy_obj_without_published(tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    landing.con.execute(
        'CREATE TABLE "obj_Customer" ('
        "customer_code TEXT, name TEXT, contact TEXT, "
        '"_d2a_mapped_at" TEXT, "_d2a_batch_id" TEXT)'
    )
    landing.con.execute(
        'INSERT INTO "obj_Customer" VALUES '
        '("LEGACY", "ShouldNotSee", "secret@x", "2026-07-21T10:00:00", "b1")'
    )
    landing.con.commit()

    client = _client(landing)
    overview = OverviewResponse.model_validate(client.get("/api/overview").json())
    assert overview.summary.object_rows is None
    assert overview.summary.materialized_objects == 0
    assert overview.versions.dataset is None
    cust = next(o for o in overview.objects if o.object == "Customer")
    assert cust.rows is None

    authed = _authed_client(landing)
    catalog = authed.get("/api/objects", headers=_auth()).json()
    customer = next(i for i in catalog if i["object"] == "Customer")
    assert customer["rows"] is None
    assert customer["version"] is None
    assert "LEGACY" not in str(catalog)

    rows = authed.get("/api/objects/Customer", headers=_auth())
    assert rows.status_code == 409
    detail = rows.json().get("detail", "")
    assert "obj_" not in detail
    assert "objv_" not in detail
    assert "SELECT" not in detail.upper()
    assert "LEGACY" not in detail


# ---- Gate 3: Pipeline — raw 超前且重建失败 → stale + 仍服务旧版 ----


def test_pipeline_stale_when_raw_ahead_after_failed_rebuild(tmp_path):
    landing, pack, version = _publish(tmp_path)
    src = tmp_path / "source.sqlite"
    cfg = _pipeline_cfg(tmp_path, landing, src)

    # raw 继续前进
    landing.con.execute(
        f'UPDATE "raw_{SOURCE}__CUSTOMER" SET "_d2a_extracted_at" = ?',
        ("2099-01-01T00:00:00",),
    )
    # 新构建失败(不发布)
    landing.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, finished_at, tables, rows,"
        " status, detail, run_type) "
        "VALUES (?, '2099-01-01T00:01:00', '2099-01-01T00:01:30', 5, 0,"
        " 'failed', '构建失败:熔断', 'apply')",
        (SOURCE,),
    )
    landing.con.commit()
    assert landing.get_published_dataset(SOURCE).dataset_version == version

    body = PipelineResponse.model_validate(
        _client(landing, cfg=cfg).get("/api/pipeline").json())
    nodes = {n.node: n for n in body.nodes}
    assert nodes["objects"].status == "stale"
    assert "对象层仍服务旧版本" in nodes["objects"].status_reason
    assert nodes["objects"].version == version
    # 不是 partial success
    assert nodes["objects"].status != "healthy"
    assert nodes["objects"].status != "warning" or "部分" not in (
        nodes["objects"].status_reason or "")


# ---- Gate 4: /api/objects 版本来自 object_version / snapshot ----


def test_objects_api_versions_from_object_version_rows(tmp_path):
    landing, _pack, _version = _publish(tmp_path)
    snap = resolve_published_snapshot(landing, SOURCE)

    items = _authed_client(landing).get("/api/objects", headers=_auth()).json()
    by_name = {i["object"]: i for i in items}
    for name, entry in snap.objects.items():
        assert by_name[name]["rows"] == entry.row_count
        assert by_name[name]["version"] == entry.object_version
        assert by_name[name]["version"] is not None
        assert not str(by_name[name]["version"]).startswith("obj_")


# ---- Gate 5: Console 与 MCP 同一 landing 指向同一 dataset_version ----


def test_console_and_mcp_share_dataset_version_after_publish(tmp_path):
    landing, _pack, version = _publish(tmp_path)

    overview = OverviewResponse.model_validate(
        _client(landing).get("/api/overview").json())
    assert overview.versions.dataset == version

    svc = QueryService(
        landing.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx(),
    )
    mcp = svc.query_objects("Customer", limit=1)
    assert mcp["meta"]["dataset_version"] == version
    assert mcp["meta"]["dataset_version"] == overview.versions.dataset

    catalog = _authed_client(landing).get("/api/objects", headers=_auth()).json()
    customer = next(i for i in catalog if i["object"] == "Customer")
    snap = resolve_published_snapshot(landing, SOURCE)
    assert customer["version"] == snap.objects["Customer"].object_version
