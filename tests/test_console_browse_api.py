"""M4-T07 raw/object API 测试:目录、分页浏览、强鉴权、访问审计、脱敏、错误语义。"""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.dataset_publish import build_dataset
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore
from tests.helpers import whitelist_from_pack
from data2agent.console import data_browser as br
from data2agent.console.app import create_app
from data2agent.console.contracts import (
    HttpError,
    ObjectRowsPageResponse,
    RawDataPageResponse,
    RawTableCatalogResponse,
)
from data2agent.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TOKEN = "raw-secret"


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    return landing


def _client(landing: LandingStore, token: str | None = TOKEN) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates", token=token))


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


# ---- raw 目录 ----


def test_raw_catalog(env):
    body = RawTableCatalogResponse.model_validate(
        _client(env).get("/api/data/raw", headers=_auth()).json())
    assert body.items, "同步后应有目录"
    customer = next(i for i in body.items if i.table == "CUSTOMER")
    assert customer.rows == 24
    assert customer.searchable is True
    assert body.generated_at.tzinfo is not None
    # 不得暴露 SQLite 内部表
    assert all(not i.table.startswith(("sqlite_", "d2a_")) for i in body.items)


def test_raw_catalog_requires_token_and_does_not_expose_orphans(env):
    LandingStore(env.db_path).con.execute(
        'CREATE TABLE "raw_orphan__SECRET" ("Id" INTEGER PRIMARY KEY)')
    LandingStore(env.db_path).con.execute(
        f'CREATE TABLE "raw_{SOURCE}__SECRET" ("Id" INTEGER PRIMARY KEY)')
    client = _client(env)
    wrong = client.get("/api/data/raw", headers={"Authorization": "Bearer wrong"})
    assert wrong.status_code == 401
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "unauthorized"
    ok = client.get("/api/data/raw", headers=_auth())
    assert ok.status_code == 200
    items = {(i["source"], i["table"]) for i in ok.json()["items"]}
    assert ("orphan", "SECRET") not in items
    assert (SOURCE, "SECRET") not in items
    assert client.get(f"/api/data/raw/{SOURCE}/SECRET", headers=_auth()).status_code == 404


def test_raw_catalog_unexpected_failure_is_audited_without_leak(env, monkeypatch):
    client = _client(env)

    def boom(*args, **kwargs):
        raise RuntimeError("catalog boom sensitive sql")

    monkeypatch.setattr(br, "raw_catalog", boom)
    r = client.get("/api/data/raw", headers=_auth())
    assert r.status_code == 500
    assert r.json()["detail"] == "raw 目录失败"
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0
    assert row["reason_code"] == "catalog_failed"
    assert "catalog boom" not in str(dict(row))


# ---- raw 强鉴权 ----


def test_raw_requires_configured_token(env):
    client = _client(env, token=None)  # 未配置 Token
    r = client.get(f"/api/data/raw/{SOURCE}/CUSTOMER")
    assert r.status_code == 403
    HttpError.model_validate(r.json())
    # 拒绝也写审计(不泄密)
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "token_not_configured"
    assert row["subject"] == "anonymous"


