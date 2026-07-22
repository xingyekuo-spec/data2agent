"""M5 证据纯函数核心：canonical JSON、digest、normalized query 与 summary。"""

from __future__ import annotations

import hmac
import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from data2agent.connect.landing import LandingStore

EvidenceChannel = Literal["console", "mcp_stdio", "mcp_http", "demo"]

EVIDENCE_SCHEMA_VERSION = 1
SUMMARY_MAX_BYTES = 32 * 1024
OBJECT_SUMMARY_MAX_ROWS = 20
METRIC_SUMMARY_MAX_ITEMS = 50
MAX_STRING_LENGTH = 8192
MAX_PROPOSAL_EVIDENCE_ITEMS = 20
_VOLATILE_META_KEYS = {
    "query_id",
    "duration_ms",
    "created_at",
    "expires_at",
    "session_id",
    "evidence_scope",
    "result_digest",
    "result_summary",
    "warnings",
}


class EvidenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal: str = Field(min_length=1)
    session_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    channel: EvidenceChannel


class QueryEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: str
    evidence_schema_version: int = EVIDENCE_SCHEMA_VERSION
    principal: str
    session_id: str
    channel: EvidenceChannel
    source: str
    tool: Literal["query_objects", "query_metrics"]
    target: str
    normalized_query_json: str
    dataset_version: str | None = None
    template_version: str | None = None
    binding_hashes_json: str
    result_digest: str
    result_summary_json: str
    warnings_json: str
    row_count: int | None = None
    created_at: str
    expires_at: str


class ProposalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    evidence_schema_version: int = EVIDENCE_SCHEMA_VERSION
    principal: str
    session_id: str
    channel: EvidenceChannel
    source: str
    object: str
    action: str
    action_desc: str
    tier: str
    conclusion: str
    governance: str
    dataset_version: str | None = None
    created_at: str


class ProposalEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    evidence_ordinal: int = Field(ge=0)
    claim: str
    query_id: str
    query_tool: Literal["query_objects", "query_metrics"]
    query_target: str
    normalized_query_json: str
    dataset_version: str | None = None
    template_version: str | None = None
    binding_hashes_json: str
    result_digest: str
    result_summary_json: str
    warnings_json: str
    query_created_at: str


class GatewayAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    created_at: str
    principal: str
    session_id: str
    channel: EvidenceChannel
    source: str
    operation: str
    target: str
    outcome: str
    reason_code: str
    query_id: str | None = None
    proposal_id: str | None = None
    dataset_version: str | None = None
    result_digest: str | None = None
    detail_json: str


class EvidenceStore:
    """M5 持久证据存储包装：薄封装 LandingStore，保持事务由调用方控制。"""

    def __init__(self, landing: LandingStore):
        self.landing = landing

    def insert_query(self, record: QueryEvidenceRecord, *, commit: bool = True) -> None:
        self.landing.insert_gateway_query_evidence(record, commit=commit)

    def get_query(self, query_id: str) -> QueryEvidenceRecord | None:
        return self.landing.get_gateway_query_evidence(query_id)

    def insert_proposal(self, record: ProposalRecord, *, commit: bool = True) -> None:
        self.landing.insert_gateway_proposal(record, commit=commit)

    def get_proposal(self, proposal_id: str) -> ProposalRecord | None:
        return self.landing.get_gateway_proposal(proposal_id)

    def insert_proposal_evidence(
        self, records: list[ProposalEvidenceRecord], *, commit: bool = True,
    ) -> None:
        self.landing.insert_gateway_proposal_evidence(records, commit=commit)

    def list_proposal_evidence(self, proposal_id: str) -> list[ProposalEvidenceRecord]:
        return self.landing.list_gateway_proposal_evidence(proposal_id)

    def insert_audit(self, record: GatewayAuditRecord, *, commit: bool = True) -> None:
        self.landing.insert_gateway_audit(record, commit=commit)

    def list_audit(
        self, *, principal: str | None = None, session_id: str | None = None,
    ) -> list[GatewayAuditRecord]:
        return self.landing.list_gateway_audit(
            principal=principal, session_id=session_id,
        )


def _fail(detail: str) -> ValueError:
    return ValueError(f"evidence_invalid: {detail}")


