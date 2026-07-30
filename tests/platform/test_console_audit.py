"""M4-T05 审计 API 测试:SQL 审计筛选/总数/时间区间、访问审计查询与安全字段。"""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore
from tests.helpers import whitelist_from_pack
from data2agent.platform.console.app import create_app
from data2agent.platform.console.contracts import AccessAuditPage
from data2agent.shared.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"


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
    # 访问审计样本:允许 + 拒绝
    landing.log_access(subject="console-admin", resource_type="raw",
                       source=SOURCE, resource="CUSTOMER", allowed=True,
                       reason_code="ok", page_offset=0, page_limit=50, returned_rows=36)
    landing.log_access(subject="console-admin", resource_type="raw",
                       source=None, resource="sqlite_master", allowed=False,
                       reason_code="not_in_catalog")
    landing.log_access(subject="console-admin", resource_type="object",
                       source=None, resource="Customer", allowed=True,
                       reason_code="ok", page_offset=0, page_limit=50, returned_rows=24)
    return landing


def _client(landing: LandingStore) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates"))


# ---- SQL 操作审计 ----


def test_audit_array_shape_total_header_and_id(env):
    client = _client(env)
    r = client.get("/api/audit")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert body and "id" in body[0] and "ts" in body[0]
    total = int(r.headers["X-Total-Count"])
    assert total >= len(body)
    assert len(body) == min(total, 50)  # 默认审计页大小


def test_audit_filters(env):
    client = _client(env)
    r = client.get("/api/audit", params={"source": SOURCE})
    assert r.status_code == 200
    assert all(x["source"] == SOURCE for x in r.json())
    r = client.get("/api/audit", params={"action": "select"})
    assert all(x["action"] == "select" for x in r.json())
    r = client.get("/api/audit", params={"action": "ingest"})
    assert r.json() == [] and r.headers["X-Total-Count"] == "0"


def test_audit_time_range(env):
    client = _client(env)
    r = client.get("/api/audit", params={
        "from": "2020-01-01T00:00:00+08:00", "to": "2030-01-01T00:00:00+08:00"})
    assert r.status_code == 200 and r.json()
    r = client.get("/api/audit", params={
        "from": "2030-01-01T00:00:00+08:00", "to": "2031-01-01T00:00:00+08:00"})
    assert r.json() == []
    # from >= to → 422
    r = client.get("/api/audit", params={
        "from": "2030-01-01T00:00:00+08:00", "to": "2020-01-01T00:00:00+08:00"})
    assert r.status_code == 422
    r = client.get("/api/audit", params={
        "from": "2026-07-18T00:00:00+08:00", "to": "2026-07-18T00:00:00+08:00"})
    assert r.status_code == 422
    r = client.get("/api/audit", params={
        "from": "2026-07-18T00:00:00", "to": "2026-07-19T00:00:00+08:00"})
    assert r.status_code == 422
    r = client.get("/api/audit/access", params={
        "from": "2026-07-18T00:00:00", "to": "2026-07-19T00:00:00+08:00"})
    assert r.status_code == 422


def test_audit_rejects_bad_limit(env):
    client = _client(env)
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get("/api/audit", params=params).status_code == 422


# ---- 访问审计 ----


def test_access_audit_page_and_filters(env):
    client = _client(env)
    r = client.get("/api/audit/access")
    assert r.status_code == 200
    body = AccessAuditPage.model_validate(r.json())
    assert body.total == 3
    assert body.generated_at.tzinfo is not None
    # 筛选:允许/拒绝
    r = client.get("/api/audit/access", params={"allowed": False})
    denied = AccessAuditPage.model_validate(r.json())
    assert denied.total == 1
    assert denied.items[0].allowed is False
    assert denied.items[0].reason_code == "not_in_catalog"
    # 资源类型
    r = client.get("/api/audit/access", params={"resource_type": "object"})
    objs = AccessAuditPage.model_validate(r.json())
    assert objs.total == 1
    assert objs.items[0].resource_type == "object"
    # 主体
    r = client.get("/api/audit/access", params={"subject": "nobody"})
    assert AccessAuditPage.model_validate(r.json()).total == 0


def test_access_audit_has_no_sensitive_fields(env):
    client = _client(env)
    body = client.get("/api/audit/access").json()
    allowed_keys = {"id", "ts", "subject", "resource_type", "source", "resource",
                    "allowed", "reason_code", "offset", "limit", "returned_rows",
                    "request_id"}
    for item in body["items"]:
        assert set(item.keys()) == allowed_keys
        for value in item.values():
            text = str(value).lower()
            assert "token" not in text or value == item.get("reason_code") or value is True