def test_raw_rejects_wrong_token_and_allows_valid(env):
    client = _client(env)
    assert client.get(f"/api/data/raw/{SOURCE}/CUSTOMER",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "unauthorized"
    r = client.get(f"/api/data/raw/{SOURCE}/CUSTOMER", headers=_auth())
    assert r.status_code == 200
    body = RawDataPageResponse.model_validate(r.json())
    assert body.total == 24
    assert body.searchable is True
    assert body.sort.startswith("pk:")
    # 敏感列脱敏、未知列警告
    cols = {c.name: c for c in body.columns}
    assert cols["CONTACT_EMAIL"].masked is True
    assert all(row["CONTACT_EMAIL"].root == "***" for row in body.rows)
    assert any("LAST_MODIFIED_DATE" in w for w in body.warnings)
    # 允许审计:主体 console-admin,不记 Token
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit WHERE allowed = 1 "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["subject"] == "console-admin"
    assert row["returned_rows"] == len(body.rows)
    assert TOKEN not in str(dict(row))


def test_raw_404_and_audit_no_leak(env):
    client = _client(env)
    assert client.get("/api/data/raw/bogus/CUSTOMER", headers=_auth()).status_code == 404
    assert client.get(f"/api/data/raw/{SOURCE}/sqlite_master",
                      headers=_auth()).status_code == 404
    (denied,) = LandingStore(env.db_path).con.execute(
        "SELECT COUNT(*) FROM d2a_console_access_audit "
        "WHERE allowed = 0 AND reason_code = 'not_in_catalog'").fetchone()
    assert denied >= 2


def test_raw_search_and_pagination(env):
    client = _client(env)
    hit = client.get(f"/api/data/raw/{SOURCE}/CUSTOMER",
                     params={"q": "C001"}, headers=_auth()).json()
    assert hit["total"] >= 1 and hit["query"] == "C001"
    page = client.get(f"/api/data/raw/{SOURCE}/CUSTOMER",
                      params={"limit": 10, "offset": 10}, headers=_auth()).json()
    assert len(page["rows"]) == 10 and page["offset"] == 10
    assert client.get(f"/api/data/raw/{SOURCE}/CUSTOMER",
                      params={"limit": 0}, headers=_auth()).status_code == 422
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "invalid_query"
    assert client.get(f"/api/data/raw/{SOURCE}/CUSTOMER",
                      params={"limit": "not-an-int"}, headers=_auth()).status_code == 422
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0 and row["reason_code"] == "invalid_query"


def test_raw_validation_audit_failure_is_closed_without_leak(env, monkeypatch):
    client = _client(env)

    def boom(*args, **kwargs):
        raise RuntimeError("secret-storage-detail")

    monkeypatch.setattr(LandingStore, "log_access", boom)
    r = client.get(
        f"/api/data/raw/{SOURCE}/CUSTOMER",
        params={"limit": "not-an-int"},
        headers=_auth())
    assert r.status_code == 500
    assert r.json()["detail"] == "访问审计写入失败,raw 浏览已关闭"
    assert "secret-storage-detail" not in r.text


def test_raw_unexpected_failure_is_audited_without_leak(env, monkeypatch):
    client = _client(env)

    def boom(*args, **kwargs):
        raise RuntimeError("meta boom sensitive sql")

    monkeypatch.setattr(br, "raw_column_meta", boom)
    r = client.get(f"/api/data/raw/{SOURCE}/CUSTOMER", headers=_auth())
    assert r.status_code == 500
    assert r.json()["detail"] == "raw 浏览失败"
    row = LandingStore(env.db_path).con.execute(
        "SELECT * FROM d2a_console_access_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row["allowed"] == 0
    assert row["reason_code"] == "browse_failed"
    assert "meta boom" not in str(dict(row))


# ---- objects ----


def test_objects_catalog_and_rows(env):
    client = _client(env)
    items = client.get("/api/objects", headers=_auth()).json()
    assert len(items) == 15
    customer = next(i for i in items if i["object"] == "Customer")
    assert customer["rows"] == 24 and customer["searchable"] is True
    r = client.get("/api/objects/Customer", headers=_auth())
    assert r.status_code == 200
    body = ObjectRowsPageResponse.model_validate(r.json())
    assert body.total == 24
    cols = {c.name: c for c in body.columns}
    assert cols["customer_code"].role == "business_key"
    assert cols["contact"].masked is True
    assert all(row["contact"].root == "***" for row in body.rows)


def test_object_404_and_not_materialized(env):
    client = _client(env)
    assert client.get("/api/objects/Bogus", headers=_auth()).status_code == 404
    # 退役 published 快照 → 具名 409(不回退遗留表)
    pub = LandingStore(env.db_path).get_published_dataset(SOURCE)
    assert pub is not None
    store = LandingStore(env.db_path)
    store.con.execute(
        "UPDATE d2a_dataset_version SET status = 'retired' WHERE dataset_version = ?",
        (pub.dataset_version,),
    )
    store.con.commit()
    r = client.get("/api/objects/Material", headers=_auth())
    assert r.status_code == 409
    HttpError.model_validate(r.json())
    assert "obj_" not in r.json().get("detail", "")


def test_browse_has_no_business_side_effects(env):
    before = {t: env.con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
              for t in ("d2a_sync_state", "d2a_quarantine", "d2a_sync_run")}
    client = _client(env)
    client.get("/api/data/raw").json()
    client.get(f"/api/data/raw/{SOURCE}/CUSTOMER", headers=_auth())
    client.get("/api/objects/Customer", headers=_auth())
    after = {t: env.con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
             for t in ("d2a_sync_state", "d2a_quarantine", "d2a_sync_run")}
    assert before == after, "浏览不得改变业务表/水位/隔离"
    # 唯一的写入是访问审计
    (n,) = LandingStore(env.db_path).con.execute(
        "SELECT COUNT(*) FROM d2a_console_access_audit").fetchone()
    assert n >= 1
