"""中间机元数据扫描 / 详情 / 键与水位校验 API。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.middle.admin.app import create_app  # noqa: E402


def _sample_db(path: Path) -> None:
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE CUSTOMER (
            Id INTEGER PRIMARY KEY,
            CODE TEXT NOT NULL UNIQUE,
            LAST_MODIFIED_DATE TEXT
        );
        INSERT INTO CUSTOMER VALUES (1, 'C1', '2026-07-01');
        """
    )
    con.close()


@pytest.fixture()
def meta_env(tmp_path: Path):
    from data2agent.middle.admin import app as middle_app
    middle_app._SCAN_STORE.clear()
    src = tmp_path / "source.sqlite"
    _sample_db(src)
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "templates": str(Path(__file__).resolve().parents[1] / "templates"),
            "landing": str(tmp_path / "landing.sqlite"),
            "sources": {
                "digiwin_e10": {
                    "adapter": "sqlite_readonly",
                    "path": str(src),
                    "tables": {},
                }
            },
        }),
        encoding="utf-8",
    )
    app = create_app(config_path=cfg, token="secret", log_path=tmp_path / "c.log")
    return TestClient(app), cfg


def _wait_scan(client: TestClient, scan_id: str, headers: dict, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/metadata/scans/{scan_id}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        if body["status"] in ("completed", "partial", "failed", "timeout"):
            return body
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def test_metadata_scan_without_tables(meta_env):
    client, _ = meta_env
    h = {"Authorization": "Bearer secret"}
    started = client.post("/api/metadata/scans", headers=h, json={})
    assert started.status_code == 200
    scan_id = started.json()["scan_id"]
    body = _wait_scan(client, scan_id, h)
    assert body["status"] == "completed"
    assert body["table_count"] >= 1
    created = datetime.strptime(body["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    assert created.year >= 2020

    listed = client.get("/api/metadata/tables", headers=h)
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] >= 1
    assert any(t["name"] == "CUSTOMER" for t in payload["tables"])
    assert all(t["in_extraction_plan"] is False for t in payload["tables"])
    customer = next(t for t in payload["tables"] if t["name"] == "CUSTOMER")
    assert customer.get("schema_fingerprint", "").startswith("sha256:")

    detail = client.get("/api/metadata/tables/main/CUSTOMER", headers=h)
    assert detail.status_code == 200
    d = detail.json()
    assert d["primary_key"] == ["Id"]
    assert "LAST_MODIFIED_DATE" in d["watermark_candidates"]
    assert d["schema_fingerprint"].startswith("sha256:")


def test_metadata_key_and_watermark_check(meta_env):
    client, _ = meta_env
    h = {"Authorization": "Bearer secret"}
    key = client.post("/api/metadata/key-check", headers=h, json={
        "schema": "main", "table": "CUSTOMER", "columns": ["Id"],
    })
    assert key.status_code == 200 and key.json()["ok"] is True

    wm = client.post("/api/metadata/watermark-check", headers=h, json={
        "schema": "main", "table": "CUSTOMER", "column": "LAST_MODIFIED_DATE",
    })
    assert wm.status_code == 200
    assert wm.json()["ok"] is True
    assert wm.json()["candidate"] is True


def test_metadata_tables_requires_scan(meta_env):
    client, _ = meta_env
    h = {"Authorization": "Bearer secret"}
    r = client.get("/api/metadata/tables", headers=h)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "metadata_stale"
    assert r.json()["detail"].get("suggestion")


def test_running_scan_does_not_hide_completed_cache(meta_env):
    client, cfg = meta_env
    h = {"Authorization": "Bearer secret"}
    first = client.post("/api/metadata/scans", headers=h, json={}).json()["scan_id"]
    _wait_scan(client, first, h)

    # 人为占用活动槽:直接 begin 一个 running 记录
    from data2agent.middle.admin import app as middle_app
    blocking = middle_app._SCAN_STORE.try_begin("digiwin_e10")
    listed = client.get("/api/metadata/tables", headers=h)
    assert listed.status_code == 200
    assert listed.json()["scan_id"] == first

    busy = client.post("/api/metadata/scans", headers=h, json={})
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "scan_busy"
    assert busy.json()["detail"].get("suggestion")
    middle_app._SCAN_STORE.fail(blocking.scan_id, "cancelled", "test")


def test_in_extraction_plan_uses_schema(meta_env):
    client, cfg = meta_env
    h = {"Authorization": "Bearer secret"}
    scan_id = client.post("/api/metadata/scans", headers=h, json={}).json()["scan_id"]
    _wait_scan(client, scan_id, h)

    # 写入仅 main.CUSTOMER 的计划(sqlite schema=main)
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["sources"]["digiwin_e10"]["tables"] = {
        "CUSTOMER": {"mode": "full_refresh", "schema": "main"},
    }
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")

    listed = client.get("/api/metadata/tables", headers=h).json()["tables"]
    customer = next(t for t in listed if t["name"] == "CUSTOMER")
    assert customer["in_extraction_plan"] is True

    detail = client.get("/api/metadata/tables/main/CUSTOMER", headers=h).json()
    assert detail["in_extraction_plan"] is True

    # 同名不同 schema 不应命中
    from data2agent.middle.extract.metadata import extraction_plan_keys, in_extraction_plan
    from data2agent.shared.config import load_config
    planned = extraction_plan_keys(
        load_config(cfg).sources["digiwin_e10"].tables, default_schema="main")
    assert not in_extraction_plan("audit", "CUSTOMER", planned, default_schema="main")


def test_scan_marks_partial_when_get_table_fails(meta_env, monkeypatch):
    """详情失败必须进入 table_errors / partial,不得伪装 completed 空列表。"""
    from data2agent.middle.extract.discoverers.sqlite import SqliteMetadataDiscoverer
    from data2agent.middle.extract.metadata import MetadataError, TableSummary

    client, _ = meta_env
    h = {"Authorization": "Bearer secret"}

    real_list = SqliteMetadataDiscoverer.list_tables
    real_get = SqliteMetadataDiscoverer.get_table

    def list_with_catalog(self, **kwargs):
        tables, total = real_list(self, **kwargs)
        # 确保目录至少有一张表
        assert total >= 1
        return tables, total

    def get_fail_once(self, schema, table):
        raise MetadataError("table_missing", f"模拟失败:{schema}.{table}")

    monkeypatch.setattr(SqliteMetadataDiscoverer, "list_tables", list_with_catalog)
    monkeypatch.setattr(SqliteMetadataDiscoverer, "get_table", get_fail_once)

    scan_id = client.post("/api/metadata/scans", headers=h, json={}).json()["scan_id"]
    body = _wait_scan(client, scan_id, h)
    assert body["status"] == "partial"
    assert body["table_errors"] >= 1
    assert body["table_count"] >= 1
    assert any(t.get("error_code") for t in body["tables"])
