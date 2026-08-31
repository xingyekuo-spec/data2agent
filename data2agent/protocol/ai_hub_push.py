"""AI Hub PUSH_AGENT v1 client contract (C1-B).

Digest and envelope rules match AI Hub `PUSH_PROTOCOL_VERSION = "1"`.
This module is a semantic transplant of the platform record digest, not a
copy of the data2agent table-level ingest v3 protocol.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

AI_HUB_PUSH_PROTOCOL_VERSION = "1"
AI_HUB_PUSH_API_PREFIX = "/platform-api/v1/ingest/push"
AI_HUB_PUSH_AUDIENCE = "ai-hub-platform"
AI_HUB_PUSH_SCOPE = "ai_hub.identity ai_hub.ingest.push"


def canonical_json_bytes(value: Any) -> bytes:
    """Canonical JSON used by PUSH_AGENT v1 digests and size accounting."""
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def canonical_json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def payload_size_bytes(payload: Mapping[str, Any] | None) -> int:
    """Single-record payload size: canonical JSON, not the batch envelope."""
    if payload is None:
        return 0
    return len(canonical_json_bytes(dict(payload)))


def records_content_bytes(records: Sequence[Mapping[str, Any]]) -> int:
    """Generation byte meter: UTF-8 object_id plus canonical payload per record."""
    total = 0
    for record in records:
        total += len(str(record["object_id"]).encode())
        total += payload_size_bytes(
            None if record.get("payload") is None else dict(record["payload"])
        )
    return total


def payload_content_hash(payload: Mapping[str, Any] | None) -> str:
    if payload is None:
        return hashlib.sha256(b"").hexdigest()
    return canonical_json_digest(dict(payload))


def batch_content_digest(records: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "object_id": record["object_id"],
            "operation": record["operation"],
            "version": record["version"],
            "payload": None if record.get("payload") is None else dict(record["payload"]),
            "content_hash": payload_content_hash(
                None if record.get("payload") is None else dict(record["payload"])
            ),
        }
        for record in records
    ]
    return canonical_json_digest(payload)


def ordered_batch_digest(batches: Sequence[Mapping[str, Any]]) -> str:
    return canonical_json_digest(
        [
            {
                "sequence_no": batch["sequence_no"],
                "external_batch_id": batch["external_batch_id"],
                "content_sha256": batch["content_sha256"],
            }
            for batch in batches
        ]
    )
