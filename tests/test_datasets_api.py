"""v0.3 M1-T04: datasets 只读 API 与 publish/rollback fail-closed。"""

from pathlib import Path

from fastapi.testclient import TestClient

from data2agent.shared.store.landing import LandingStore
from data2agent.platform.console.app import create_app
from data2agent.platform.console.contracts import DatasetDetail, DatasetSummary

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


def _client(landing: LandingStore) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates"))


def test_datasets_list_empty_does_not_fabricate_versions(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    client = _client(store)
    r = client.get("/api/datasets")
    assert r.status_code == 200
    assert r.json() == []
    assert r.headers["X-Total-Count"] == "0"


def test_datasets_list_and_detail_with_objects(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    con = store.con
    con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at, published_at, "
        "object_manifest) "
        "VALUES ('ds-1', ?, '0.1.0', 'published', '2026-07-21T10:00:00', "
        "'2026-07-21T10:05:00', ?)",
        (SOURCE, '["Customer"]'),
    )
    con.execute(
        "INSERT INTO d2a_object_version "
        "(dataset_version, object, object_version, binding_hash, row_count, "
        "status, built_at, published_at) "
        "VALUES ('ds-1', 'Customer', 'obj-1', ?, 12, 'published', "
        "'2026-07-21T10:00:00', '2026-07-21T10:05:00')",
        ("sha256:" + "c" * 64,),
    )
    con.commit()

    client = _client(store)
    listed = client.get("/api/datasets")
    assert listed.status_code == 200
    assert listed.headers["X-Total-Count"] == "1"
    body = [DatasetSummary.model_validate(x) for x in listed.json()]
    assert body[0].dataset_version == "ds-1"
    assert body[0].status == "published"
    assert body[0].built_at.tzinfo is not None
    assert body[0].object_manifest == ["Customer"]

    detail = DatasetDetail.model_validate(
        client.get("/api/datasets/ds-1").json())
    assert detail.dataset_version == "ds-1"
    assert len(detail.objects) == 1
    assert detail.objects[0].object == "Customer"
    assert detail.objects[0].binding_hash.startswith("sha256:")

    missing = client.get("/api/datasets/missing")
    assert missing.status_code == 404


def test_datasets_publish_and_rollback_map_missing_to_404(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    client = _client(store)
    for path in (
        "/api/datasets/ds-missing/publish",
        "/api/datasets/ds-missing/rollback",
    ):
        r = client.post(path)
        assert r.status_code == 404
        assert "不存在" in r.json()["detail"] or "not_found" in r.json()["detail"]


def test_datasets_publish_conflict_when_not_ready(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    con = store.con
    con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at) "
        "VALUES ('ds-build', ?, '0.1.0', 'building', '2026-07-21T11:00:00')",
        (SOURCE,),
    )
    con.commit()
    client = _client(store)
    r = client.post("/api/datasets/ds-build/publish")
    assert r.status_code == 409
    after = con.execute("SELECT status FROM d2a_dataset_version").fetchone()[0]
    assert after == "building"


def test_datasets_error_is_sanitized_and_has_error_id(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    leak = (
        "Traceback (most recent call last):\n"
        "  File \"/Users/ops/app.py\", line 1\n"
        "pyodbc.Error: DSN=Server=db;UID=sa;PWD=s3cret!;Token=Bearer abc.def\n"
        "path=/var/data/factory.sqlite"
    )
    store.con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at, error) "
        "VALUES ('ds-fail', ?, '0.1.0', 'failed', '2026-07-21T11:00:00', ?)",
        (SOURCE, leak),
    )
    store.con.commit()

    client = _client(store)
    detail = DatasetDetail.model_validate(
        client.get("/api/datasets/ds-fail").json())
    assert detail.error_id and len(detail.error_id) == 12
    assert detail.error == f"数据集构建失败(error_id={detail.error_id})"
    body = client.get("/api/datasets/ds-fail").text
    for needle in (
        "s3cret", "PWD=", "Bearer abc", "/Users/ops", "factory.sqlite", "Traceback",
    ):
        assert needle not in body


def test_datasets_error_never_returns_raw_unknown_secret_formats(tmp_path):
    """未命中旧黑名单的秘密格式也不得出网;始终固定摘要。"""
    store = LandingStore(tmp_path / "landing.sqlite")
    leak = "api_key=sk-live-123 customer=alice@example.com build step failed"
    store.con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at, error) "
        "VALUES ('ds-secret', ?, '0.1.0', 'failed', '2026-07-21T11:00:00', ?)",
        (SOURCE, leak),
    )
    store.con.commit()
    r = _client(store).get("/api/datasets/ds-secret")
    assert r.status_code == 200
    body = DatasetDetail.model_validate(r.json())
    assert body.error == f"数据集构建失败(error_id={body.error_id})"
    assert "sk-live" not in r.text
    assert "alice@example.com" not in r.text
    assert "api_key" not in r.text


def test_datasets_published_requires_published_at(tmp_path):
    """API 对 published/retired 强制可解析 published_at(绕过 DDL 的映射层回归)。"""
    import pytest
    from fastapi import HTTPException

    from data2agent.platform.console.app import _map_dataset_summary, _map_object_version
    from data2agent.shared.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord

    with pytest.raises(HTTPException) as ds_exc:
        _map_dataset_summary(DatasetVersionRecord(
            dataset_version="ds-1",
            source=SOURCE,
            template_version="0.1.0",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at=None,
        ))
    assert ds_exc.value.status_code == 500
    assert "published_at" in ds_exc.value.detail

    with pytest.raises(HTTPException) as obj_exc:
        _map_object_version(ObjectVersionRecord(
            dataset_version="ds-1",
            object="Customer",
            object_version="obj-1",
            binding_hash="sha256:" + "a" * 64,
            row_count=1,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at=None,
        ))
    assert obj_exc.value.status_code == 500
    assert "published_at" in obj_exc.value.detail


def test_datasets_corrupt_times_fail_closed(tmp_path):
    store = LandingStore(tmp_path / "landing.sqlite")
    store.con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at, published_at) "
        "VALUES ('ds-bad-pub', ?, '0.1.0', 'published', '2026-07-21T10:00:00', "
        "'not-a-timestamp')",
        (SOURCE,),
    )
    store.con.execute(
        "INSERT INTO d2a_dataset_version "
        "(dataset_version, source, template_version, status, built_at) "
        "VALUES ('ds-bad-built', ?, '0.1.0', 'building', '@@@')",
        (SOURCE,),
    )
    store.con.commit()
    client = _client(store)

    bad_pub = client.get("/api/datasets/ds-bad-pub")
    assert bad_pub.status_code == 500
    assert "published_at" in bad_pub.json()["detail"]
    assert "error_id=" in bad_pub.json()["detail"]

    bad_built = client.get("/api/datasets/ds-bad-built")
    assert bad_built.status_code == 500
    assert "built_at" in bad_built.json()["detail"]
    assert "error_id=" in bad_built.json()["detail"]
