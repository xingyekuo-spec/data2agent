"""v0.3 M4-T01: field lineage API 契约冻结与失败测试。

门禁:OpenAPI 命名 schema、复合键 key token、非法 token 422、Bearer-only、
reason_code 字面量可见、未实现查询 fail-closed(501)。完整 published 查询见 T07。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.shared.store.field_lineage import (  # noqa: E402
    canonical_object_key_json,
    is_valid_lineage_key_token,
    object_key_token,
    object_key_token_from_pairs,
    require_lineage_key_token,
)
from data2agent.platform.console.app import create_app  # noqa: E402
from data2agent.platform.console.contracts import (  # noqa: E402
    ObjectLineageError,
    ObjectLineageResponse,
)

ROOT = Path(__file__).resolve().parents[1]
LINEAGE_PATH = "/api/objects/{object}/{key}/lineage"
TOKEN = "test-console-token"

ERROR_REASON_CODES = {
    "unauthorized",
    "token_not_configured",
    "object_not_found",
    "field_not_found",
    "record_not_found",
    "dataset_not_published",
    "snapshot_corrupt",
    "lineage_incomplete",
    "lineage_key_invalid",
    "lineage_query_failed",
}

FIELD_UNAVAILABLE_REASONS = {
    "property_unmapped",
    "join_target_missing",
    "source_evidence_unavailable",
}

RESPONSE_SCHEMA_NAMES = {
    "ObjectLineageResponse",
    "ObjectLineageError",
    "ObjectLineageField",
    "ObjectLineageStep",
    "ObjectLineageInput",
    "ObjectLineageRef",
    "ValueEvidence",
}


def _openapi_app(tmp_path, *, token: str | None = None):
    landing = tmp_path / "empty-landing.sqlite"
    return create_app(
        landing=str(landing),
        templates=str(ROOT / "templates"),
        token=token,
    )


def _client(tmp_path, *, token: str | None = None) -> TestClient:
    return TestClient(_openapi_app(tmp_path, token=token))


def _schema_ref(node: dict) -> str | None:
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    return None


def _assert_schema_not_unknown(schemas: dict, name: str) -> None:
    assert name in schemas, name
    node = schemas[name]
    assert node != {}, f"{name} must not be an empty schema"
    if "anyOf" in node:
        assert node["anyOf"], f"{name}.anyOf empty"
        return
    if node.get("type") == "object":
        props = node.get("properties") or {}
        addl = node.get("additionalProperties")
        assert props or addl not in (None, True, {}), (
            f"{name} object must declare properties or typed additionalProperties"
        )
        return
    assert "type" in node or "$ref" in node, f"{name} untyped: {node}"


def _enum_values(schema: dict) -> set[str]:
    if "enum" in schema:
        return set(schema["enum"])
    if "const" in schema:
        return {schema["const"]}
    values: set[str] = set()
    for key in ("anyOf", "oneOf", "allOf"):
        for item in schema.get(key, []) or []:
            values |= _enum_values(item)
    return values


def _assert_required_nullable(schemas: dict, model: str, field: str) -> None:
    node = schemas[model]
    assert field in node.get("required", []), (
        f"{model}.{field} must be required (present, may be null); "
        f"required={node.get('required')}"
    )
    prop = node["properties"][field]
    if "anyOf" in prop:
        kinds = {item.get("type") for item in prop["anyOf"] if isinstance(item, dict)}
        assert "null" in kinds, f"{model}.{field} not nullable: {prop}"
    else:
        assert prop.get("type") == "null" or "null" in (prop.get("type") or []), prop
    assert "default" not in prop, (
        f"{model}.{field} must not omit via default; got {prop}"
    )


def test_sales_order_line_composite_key_token_stable():
    """黄金样本:SalesOrderLine(order_no,line_no) 复合键 → 稳定 64 hex。"""
    keys = ["order_no", "line_no"]
    values = {"order_no": "SO-001", "line_no": 10}
    canonical = canonical_object_key_json(keys, values)
    assert canonical == '[["order_no","SO-001"],["line_no",10]]'
    token = object_key_token(keys, values)
    assert is_valid_lineage_key_token(token)
    assert token == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert token == object_key_token_from_pairs(
        [["order_no", "SO-001"], ["line_no", 10]]
    )
    # 类型变化敏感:字符串 "10" ≠ 数字 10
    assert object_key_token(keys, {"order_no": "SO-001", "line_no": "10"}) != token
    # 键序按 template 固定,不按字典排序改写
    assert object_key_token(keys, {"line_no": 10, "order_no": "SO-001"}) == token


def test_require_lineage_key_token_rejects_non_canonical():
    good = "a" * 64
    assert require_lineage_key_token(good) == good
    for bad in (
        "A" * 64,  # uppercase
        "a" * 63,
        "a" * 65,
        "g" * 64,  # non-hex
        "",
        "not-a-hash",
    ):
        with pytest.raises(Exception) as ei:
            require_lineage_key_token(bad)
        assert ei.value.reason_code == "lineage_key_invalid"


def test_openapi_lineage_route_and_named_schemas(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    assert LINEAGE_PATH in spec["paths"]
    op = spec["paths"][LINEAGE_PATH]["get"]
    content = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert _schema_ref(content) == "ObjectLineageResponse"
    assert "501" not in op["responses"]
    for code in ("401", "403", "404", "409", "422", "500"):
        assert code in op["responses"], code
    schemas = spec["components"]["schemas"]
    for name in RESPONSE_SCHEMA_NAMES:
        _assert_schema_not_unknown(schemas, name)
    # ObjectRowsPageResponse 加法扩展 lineage_refs
    rows = schemas["ObjectRowsPageResponse"]["properties"]
    assert "lineage_refs" in rows
    ref_items = rows["lineage_refs"]["items"]
    assert _schema_ref(ref_items) == "ObjectLineageRef"


def test_openapi_security_is_bearer_only(tmp_path):
    op = _openapi_app(tmp_path).openapi()["paths"][LINEAGE_PATH]["get"]
    assert op.get("security") == [{"HTTPBearer": []}]
    assert {} not in (op.get("security") or [])


def test_openapi_error_and_field_reason_codes(tmp_path):
    schemas = _openapi_app(tmp_path).openapi()["components"]["schemas"]
    error = schemas["ObjectLineageError"]["properties"]["reason_code"]
    assert ERROR_REASON_CODES <= _enum_values(error)
    field_reason = schemas["ObjectLineageField"]["properties"]["reason_code"]
    assert FIELD_UNAVAILABLE_REASONS <= _enum_values(field_reason)
    for field in ("status", "reason_code", "detail", "error_id"):
        assert field in schemas["ObjectLineageError"]["properties"]


def test_openapi_422_includes_lineage_error(tmp_path):
    op = _openapi_app(tmp_path).openapi()["paths"][LINEAGE_PATH]["get"]
    schema = op["responses"]["422"]["content"]["application/json"]["schema"]
    refs = {_schema_ref(x) for x in schema.get("anyOf", [])} or {_schema_ref(schema)}
    assert "ObjectLineageError" in refs, schema
    assert "RequestError" in refs, schema


def test_openapi_required_nullable_fields(tmp_path):
    schemas = _openapi_app(tmp_path).openapi()["components"]["schemas"]
    _assert_required_nullable(schemas, "ObjectLineageResponse", "reason_code")
    _assert_required_nullable(schemas, "ObjectLineageResponse", "source")
    _assert_required_nullable(schemas, "ObjectLineageField", "final_value")
    _assert_required_nullable(schemas, "ObjectLineageField", "reason_code")
    _assert_required_nullable(schemas, "ObjectLineageError", "error_id")
    assert "warnings" in schemas["ObjectLineageResponse"]["required"]
    assert "fields" in schemas["ObjectLineageResponse"]["required"]


def test_unavailable_old_dataset_response_shape():
    """旧 published(lineage_schema_version=NULL)成功体:200 unavailable。"""
    body = ObjectLineageResponse(
        state="unavailable",
        reason_code="lineage_not_recorded",
        source="digiwin_e10",
        object="SalesOrderLine",
        display_name="销售订单行",
        object_key=[["order_no", "SO-001"], ["line_no", 10]],
        key_token=object_key_token(
            ["order_no", "line_no"], {"order_no": "SO-001", "line_no": 10}
        ),
        dataset_version="ds_old",
        object_version="obj_old",
        template_version=None,
        binding_hash=None,
        binding_status=None,
        map_batch_id=None,
        fields=[],
        warnings=["该数据集构建时未记录字段血缘"],
        generated_at=datetime.now(timezone.utc),
    )
    dumped = body.model_dump(mode="json")
    assert dumped["state"] == "unavailable"
    assert dumped["reason_code"] == "lineage_not_recorded"
    assert dumped["fields"] == []
    assert is_valid_lineage_key_token(dumped["key_token"])


def test_unconfigured_token_returns_lineage_error(tmp_path):
    key = "a" * 64
    r = _client(tmp_path).get(f"/api/objects/SalesOrderLine/{key}/lineage")
    assert r.status_code == 403
    err = ObjectLineageError.model_validate(r.json())
    assert err.reason_code == "token_not_configured"
    assert err.error_id is None
    assert "fields" not in r.json()


def test_invalid_key_token_returns_422(tmp_path):
    client = _client(tmp_path, token=TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    for bad in ("AAAA" + "a" * 60, "a" * 63, "g" * 64, "not-hex"):
        r = client.get(f"/api/objects/SalesOrderLine/{bad}/lineage", headers=headers)
        assert r.status_code == 422, bad
        err = ObjectLineageError.model_validate(r.json())
        assert err.reason_code == "lineage_key_invalid"
        assert err.error_id is None
        assert "fields" not in r.json()


def test_valid_key_no_published_returns_409(tmp_path):
    """T07:合法 key 但无 published 数据集 → 409 dataset_not_published。"""
    client = _client(tmp_path, token=TOKEN)
    key = object_key_token(
        ["order_no", "line_no"], {"order_no": "SO-001", "line_no": 10}
    )
    r = client.get(
        f"/api/objects/SalesOrderLine/{key}/lineage",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["reason_code"] == "dataset_not_published"


def test_wrong_bearer_returns_unauthorized(tmp_path):
    client = _client(tmp_path, token=TOKEN)
    key = "b" * 64
    r = client.get(
        f"/api/objects/SalesOrderLine/{key}/lineage",
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401
    err = ObjectLineageError.model_validate(r.json())
    assert err.reason_code == "unauthorized"


def test_canonical_json_roundtrip_bytes():
    """紧凑 JSON 不含空格;UTF-8 编码稳定。"""
    raw = canonical_object_key_json(
        ["order_no", "line_no"], {"order_no": "订单-壹", "line_no": 1}
    )
    assert " " not in raw
    assert json.loads(raw) == [["order_no", "订单-壹"], ["line_no", 1]]