def _normalize_json(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _fail("float must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise _fail("string too long")
        return value
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise _fail("object keys must be strings")
            out[key] = _normalize_json(value[key])
        return out
    raise _fail(f"unsupported json type: {type(value).__name__}")


def canonical_json_dumps(value: object) -> str:
    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: object) -> bytes:
    return canonical_json_dumps(value).encode("utf-8")


def is_valid_digest(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if not value.startswith("sha256:"):
        return False
    hex_part = value.removeprefix("sha256:")
    return len(hex_part) == 64 and all(ch in "0123456789abcdef" for ch in hex_part)


def constant_time_digest_equal(left: str, right: str) -> bool:
    if not (is_valid_digest(left) and is_valid_digest(right)):
        return False
    return hmac.compare_digest(left, right)


def normalize_query_objects(
    *,
    source: str,
    object_name: str,
    filters: dict[str, object] | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: int = 20,
) -> dict[str, object]:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise _fail("limit must be int")
    normalized_filters: dict[str, object] = {}
    for key, value in sorted((filters or {}).items()):
        if not isinstance(key, str):
            raise _fail("filter key must be string")
        if isinstance(value, (dict, list)):
            raise _fail("filter value must be scalar")
        normalized_filters[key] = _normalize_json(value)
    return {
        "tool": "query_objects",
        "source": source,
        "object": object_name,
        "filters": normalized_filters,
        "order_by": order_by,
        "desc": desc,
        "limit": max(1, min(limit, 200)),
    }


def normalize_query_metrics(
    *,
    source: str,
    metric: str,
    group_by: str,
    limit: int,
) -> dict[str, object]:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise _fail("limit must be int")
    return {
        "tool": "query_metrics",
        "source": source,
        "metric": metric,
        "group_by": group_by,
        "limit": max(1, min(limit, 200)),
    }


def build_result_envelope(
    *,
    tool: Literal["query_objects", "query_metrics"],
    source: str,
    target: str,
    normalized_query: dict[str, object],
    dataset_version: str | None,
    template_version: str | None,
    binding_hashes: dict[str, str],
    response_payload: dict[str, object],
) -> dict[str, object]:
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "tool": tool,
        "source": source,
        "target": target,
        "normalized_query": _normalize_json(normalized_query),
        "dataset_version": dataset_version,
        "template_version": template_version,
        "binding_hashes": _normalize_json(binding_hashes),
        "response_payload": _normalize_json(_stable_response_payload(response_payload)),
    }


def result_digest(envelope: dict[str, object]) -> str:
    stable = dict(envelope)
    payload = stable.get("response_payload")
    if isinstance(payload, dict):
        stable["response_payload"] = _stable_response_payload(payload)
    digest = hashlib.sha256(canonical_json_bytes(stable)).hexdigest()
    return f"sha256:{digest}"


def _stable_response_payload(response_payload: dict[str, object]) -> dict[str, object]:
    payload = dict(response_payload)
    meta = payload.get("meta")
    if isinstance(meta, dict):
        payload["meta"] = {
            key: value for key, value in meta.items() if key not in _VOLATILE_META_KEYS
        }
    return payload


def _bounded_summary(
    *,
    base: dict[str, object],
    preview_key: str,
    preview_items: list[dict[str, object]],
    preview_limit: int,
) -> dict[str, object]:
    preview = [_normalize_json(item) for item in preview_items[:preview_limit]]
    truncated = len(preview_items) > preview_limit
    summary = {**base, preview_key: preview, "preview_truncated": truncated}
    while preview and len(canonical_json_bytes(summary)) > SUMMARY_MAX_BYTES:
        preview = preview[:-1]
        summary = {**base, preview_key: preview, "preview_truncated": True}
    if len(canonical_json_bytes(summary)) > SUMMARY_MAX_BYTES:
        raise _fail("summary exceeds max bytes")
    return summary


def build_object_result_summary(
    *,
    columns: list[str],
    rows: list[dict[str, object]],
) -> dict[str, object]:
    return _bounded_summary(
        base={
            "kind": "query_objects",
            "returned_row_count": len(rows),
            "columns": _normalize_json(columns),
        },
        preview_key="rows_preview",
        preview_items=rows,
        preview_limit=OBJECT_SUMMARY_MAX_ROWS,
    )


def build_metric_result_summary(
    *,
    metric: str,
    status: str,
    unit: str | None,
    group_by: str | None,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    return _bounded_summary(
        base={
            "kind": "query_metrics",
            "metric": metric,
            "status": status,
            "unit": unit,
            "group_by": group_by,
            "returned_row_count": len(rows),
        },
        preview_key="series_preview",
        preview_items=rows,
        preview_limit=METRIC_SUMMARY_MAX_ITEMS,
    )
