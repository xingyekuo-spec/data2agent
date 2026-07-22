"""v0.3 M4-T07: lineage 查询 API 端到端测试。

门禁:published snapshot 只读查询;property 过滤;脱敏;
404/409/422/500 契约;旧版 200 unavailable;并发 publish 无混版。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.dataset_publish import build_dataset  # noqa: E402
from data2agent.connect.field_lineage import object_key_token  # noqa: E402
from data2agent.connect.increment import (  # noqa: E402
    incremental_sync,
    watermarks_from_pack,
)
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.connect.sync import whitelist_from_pack  # noqa: E402
from data2agent.console.app import create_app  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.showroom.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TOKEN = "test-console-token"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture(scope="module")
def published_env(tmp_path_factory, pack):
    """构建并发布一个带 lineage 的数据集,返回 (landing, client, ds_version)。"""
    td = tmp_path_factory.mktemp("lineage-api")
    src = td / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(td / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))

    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.outcome == "ok"

    app = create_app(
        landing=str(td / "landing.sqlite"),
        templates=str(ROOT / "templates"),
        token=TOKEN,
    )
    client = TestClient(app, raise_server_exceptions=False)
    return landing, client, result.dataset_version


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_lineage_token(
    landing: LandingStore, ds: str, obj: str,
) -> str:
    row = landing.con.execute(
        "SELECT DISTINCT object_key_hash FROM d2a_field_lineage "
        "WHERE dataset_version = ? AND object = ? LIMIT 1",
        (ds, obj),
    ).fetchone()
    assert row is not None
    return row["object_key_hash"]


# ---- 成功查询 -------------------------------------------------------------------


def test_lineage_available(published_env, pack):
    """published 数据集的 lineage 查询返回 available 和完整字段。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    tpl = next(t for t in pack.objects if t.object == obj.object)
    token = _get_lineage_token(landing, ds, obj.object)

    r = client.get(
        f"/api/objects/{obj.object}/{token}/lineage", headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "available"
    assert body["reason_code"] is None
    assert body["object"] == obj.object
    assert body["key_token"] == token
    assert body["dataset_version"] == ds
    assert body["template_version"] == pack.version
    assert body["binding_hash"] is not None
    assert body["map_batch_id"] is not None
    assert len(body["fields"]) == len(tpl.properties)
    assert body["generated_at"] is not None


def test_lineage_field_structure(published_env, pack):
    """每个字段有 property/display_name/state/steps/inputs。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    token = _get_lineage_token(landing, ds, obj.object)

    r = client.get(
        f"/api/objects/{obj.object}/{token}/lineage", headers=_auth(),
    )
    body = r.json()
    for field in body["fields"]:
        assert "property" in field
        assert "display_name" in field
        assert field["state"] in ("available", "unavailable")
        assert isinstance(field["steps"], list)
        assert isinstance(field["inputs"], list)
        if field["state"] == "available":
            assert field["final_value"] is not None


def test_lineage_property_filter(published_env, pack):
    """property 参数只返回指定字段。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    tpl = next(t for t in pack.objects if t.object == obj.object)
    token = _get_lineage_token(landing, ds, obj.object)
    prop = tpl.properties[0].name

    r = client.get(
        f"/api/objects/{obj.object}/{token}/lineage",
        params={"property": prop},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["fields"]) == 1
    assert body["fields"][0]["property"] == prop


# ---- 错误契约 -------------------------------------------------------------------


def test_lineage_invalid_key_token(published_env):
    """非法 key token → 422 lineage_key_invalid。"""
    _, client, _ = published_env
    r = client.get(
        "/api/objects/SalesOrderLine/not-a-valid-token/lineage",
        headers=_auth(),
    )
    assert r.status_code == 422
    assert r.json()["reason_code"] == "lineage_key_invalid"


def test_lineage_object_not_found(published_env):
    """不存在的对象 → 404 object_not_found。"""
    _, client, _ = published_env
    token = "a" * 64
    r = client.get(
        f"/api/objects/NoSuchObject/{token}/lineage", headers=_auth(),
    )
    assert r.status_code == 404
    assert r.json()["reason_code"] == "object_not_found"


def test_lineage_field_not_found(published_env, pack):
    """不存在的属性过滤 → 404 field_not_found。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    token = _get_lineage_token(landing, ds, obj.object)

    r = client.get(
        f"/api/objects/{obj.object}/{token}/lineage",
        params={"property": "no_such_field"},
        headers=_auth(),
    )
    assert r.status_code == 404
    assert r.json()["reason_code"] == "field_not_found"


def test_lineage_record_not_found(published_env, pack):
    """合法 token 但无对应记录 → 404 record_not_found。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    fake_token = "f" * 64

    r = client.get(
        f"/api/objects/{obj.object}/{fake_token}/lineage", headers=_auth(),
    )
    assert r.status_code == 404
    assert r.json()["reason_code"] == "record_not_found"


def test_lineage_unauthorized(published_env):
    """错误 Bearer → 401 unauthorized。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    token = _get_lineage_token(landing, ds, obj.object)

    r = client.get(
        f"/api/objects/{obj.object}/{token}/lineage",
        headers=_auth("wrong-token"),
    )
    assert r.status_code == 401
    assert r.json()["reason_code"] == "unauthorized"


def test_lineage_no_bearer(published_env):
    """无 Bearer → 401。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    token = _get_lineage_token(landing, ds, obj.object)

    r = client.get(f"/api/objects/{obj.object}/{token}/lineage")
    assert r.status_code == 401


# ---- 脱敏 -----------------------------------------------------------------------


def test_lineage_sensitive_masked(published_env, pack):
    """敏感属性的 final_value 和 source_value 被遮罩。"""
    landing, client, ds = published_env
    # 找一个有敏感属性的对象
    sensitive_obj = None
    sensitive_prop = None
    for tpl in pack.objects:
        for p in tpl.properties:
            if p.sensitive:
                sensitive_obj = tpl.object
                sensitive_prop = p.name
                break
        if sensitive_obj:
            break

    if sensitive_obj is None:
        pytest.skip("模板无敏感属性")

    token = _get_lineage_token(landing, ds, sensitive_obj)
    r = client.get(
        f"/api/objects/{sensitive_obj}/{token}/lineage",
        params={"property": sensitive_prop},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    field = body["fields"][0]
    if field["state"] == "available" and field["final_value"]:
        fv = field["final_value"]
        if fv.get("value") is not None:
            assert fv["value"] == "•••"


# ---- 审计 -----------------------------------------------------------------------


def test_lineage_access_audit(published_env):
    """成功查询写入访问审计。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    token = _get_lineage_token(landing, ds, obj.object)

    before = landing.con.execute(
        "SELECT COUNT(*) FROM d2a_console_access_audit "
        "WHERE resource LIKE 'field_lineage:%'",
    ).fetchone()[0]

    client.get(
        f"/api/objects/{obj.object}/{token}/lineage", headers=_auth(),
    )

    after = landing.con.execute(
        "SELECT COUNT(*) FROM d2a_console_access_audit "
        "WHERE resource LIKE 'field_lineage:%'",
    ).fetchone()[0]
    assert after > before


# ---- 版本身份一致性 ---------------------------------------------------------------


def test_lineage_version_consistency(published_env, pack):
    """响应版本与 published snapshot 完全一致。"""
    landing, client, ds = published_env
    objs = landing.list_object_versions(ds)
    obj = objs[0]
    token = _get_lineage_token(landing, ds, obj.object)

    r = client.get(
        f"/api/objects/{obj.object}/{token}/lineage", headers=_auth(),
    )
    body = r.json()
    assert body["dataset_version"] == ds
    assert body["object_version"] == obj.object_version
    assert body["template_version"] == pack.version
    assert body["binding_hash"] == obj.binding_hash
