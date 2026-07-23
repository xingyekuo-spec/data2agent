"""v0.3 M2-T07: CLI / scheduler / Console apply+retry 入口迁移到 build_dataset。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from tests.helpers import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import ApplyActionResult, RetryActionResult
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def synced(tmp_path, pack):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    return src, landing, pack


def _legacy_obj_tables(landing: LandingStore) -> list[str]:
    rows = landing.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'obj_%'"
    ).fetchall()
    return [r["name"] for r in rows if not str(r["name"]).startswith("objv_")]


def _cfg_file(tmp_path: Path, src: Path, landing: LandingStore) -> Path:
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n",
        encoding="utf-8",
    )
    return cfg


# ---- CLI apply ----


def test_cli_apply_default_publishes(synced, tmp_path, monkeypatch):
    from data2agent.connect import __main__ as cli

    _src, landing, _pack = synced
    monkeypatch.setattr(
        "sys.argv",
        [
            "connect", "apply",
            "--source", SOURCE,
            "--landing", landing.db_path,
            "--templates", str(ROOT / "templates"),
        ],
    )
    assert cli.main() == 0
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.status == "published"
    assert _legacy_obj_tables(landing) == []


def test_cli_apply_stage_only_does_not_publish(synced, monkeypatch):
    from data2agent.connect import __main__ as cli

    _src, landing, _pack = synced
    monkeypatch.setattr(
        "sys.argv",
        [
            "connect", "apply",
            "--source", SOURCE,
            "--landing", landing.db_path,
            "--templates", str(ROOT / "templates"),
            "--stage-only",
        ],
    )
    assert cli.main() == 0
    assert landing.get_published_dataset(SOURCE) is None
    building = landing.con.execute(
        "SELECT dataset_version, status FROM d2a_dataset_version "
        "WHERE source = ? ORDER BY dataset_version DESC LIMIT 1",
        (SOURCE,),
    ).fetchone()
    assert building is not None
    assert building["status"] == "building"
    assert _legacy_obj_tables(landing) == []


# ---- scheduler ----


def test_scheduler_auto_publishes_after_sync(synced, pack):
    from data2agent.connect import scheduler as sched
    from data2agent.connect.config import SourceConfig

    src, landing, _ = synced
    # Fresh landing so sync+apply run together
    landing2 = LandingStore(Path(landing.db_path).parent / "sched.sqlite")
    scfg = SourceConfig(adapter="sqlite_readonly", path=str(src),
                        tables={
                            "CUSTOMER": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                            "CURRENCY": {"mode": "full_refresh"},
                            "ITEM": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                            "QUOTATION": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                            "SALES_ORDER": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                            "SALES_ORDER_D": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                            "ITEM_WAREHOUSE": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                        })
    assert sched.run_sync_cycle(SOURCE, scfg, landing2.db_path) is True
    pub = landing2.get_published_dataset(SOURCE)
    assert pub is not None and pub.status == "published"
    assert _legacy_obj_tables(landing2) == []


# ---- Console apply ----


def test_console_apply_publish_true(synced, tmp_path):
    from data2agent.connect.config import load_config

    src, landing, _pack = synced
    cfg = load_config(_cfg_file(tmp_path, src, landing))
    client = TestClient(create_app(landing.db_path, ROOT / "templates", cfg))
    r = client.post("/api/actions/apply", json={"source": SOURCE, "publish": True})
    assert r.status_code == 200
    body = ApplyActionResult.model_validate(r.json())
    assert body.executed is True
    assert body.published is True
    assert body.dataset_version
    assert landing.get_published_dataset(SOURCE) is not None
    assert body.dataset_version == landing.get_published_dataset(SOURCE).dataset_version
    assert _legacy_obj_tables(landing) == []


def test_console_apply_publish_false_stage_only(synced, tmp_path):
    from data2agent.connect.config import load_config

    src, landing, _pack = synced
    cfg = load_config(_cfg_file(tmp_path, src, landing))
    client = TestClient(create_app(landing.db_path, ROOT / "templates", cfg))
    r = client.post("/api/actions/apply", json={"source": SOURCE, "publish": False})
    assert r.status_code == 200
    body = ApplyActionResult.model_validate(r.json())
    assert body.executed is True
    assert body.published is False
    assert body.dataset_version
    assert landing.get_published_dataset(SOURCE) is None
    ds = landing.get_dataset_version(body.dataset_version)
    assert ds is not None and ds.status == "building"
    assert _legacy_obj_tables(landing) == []


def test_console_apply_failure_keeps_published(synced, tmp_path, monkeypatch):
    from data2agent.connect.config import load_config
    from data2agent.connect.dataset_publish import build_dataset
    from data2agent.connect.mapping_apply import MappingCircuitBreaker

    src, landing, pack = synced
    first = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert first.published is True
    previous = first.dataset_version

    def _boom(*_a, **_k):
        raise MappingCircuitBreaker(
            "forced", total=10, mapped=0, quarantined=10, batch_id="x",
        )

    monkeypatch.setattr(
        "data2agent.connect.dataset_publish.apply_object", _boom,
    )
    cfg = load_config(_cfg_file(tmp_path, src, landing))
    client = TestClient(create_app(landing.db_path, ROOT / "templates", cfg))
    r = client.post("/api/actions/apply", json={"source": SOURCE, "publish": True})
    assert r.status_code == 200
    body = ApplyActionResult.model_validate(r.json())
    assert body.aborted  # 至少一个对象熔断
    assert body.published is False
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == previous


# ---- Console / CLI retry: full dataset rebuild ----


def test_console_retry_rebuilds_full_dataset_and_publishes(synced, tmp_path):
    from data2agent.connect.config import load_config
    from data2agent.connect.dataset_publish import build_dataset

    src, landing, pack = synced
    first = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert first.published is True
    focus = pack.objects[0].object
    enabled = [
        o.object for o in pack.objects
        if any(b.source == SOURCE and b.enabled and b.field_map for b in o.bindings)
    ]
    assert len(enabled) >= 2

    cfg = load_config(_cfg_file(tmp_path, src, landing))
    client = TestClient(create_app(landing.db_path, ROOT / "templates", cfg))
    r = client.post(
        "/api/actions/retry",
        json={"source": SOURCE, "object": focus},
    )
    assert r.status_code == 200, r.text
    body = RetryActionResult.model_validate(r.json())
    assert body.object == focus
    assert body.dataset_version
    assert body.dataset_version != first.dataset_version
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == body.dataset_version
    objs = {o.object for o in landing.list_object_versions(body.dataset_version)}
    assert objs == set(enabled)
    assert _legacy_obj_tables(landing) == []


def test_cli_quarantine_retry_rebuilds_full_dataset(synced, monkeypatch):
    from data2agent.connect import __main__ as cli
    from data2agent.connect.dataset_publish import build_dataset

    _src, landing, pack = synced
    first = build_dataset(landing, pack, SOURCE, auto_publish=True)
    focus = pack.objects[0].object
    monkeypatch.setattr(
        "sys.argv",
        [
            "connect", "quarantine", "retry",
            "--object", focus,
            "--source", SOURCE,
            "--landing", landing.db_path,
            "--templates", str(ROOT / "templates"),
        ],
    )
    assert cli.main() == 0
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version != first.dataset_version
    enabled = {
        o.object for o in pack.objects
        if any(b.source == SOURCE and b.enabled and b.field_map for b in o.bindings)
    }
    objs = {o.object for o in landing.list_object_versions(pub.dataset_version)}
    assert objs == enabled
    assert _legacy_obj_tables(landing) == []


def test_retry_failure_keeps_published_and_quarantine_retryable(synced, tmp_path, monkeypatch):
    """失败时旧 published 不变;隔离记录保持可重试(未被成功发布前取代)。"""
    from data2agent.connect.config import load_config
    from data2agent.connect.dataset_publish import build_dataset
    from data2agent.connect.mapping_apply import MappingCircuitBreaker

    src, landing, pack = synced
    # Seed a quarantine row on Quotation
    tpl = next(o for o in pack.objects if o.object == "Quotation")
    raw = raw_table_name(SOURCE, "QUOTATION")
    landing.con.execute(f'UPDATE "{raw}" SET DOC_NO = NULL WHERE Id = 5')
    landing.con.commit()
    first = build_dataset(landing, pack, SOURCE, auto_publish=True)
    # Quarantine may exist from first build (DOC_NO null); if breaker didn't trip,
    # force an unresolved row for the focus object.
    if landing.quarantine_count(SOURCE, "Quotation") == 0:
        landing.quarantine_add(
            SOURCE, "Quotation",
            [{"keys": {"id": 5}, "reason": "seed", "raw": {}}],
            "seed-batch",
        )
    before_q = landing.quarantine_count(SOURCE, "Quotation")
    assert before_q >= 1
    previous = first.dataset_version

    def _boom(*_a, **_k):
        raise MappingCircuitBreaker(
            "forced", total=10, mapped=0, quarantined=10, batch_id="fail",
        )

    monkeypatch.setattr(
        "data2agent.connect.dataset_publish.apply_object", _boom,
    )
    cfg = load_config(_cfg_file(tmp_path, src, landing))
    client = TestClient(create_app(landing.db_path, ROOT / "templates", cfg))
    r = client.post(
        "/api/actions/retry",
        json={"source": SOURCE, "object": "Quotation"},
    )
    assert r.status_code in (409, 500)
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None
    assert pub.dataset_version == previous
    assert landing.quarantine_count(SOURCE, "Quotation") >= before_q


def test_publish_supersedes_prior_quarantine_stage_only_does_not(synced):
    from data2agent.connect.dataset_publish import build_dataset

    _src, landing, pack = synced
    landing.quarantine_add(
        SOURCE, "Customer",
        [{"keys": {"id": 1}, "reason": "old", "raw": {}}],
        "old-batch",
    )
    staged = build_dataset(landing, pack, SOURCE, auto_publish=False)
    assert staged.outcome == "ok" and staged.published is False
    assert landing.quarantine_count(SOURCE, "Customer") >= 1

    published = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert published.published is True
    # 旧 batch 应被取代;若本轮仍有隔离则只剩本轮 batch
    unresolved = landing.con.execute(
        "SELECT batch_id FROM d2a_quarantine "
        "WHERE source = ? AND object = ? AND resolved_at IS NULL",
        (SOURCE, "Customer"),
    ).fetchall()
    assert all(r["batch_id"] != "old-batch" for r in unresolved)


def test_retry_forwards_build_conflict_reason_code(synced, tmp_path, monkeypatch):
    from data2agent.connect.config import load_config
    from data2agent.connect.dataset_publish import BuildDatasetResult
    from data2agent.console.contracts import RetryActionError

    _src, landing, pack = synced
    monkeypatch.setattr(
        "data2agent.console.app.build_dataset",
        lambda *_a, **_k: BuildDatasetResult(
            source=SOURCE,
            dataset_version=None,
            previous_dataset_version=None,
            status=None,
            ready=False,
            published=False,
            outcome="conflict",
            reason_code="active_build",
            error="已有运行中的数据集构建",
        ),
    )
    cfg = load_config(_cfg_file(tmp_path, _src, landing))
    client = TestClient(create_app(landing.db_path, ROOT / "templates", cfg))
    r = client.post(
        "/api/actions/retry",
        json={"source": SOURCE, "object": "Customer"},
    )
    assert r.status_code == 409
    error = RetryActionError.model_validate(r.json())
    assert error.reason_code == "active_build"
    assert error.executed is False
