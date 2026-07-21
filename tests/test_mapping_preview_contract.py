"""v0.3 M3-T01: mapping preview API 契约冻结与失败测试。

门禁:OpenAPI 命名 schema、请求边界、非法草稿 422、端点 fail-closed(501)、
Bearer-only 安全声明、reason_code 字面量可见。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.console.app import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PREVIEW_PATH = "/api/mappings/{object}/preview"
PREVIEW_URL = "/api/mappings/Customer/preview"
SOURCE = "digiwin_e10"

ISSUE_REASON_CODES = {
    "enum_unmapped",
    "enum_invalid",
    "type_coercion",
    "derived_unmatched",
    "derived_invalid_enum",
    "business_key_missing",
    "business_key_duplicate",
}

ERROR_REASON_CODES = {
    "unauthorized",
    "token_not_configured",
    "object_not_found",
    "source_not_found",
    "raw_table_not_found",
    "sample_batch_not_found",
    "current_binding_unavailable",
    "raw_unavailable",
    "draft_invalid",
    "sample_invalid",
    "anchor_changed",
    "preview_failed",
}

RESPONSE_SCHEMA_NAMES = {
    "MappingPreviewResponse",
    "MappingPreviewRequest",
    "MappingPreviewError",
    "MappingPreviewEvaluation",
    "MappingPreviewSummary",
    "MappingPreviewRow",
    "MappingPreviewIssue",
    "MappingPreviewEnumGap",
    "MappingPreviewBusinessKeyIssues",
    "MappingPreviewDerivedCoverage",
    "MappingPreviewDiff",
    "MappingPreviewSampleInfo",
}


def _openapi_app(tmp_path):
    landing = tmp_path / "empty-landing.sqlite"
    return create_app(landing=str(landing), templates=str(ROOT / "templates"))


def _client(tmp_path) -> TestClient:
    return TestClient(_openapi_app(tmp_path))


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


def _valid_body(**overrides) -> dict:
    body = {
        "source": SOURCE,
        "sample": {"limit": 50, "offset": 0},
        "draft_binding": None,
    }
    body.update(overrides)
    return body


def test_openapi_preview_route_and_named_success_schema(tmp_path):
    spec = _openapi_app(tmp_path).openapi()
    assert PREVIEW_PATH in spec["paths"]
    op = spec["paths"][PREVIEW_PATH]["post"]
    content = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert _schema_ref(content) == "MappingPreviewResponse"
    assert "501" in op["responses"]
    schemas = spec["components"]["schemas"]
    _assert_schema_not_unknown(schemas, "MappingPreviewResponse")
    for name in RESPONSE_SCHEMA_NAMES:
        _assert_schema_not_unknown(schemas, name)


def test_openapi_request_limit_offset_bounds(tmp_path):
    schemas = _openapi_app(tmp_path).openapi()["components"]["schemas"]
    sample = schemas["MappingPreviewSample"]
    props = sample["properties"]
    assert props["limit"]["minimum"] == 1
    assert props["limit"]["maximum"] == 200
    assert props["offset"]["minimum"] == 0
    assert props["offset"]["maximum"] == 10000
    assert props["limit"].get("default") == 50
    assert props["offset"].get("default") == 0


def test_openapi_security_is_bearer_only(tmp_path):
    op = _openapi_app(tmp_path).openapi()["paths"][PREVIEW_PATH]["post"]
    assert op.get("security") == [{"HTTPBearer": []}]
    assert {} not in (op.get("security") or [])


def test_openapi_issue_and_error_reason_codes(tmp_path):
    schemas = _openapi_app(tmp_path).openapi()["components"]["schemas"]
    issue = schemas["MappingPreviewIssue"]["properties"]["reason_code"]
    assert ISSUE_REASON_CODES <= _enum_values(issue)
    error = schemas["MappingPreviewError"]["properties"]["reason_code"]
    assert ERROR_REASON_CODES <= _enum_values(error)
    for field in ("status", "reason_code", "detail", "error_id"):
        assert field in schemas["MappingPreviewError"]["properties"]


def test_valid_shaped_request_is_fail_closed_501(tmp_path):
    client = _client(tmp_path)
    r = client.post(PREVIEW_URL, json=_valid_body())
    assert r.status_code == 501
    assert r.json().get("detail")
    # 不得伪装成功或返回伪造成功体
    assert "object" not in r.json() or r.status_code != 200


@pytest.mark.parametrize(
    "sample",
    [
        {"limit": 0, "offset": 0},
        {"limit": 201, "offset": 0},
        {"limit": 50, "offset": -1},
        {"limit": 50, "offset": 10001},
    ],
)
def test_illegal_sample_bounds_return_422(tmp_path, sample):
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(sample=sample))
    assert r.status_code == 422


def test_oversized_draft_tables_return_422(tmp_path):
    draft = {
        "tables": [f"T{i}" for i in range(17)],
        "key_map": {},
        "field_map": {},
        "derived": {},
        "watermark": None,
        "notes": "",
    }
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(draft_binding=draft))
    assert r.status_code == 422


def test_oversized_draft_field_map_return_422(tmp_path):
    draft = {
        "tables": ["CUSTOMER"],
        "key_map": {},
        "field_map": {f"f{i}": f"col{i}" for i in range(129)},
        "derived": {},
        "watermark": None,
        "notes": "",
    }
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(draft_binding=draft))
    assert r.status_code == 422


def test_oversized_draft_notes_return_422(tmp_path):
    draft = {
        "tables": ["CUSTOMER"],
        "key_map": {},
        "field_map": {},
        "derived": {},
        "watermark": None,
        "notes": "x" * 2001,
    }
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(draft_binding=draft))
    assert r.status_code == 422


def test_oversized_derived_rules_return_422(tmp_path):
    draft = {
        "tables": ["CUSTOMER"],
        "key_map": {},
        "field_map": {},
        "derived": {
            "region": {
                "rules": [{"when": {"a": "1"}, "value": "X"} for _ in range(65)],
                "default": None,
            },
        },
        "watermark": None,
        "notes": "",
    }
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(draft_binding=draft))
    assert r.status_code == 422


def test_oversized_map_string_values_return_422(tmp_path):
    draft = {
        "tables": ["CUSTOMER"],
        "key_map": {"id": "x" * 513},
        "field_map": {},
        "derived": {},
        "watermark": None,
        "notes": "",
    }
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(draft_binding=draft))
    assert r.status_code == 422


def test_draft_rejects_client_forged_status(tmp_path):
    draft = {
        "tables": ["CUSTOMER"],
        "key_map": {},
        "field_map": {},
        "derived": {},
        "watermark": None,
        "notes": "",
        "status": "verified",
    }
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(draft_binding=draft))
    assert r.status_code == 422


def test_oversized_batch_id_return_422(tmp_path):
    r = _client(tmp_path).post(
        PREVIEW_URL,
        json=_valid_body(sample={"limit": 10, "offset": 0, "batch_id": "b" * 129}),
    )
    assert r.status_code == 422


def test_valid_draft_shape_reaches_fail_closed_stub(tmp_path):
    draft = {
        "tables": ["CUSTOMER"],
        "key_map": {"CustomerId": "Id"},
        "field_map": {"name": "Name"},
        "derived": {
            "tier": {
                "rules": [{"when": {"region": "CN"}, "value": "A"}],
                "default": "B",
            },
        },
        "watermark": "UpdateDate",
        "notes": "temp draft",
    }
    r = _client(tmp_path).post(PREVIEW_URL, json=_valid_body(draft_binding=draft))
    assert r.status_code == 501
