"""C1-B: AiHubObjectPushSink against an in-process mock server (no real AI Hub)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest

from data2agent.middle.extract.ai_hub_object_map import (
    ObjectBinding,
    ObjectMappingError,
    map_row,
    object_id_from_row,
)
from data2agent.middle.extract.ai_hub_object_push_sink import (
    AiHubObjectPushSink,
    AiHubProtocolError,
    AiHubPushRejected,
    ObjectVersionStore,
    PendingBatchSpool,
    oidc_client_credentials_provider,
)
from data2agent.middle.extract.scheduler import build_sink
from data2agent.middle.extract.sink import HttpPushSink
from data2agent.protocol.ai_hub_push import (
    AI_HUB_PUSH_PROTOCOL_VERSION,
    batch_content_digest,
    ordered_batch_digest,
    payload_size_bytes,
    records_content_bytes,
)
from data2agent.shared.config import (
    ConnectConfig,
    SinkConfig,
    SourceConfig,
    SpoolConfig,
    TableExtractConfig,
    assert_production_ready,
)
from data2agent.shared.store.landing import LandingStore
from data2agent.shared.store.table import TableInfo

FINGERPRINT = "a" * 64
BINDING = ObjectBinding(
    object_type="erp.item",
    payload_contract_version="item.v1",
    schema_fingerprint=FINGERPRINT,
    payload_columns=("ITEM_CODE", "ITEM_NAME"),
)
DELETE_BINDING = ObjectBinding(
    object_type="erp.item",
    payload_contract_version="item.v1",
    schema_fingerprint=FINGERPRINT,
    payload_columns=("ITEM_CODE", "ITEM_NAME"),
    delete_flag_column="DELETED",
)
ITEM = TableInfo(
    name="ITEM",
    columns=[("ITEM_CODE", "text"), ("ITEM_NAME", "text"), ("DELETED", "int")],
    pk=["ITEM_CODE"],
)


def _http_error(url: str, code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url,
        code,
        "error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


class AiHubPushMock:
    def __init__(self) -> None:
        self.enabled = True
        self.protocol_versions = [AI_HUB_PUSH_PROTOCOL_VERSION]
        self.page_limit_max = 5000
        self.payload_max_bytes = 65536
        self.max_generation_rows = 100000
        self.max_batches = 1000
        self.max_generation_bytes = 50_000_000
        self.complete_status = "COMPLETED"
        self.poll_complete_status = "COMPLETED"
        self.generations: dict[str, dict] = {}
        self.complete_payloads: list[dict] = []
        self.batch_payloads: list[dict] = []
        self.committed_high_watermark: dict[str, int] = {}
        self.omit_receipt_fields: set[str] = set()
        self.empty_receipt = False
        self.heartbeats = 0
        self.aborts = 0
        self.complete_reject_writable = False
        self.complete_http_reject = False

    def get_json(self, url: str, token: str | None, timeout: float) -> dict:
        del token, timeout
        if url.endswith("/capabilities"):
            return {
                "enabled": self.enabled,
                "protocol_versions": self.protocol_versions,
                "contract_required": True,
                "payload_max_bytes": self.payload_max_bytes,
                "page_limit_max": self.page_limit_max,
                "active_generation_limit": 1,
                "max_batches": self.max_batches,
                "max_generation_rows": self.max_generation_rows,
                "max_generation_bytes": self.max_generation_bytes,
                "max_generation_lifetime_seconds": 3600,
            }
        marker = "/generations/"
        if marker in url:
            generation_id = url.rsplit(marker, 1)[1]
            if "/" not in generation_id:
                generation = self.generations[generation_id]
                if generation.get("status") == "COMPLETING":
                    generation["status"] = self.poll_complete_status
                return generation
        raise AssertionError(url)

    def post(self, url: str, payload: dict | None, token: str | None, timeout: float):
        del token, timeout
        if url.endswith("/generations"):
            return self._create(url, payload or {})
        if url.endswith("/heartbeat"):
            generation_id = url.rsplit("/generations/", 1)[1].split("/", 1)[0]
            generation = self.generations[generation_id]
            if generation["status"] not in {"OPEN", "RECEIVING"}:
                raise _http_error(
                    url, 409, {"error_code": "generation_not_writable", "message": "expired"}
                )
            self.heartbeats += 1
            return generation
        if url.endswith("/batches"):
            return self._batch(url, payload or {})
        if url.endswith("/complete"):
            return self._complete(url, payload or {})
        if url.endswith("/abort"):
            generation_id = url.rsplit("/generations/", 1)[1].split("/", 1)[0]
            generation = self.generations[generation_id]
            if generation["status"] not in {"OPEN", "RECEIVING"}:
                raise _http_error(
                    url,
                    409,
                    {"error_code": "generation_not_active", "message": "not active"},
                )
            generation["status"] = "ABORTED"
            self.aborts += 1
            return generation
        raise AssertionError(url)

    def _create(self, url: str, payload: dict) -> dict:
        object_type = payload["object_type"]
        external_id = payload["external_generation_id"]
        for existing in self.generations.values():
            if existing["external_generation_id"] == external_id:
                return existing
        for existing in self.generations.values():
            if (
                existing["object_type"] == object_type
                and existing["status"] in {"OPEN", "RECEIVING", "COMPLETING"}
            ):
                raise _http_error(
                    url,
                    409,
                    {"error_code": "generation_in_progress", "message": "active"},
                )
        generation_id = str(uuid4())
        generation = {
            "generation_id": generation_id,
            "source_application_id": payload["source_application_id"],
            "object_type": object_type,
            "external_generation_id": external_id,
            "sync_mode": payload["sync_mode"],
            "status": "OPEN",
            "next_sequence_no": 1,
            "purpose": payload.get("purpose", "production"),
            "accepted": [],
            "high_watermark": 0,
            "total_bytes": 0,
        }
        self.generations[generation_id] = generation
        return generation

    def _batch(self, url: str, payload: dict) -> dict:
        generation_id = url.rsplit("/generations/", 1)[1].split("/", 1)[0]
        generation = self.generations[generation_id]
        if generation["status"] not in {"OPEN", "RECEIVING"}:
            raise _http_error(
                url, 409, {"error_code": "generation_not_writable", "message": "expired"}
            )
        records = payload["records"]
        expected = generation["next_sequence_no"]
        for accepted in generation["accepted"]:
            if accepted["sequence_no"] == payload["sequence_no"]:
                digest = batch_content_digest(records)
                return {
                    "idempotent": True,
                    "sequence_no": payload["sequence_no"],
                    "external_batch_id": payload["external_batch_id"],
                    "content_sha256": digest,
                    "record_count": len(records),
                    "high_watermark": payload["high_watermark"],
                }
        if payload["sequence_no"] != expected:
            raise _http_error(
                url,
                409,
                {
                    "error_code": "sequence_gap",
                    "message": "gap",
                    "details": {"expected_sequence_no": expected},
                },
            )
        records = payload["records"]
        digest = batch_content_digest(records)
        if payload["content_sha256"] != digest:
            raise _http_error(
                url,
                409,
                {"error_code": "batch_digest_conflict", "message": "digest"},
            )
        for record in records:
            size = payload_size_bytes(
                None if record.get("payload") is None else record.get("payload")
            )
            if size > self.payload_max_bytes:
                raise _http_error(
                    url,
                    400,
                    {"error_code": "payload_too_large", "message": "payload"},
                )
        content_bytes = records_content_bytes(records)
        if generation.get("total_bytes", 0) + content_bytes > self.max_generation_bytes:
            raise _http_error(
                url,
                409,
                {"error_code": "generation_limit_exceeded", "message": "bytes"},
            )
        committed = self.committed_high_watermark.get(generation["object_type"], 0)
        previous = int(generation.get("high_watermark") or 0)
        if int(payload["high_watermark"]) < max(committed, previous):
            raise _http_error(
                url,
                409,
                {
                    "error_code": "generation_complete_mismatch",
                    "message": "high_watermark regression",
                    "details": {
                        "minimum_high_watermark": max(committed, previous),
                    },
                },
            )
        self.batch_payloads.append(payload)
        if self.empty_receipt:
            return {}
        receipt = {
            "idempotent": False,
            "sequence_no": payload["sequence_no"],
            "external_batch_id": payload["external_batch_id"],
            "content_sha256": digest,
            "record_count": len(records),
            "high_watermark": payload["high_watermark"],
        }
        for field in self.omit_receipt_fields:
            receipt.pop(field, None)
        if self.omit_receipt_fields:
            return receipt
        generation["accepted"].append(
            {
                "sequence_no": payload["sequence_no"],
                "external_batch_id": payload["external_batch_id"],
                "content_sha256": digest,
                "record_count": len(records),
                "high_watermark": payload["high_watermark"],
            }
        )
        generation["next_sequence_no"] = expected + 1
        generation["status"] = "RECEIVING"
        generation["high_watermark"] = int(payload["high_watermark"])
        generation["total_bytes"] = generation.get("total_bytes", 0) + content_bytes
        if generation["sync_mode"] == "incremental":
            self.committed_high_watermark[generation["object_type"]] = int(
                payload["high_watermark"]
            )
        return receipt

    def _complete(self, url: str, payload: dict) -> dict:
        generation_id = url.rsplit("/generations/", 1)[1].split("/", 1)[0]
        generation = self.generations[generation_id]
        self.complete_payloads.append(dict(payload))
        if self.complete_http_reject:
            raise _http_error(
                url,
                400,
                {
                    "error_code": "generation_complete_mismatch",
                    "message": "rejected",
                },
            )
        expected = ordered_batch_digest(generation["accepted"])
        if payload["ordered_batch_digest"] != expected:
            if self.complete_reject_writable:
                raise _http_error(
                    url,
                    400,
                    {
                        "error_code": "generation_complete_mismatch",
                        "message": "digest",
                    },
                )
            generation["status"] = "FAILED"
            generation["error_code"] = "generation_complete_mismatch"
            return generation
        if payload["expected_batch_count"] != len(generation["accepted"]):
            generation["status"] = "FAILED"
            generation["error_code"] = "generation_complete_mismatch"
            return generation
        committed = self.committed_high_watermark.get(generation["object_type"], 0)
        persisted_hw = max(
            (int(batch.get("high_watermark") or 0) for batch in generation["accepted"]),
            default=int(generation.get("high_watermark") or 0),
        )
        if int(payload["high_watermark"]) < max(committed, persisted_hw, 0):
            generation["status"] = "FAILED"
            generation["error_code"] = "generation_complete_mismatch"
            return generation
        generation["status"] = self.complete_status
        if self.complete_status == "COMPLETED":
            self.committed_high_watermark[generation["object_type"]] = int(
                payload["high_watermark"]
            )
        return generation


def _sink(tmp_path: Path, mock: AiHubPushMock, **kwargs) -> AiHubObjectPushSink:
    kwargs.setdefault("bindings", {"ITEM": BINDING})
    kwargs.setdefault("state_path", tmp_path / "push-state.sqlite")
    kwargs.setdefault("spool_directory", tmp_path / "push-spool")
    kwargs.setdefault("timeout", 1)
    kwargs.setdefault("retries", 1)
    kwargs.setdefault("complete_poll_interval", 0)
    return AiHubObjectPushSink(
        "http://ai-hub.test",
        source_application_id="e10-adapter",
        token="test-token",
        post=mock.post,
        get_json=mock.get_json,
        **kwargs,
    )


def test_http_sink_is_unchanged_table_protocol():
    sink = HttpPushSink(
        "http://platform",
        post=lambda *a, **k: None,
        get_json=lambda *a, **k: {
            "supported_ingest_protocol_versions": ["3"],
            "ingest_protocol_version": "3",
            "active_ingest_protocol_version": "3",
        },
    )
    sink.ensure_protocol()
    assert sink.url == "http://platform"


def test_build_sink_http_still_returns_http_push_sink(tmp_path: Path):
    landing = LandingStore(tmp_path / "state.sqlite")
    scfg = SourceConfig(
        adapter="sqlite_readonly",
        path="source.sqlite",
        tables={"ITEM": TableExtractConfig(mode="full_refresh")},
        sink=SinkConfig(type="http", url="http://127.0.0.1:8850", token_env="T"),
    )
    sink = build_sink(scfg, landing, source="e10")
    assert isinstance(sink, HttpPushSink)


def test_incremental_and_paged_full_then_one_complete(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_sync("e10", ["ITEM"], 1)
    sink.begin_table("e10", ITEM, mode="full_refresh", snapshot_id="snap")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1", mode="full_refresh")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-2", "ITEM_NAME": "b"}], "b2", mode="full_refresh")
    sink.complete_table("e10", ITEM, "done", 2, 2, mode="full_refresh")
    assert len(mock.batch_payloads) == 2
    assert mock.batch_payloads[0]["sequence_no"] == 1
    assert mock.batch_payloads[1]["sequence_no"] == 2
    assert len(mock.complete_payloads) == 1
    assert mock.complete_payloads[0]["expected_batch_count"] == 2
    generation = next(iter(mock.generations.values()))
    assert generation["status"] == "COMPLETED"
    assert generation["sync_mode"] == "full"


def test_complete_retry_replays_same_digest(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    sink.complete_table("e10", ITEM, "done", 1, 1)
    sink.complete_table("e10", ITEM, "done", 1, 1)
    assert len(mock.complete_payloads) == 2
    assert mock.complete_payloads[0] == mock.complete_payloads[1]


def test_sequence_gap_fail_closed(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation = sink._active["ITEM"]
    generation.next_sequence_no = 3
    with pytest.raises(AiHubPushRejected, match="sequence_gap"):
        sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    assert mock.batch_payloads == []


def test_digest_conflict_fail_closed(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    original_post = mock.post

    def tamper(url, payload, token, timeout):
        if url.endswith("/batches") and payload is not None:
            payload = dict(payload)
            payload["content_sha256"] = "b" * 64
        return original_post(url, payload, token, timeout)

    sink._post = tamper
    with pytest.raises(AiHubPushRejected, match="batch_digest_conflict"):
        sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")


def test_begin_table_resumes_in_progress_generation(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation_id = sink._active["ITEM"].generation_id
    sink.begin_table("e10", ITEM, mode="incremental")
    assert sink._active["ITEM"].generation_id == generation_id
    assert len(mock.generations) == 1


def test_lost_state_hits_generation_in_progress(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    other = _sink(tmp_path, mock, state_path=tmp_path / "other-state.sqlite")
    with pytest.raises(AiHubPushRejected, match="generation_in_progress"):
        other.begin_table("e10", ITEM, mode="incremental")


def test_expired_generation_rejects_batch(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation_id = sink._active["ITEM"].generation_id
    mock.generations[generation_id]["status"] = "EXPIRED"
    with pytest.raises(AiHubPushRejected, match="generation_not_writable"):
        sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")


def test_capabilities_disabled_fail_closed(tmp_path: Path):
    mock = AiHubPushMock()
    mock.enabled = False
    sink = _sink(tmp_path, mock)
    with pytest.raises(AiHubProtocolError, match="未启用"):
        sink.ensure_protocol()


def test_delete_and_stable_version_on_retry(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock, bindings={"ITEM": DELETE_BINDING})
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write(
        "e10",
        ITEM,
        [{"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": 1}],
        "b1",
    )
    record = mock.batch_payloads[0]["records"][0]
    assert record["operation"] == "delete"
    assert record["payload"] is None
    assert record["version"] == 1
    # Same source content under a new caller batch_id is treated as already
    # confirmed for this generation (crash-before-cursor replay).
    assert (
        sink.write(
            "e10",
            ITEM,
            [{"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": 1}],
            "b2",
        )
        == 1
    )
    assert len(mock.batch_payloads) == 1
    assert sink._active["ITEM"].total_rows == 1
    sink.complete_table("e10", ITEM, "done", 1, 1)
    restarted = _sink(tmp_path, mock, bindings={"ITEM": DELETE_BINDING})
    restarted.begin_table("e10", ITEM, mode="incremental")
    restarted.write(
        "e10",
        ITEM,
        [{"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": 1}],
        "b3",
    )
    assert mock.batch_payloads[1]["records"][0]["version"] == 1


def test_abort_unpublished_full(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="full_refresh", snapshot_id="snap")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1", mode="full_refresh")
    sink.abort_table("e10", ITEM, mode="full_refresh", snapshot_id="snap")
    generation = next(iter(mock.generations.values()))
    assert generation["status"] == "ABORTED"


def test_sqlite_full_extract_emits_object_envelope(tmp_path: Path):
    import sqlite3

    from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
    from data2agent.middle.extract.increment import incremental_sync

    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE ITEM (ITEM_CODE TEXT PRIMARY KEY, ITEM_NAME TEXT)")
    con.execute("INSERT INTO ITEM VALUES ('I-1', 'alpha')")
    con.commit()
    con.close()
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), {"ITEM"})
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    incremental_sync(
        adapter,
        landing,
        "e10",
        watermarks={},
        sink=sink,
        only_tables={"ITEM"},
        estimate_rows=False,
    )
    assert mock.batch_payloads
    record = mock.batch_payloads[0]["records"][0]
    assert record["object_id"] == '["I-1"]'
    assert record["operation"] == "upsert"
    assert record["payload"]["ITEM_NAME"] == "alpha"
    assert "ITEM_CODE" in record["payload"]
    assert next(iter(mock.generations.values()))["status"] == "COMPLETED"


def test_ai_hub_config_requires_object_binding(tmp_path: Path):
    with pytest.raises(ValueError, match="object_type"):
        SourceConfig(
            adapter="sqlite_readonly",
            path="source.sqlite",
            tables={"ITEM": TableExtractConfig(mode="full_refresh")},
            sink=SinkConfig(
                type="ai_hub",
                url="http://127.0.0.1:8000",
                source_application_id="e10-adapter",
                allow_insecure_http=True,
                allow_unauthenticated=True,
            ),
            spool=_ai_hub_encrypted_spool(tmp_path),
        )


def test_ai_hub_rejects_strict_stream_spool(tmp_path: Path):
    with pytest.raises(ValueError, match="encrypted_temp_volume"):
        SourceConfig(
            adapter="sqlite_readonly",
            path="source.sqlite",
            tables={"ITEM": _ai_hub_item_spec()},
            sink=SinkConfig(
                type="ai_hub",
                url="http://127.0.0.1:8000",
                source_application_id="e10-adapter",
                allow_insecure_http=True,
                allow_unauthenticated=True,
            ),
            spool=SpoolConfig(policy="strict_stream"),
        )


def _ai_hub_item_spec(**kwargs) -> TableExtractConfig:
    fields = dict(
        mode="full_refresh",
        object_type="erp.item",
        payload_contract_version="item.v1",
        payload_schema_fingerprint=FINGERPRINT,
        payload_columns=["ITEM_CODE", "ITEM_NAME"],
    )
    fields.update(kwargs)
    return TableExtractConfig(**fields)


def _ai_hub_encrypted_spool(tmp_path: Path) -> SpoolConfig:
    directory = tmp_path / "encrypted-spool"
    directory.mkdir(parents=True, exist_ok=True)
    return SpoolConfig(
        policy="encrypted_temp_volume",
        directory=str(directory),
        encrypted_at_rest=True,
    )


def test_object_id_is_injective_for_composite_keys():
    from data2agent.middle.extract.ai_hub_object_map import object_id_from_parts

    assert object_id_from_parts(["a|b", "c"]) != object_id_from_parts(["a", "b|c"])
    info = TableInfo(
        name="ITEM",
        columns=[("A", "text"), ("B", "text")],
        pk=["A", "B"],
    )
    left = object_id_from_row(info, {"A": "a|b", "B": "c"})
    right = object_id_from_row(info, {"A": "a", "B": "b|c"})
    assert left != right


def test_delete_flag_requires_known_values():
    from data2agent.middle.extract.ai_hub_object_map import map_row

    assert map_row(
        ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": "N"}, DELETE_BINDING, 1
    )["operation"] == "upsert"
    assert map_row(
        ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": 0}, DELETE_BINDING, 1
    )["operation"] == "upsert"
    assert map_row(
        ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": "false"}, DELETE_BINDING, 1
    )["operation"] == "upsert"
    assert map_row(
        ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": 1}, DELETE_BINDING, 1
    )["operation"] == "delete"
    with pytest.raises(ObjectMappingError, match="不在允许集合"):
        map_row(
            ITEM,
            {"ITEM_CODE": "I-1", "ITEM_NAME": "a", "DELETED": "maybe"},
            DELETE_BINDING,
            1,
        )
    missing_col = ObjectBinding(
        object_type="erp.item",
        payload_contract_version="item.v1",
        schema_fingerprint=FINGERPRINT,
        payload_columns=("ITEM_CODE",),
        delete_flag_column="NOT_A_COLUMN",
    )
    with pytest.raises(ObjectMappingError, match="不在表结构中"):
        map_row(ITEM, {"ITEM_CODE": "I-1"}, missing_col, 1)
    with pytest.raises(ObjectMappingError, match="行缺少删除标志列"):
        map_row(ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a"}, DELETE_BINDING, 1)


def test_write_splits_to_page_limit(tmp_path: Path):
    mock = AiHubPushMock()
    mock.page_limit_max = 2
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    rows = [
        {"ITEM_CODE": f"I-{i}", "ITEM_NAME": str(i)}
        for i in range(5)
    ]
    written = sink.write("e10", ITEM, rows, "b1")
    assert written == 5
    assert len(mock.batch_payloads) == 3
    assert [len(p["records"]) for p in mock.batch_payloads] == [2, 2, 1]
    assert [p["sequence_no"] for p in mock.batch_payloads] == [1, 2, 3]
    sink.complete_table("e10", ITEM, "done", 5, 1)
    assert mock.complete_payloads[0]["expected_batch_count"] == 3
    assert mock.complete_payloads[0]["total_rows"] == 5


def test_write_prechecks_generation_row_limit(tmp_path: Path):
    mock = AiHubPushMock()
    mock.max_generation_rows = 1
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    with pytest.raises(AiHubPushRejected, match="max_generation_rows"):
        sink.write(
            "e10",
            ITEM,
            [
                {"ITEM_CODE": "I-1", "ITEM_NAME": "a"},
                {"ITEM_CODE": "I-2", "ITEM_NAME": "b"},
            ],
            "b1",
        )
    assert mock.batch_payloads == []


def test_complete_requires_completed_status(tmp_path: Path):
    mock = AiHubPushMock()
    mock.complete_status = ""
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    with pytest.raises(AiHubPushRejected, match="未知或空状态"):
        sink.complete_table("e10", ITEM, "done", 1, 1)


def test_complete_polls_completing_until_completed(tmp_path: Path):
    mock = AiHubPushMock()
    mock.complete_status = "COMPLETING"
    mock.poll_complete_status = "COMPLETED"
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    sink.complete_table("e10", ITEM, "done", 1, 1)
    assert next(iter(mock.generations.values()))["status"] == "COMPLETED"


def test_http_sink_rejects_oidc_fields():
    with pytest.raises(ValueError, match="OIDC 仅用于"):
        SinkConfig(
            type="http",
            url="http://127.0.0.1:8850",
            token_env="T",
            oidc_token_url="https://idp.example/token",
            oidc_client_id="client",
            oidc_client_secret_env="SECRET",
        )


def test_oidc_token_url_requires_https_before_secret(tmp_path: Path):
    from data2agent.middle.extract.ai_hub_object_push_sink import (
        oidc_client_credentials_provider,
    )
    from data2agent.shared.config import load_config

    with pytest.raises(ValueError, match="HTTPS"):
        oidc_client_credentials_provider(
            "http://idp.example/token",
            "client",
            "secret",
            audience="a",
            scope="s",
        )
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        "landing: l.sqlite\n"
        "sources:\n"
        "  e10:\n"
        "    adapter: sqlite_readonly\n"
        "    path: s.sqlite\n"
        "    tables:\n"
        "      ITEM:\n"
        "        mode: full_refresh\n"
        f"        object_type: erp.item\n"
        "        payload_contract_version: item.v1\n"
        f"        payload_schema_fingerprint: {FINGERPRINT}\n"
        "        payload_columns: [ITEM_CODE]\n"
        "    sink:\n"
        "      type: ai_hub\n"
        "      url: http://127.0.0.1:8000\n"
        "      source_application_id: e10-adapter\n"
        "      oidc_token_url: http://idp.example/token\n"
        "      oidc_client_id: client\n"
        "      oidc_client_secret_env: SECRET\n"
        "    spool:\n"
        "      policy: encrypted_temp_volume\n"
        f"      directory: {tmp_path / 'spool'}\n"
        "      encrypted_at_rest: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="oidc_token_url"):
        load_config(cfg)
    oidc_client_credentials_provider(
        "http://127.0.0.1:8080/token",
        "client",
        "secret",
        audience="a",
        scope="s",
    )


def test_ai_hub_rejects_reconcile_schedule(tmp_path: Path):
    with pytest.raises(ValueError, match="不支持对账"):
        SourceConfig(
            adapter="sqlite_readonly",
            path="source.sqlite",
            tables={"ITEM": _ai_hub_item_spec()},
            reconcile_at="05:30",
            sink=SinkConfig(
                type="ai_hub",
                url="http://127.0.0.1:8000",
                source_application_id="e10-adapter",
                allow_insecure_http=True,
                allow_unauthenticated=True,
            ),
            spool=_ai_hub_encrypted_spool(tmp_path),
        )


def test_run_reconcile_cycle_skips_ai_hub(tmp_path: Path):
    from data2agent.middle.extract.scheduler import run_reconcile_cycle

    landing = tmp_path / "state.sqlite"
    LandingStore(landing)
    scfg = SourceConfig(
        adapter="sqlite_readonly",
        path="source.sqlite",
        tables={"ITEM": _ai_hub_item_spec()},
        sink=SinkConfig(
            type="ai_hub",
            url="http://127.0.0.1:8000",
            source_application_id="e10-adapter",
            allow_insecure_http=True,
            allow_unauthenticated=True,
        ),
        spool=_ai_hub_encrypted_spool(tmp_path),
    )
    assert run_reconcile_cycle("e10", scfg, str(landing)) is False


def test_production_rejects_ai_hub_until_c1c(tmp_path: Path):
    cfg = ConnectConfig(
        deployment_mode="production",
        sources={"e10": SourceConfig(
            adapter="sqlite_readonly",
            path="source.sqlite",
            tables={"ITEM": _ai_hub_item_spec()},
            sink=SinkConfig(
                type="ai_hub",
                url="https://ai-hub.example",
                source_application_id="e10-adapter",
                token_env="T",
            ),
            spool=_ai_hub_encrypted_spool(tmp_path),
        )},
    )
    violations = cfg.production_violations()
    assert any("C1-B 未生产启用" in item for item in violations)
    with pytest.raises(ValueError, match="C1-B 未生产启用"):
        assert_production_ready(cfg)


def test_object_type_unique_per_source(tmp_path: Path):
    with pytest.raises(ValueError, match="object_type"):
        SourceConfig(
            adapter="sqlite_readonly",
            path="source.sqlite",
            tables={
                "ITEM": _ai_hub_item_spec(),
                "ITEM_WAREHOUSE": _ai_hub_item_spec(),
            },
            sink=SinkConfig(
                type="ai_hub",
                url="http://127.0.0.1:8000",
                source_application_id="e10-adapter",
                allow_insecure_http=True,
                allow_unauthenticated=True,
            ),
            spool=_ai_hub_encrypted_spool(tmp_path),
        )


def test_source_application_id_unique_across_sources(tmp_path: Path):
    item = _ai_hub_item_spec()
    sink = SinkConfig(
        type="ai_hub",
        url="http://127.0.0.1:8000",
        source_application_id="shared-id",
        allow_insecure_http=True,
        allow_unauthenticated=True,
    )
    spool = _ai_hub_encrypted_spool(tmp_path)
    with pytest.raises(ValueError, match="source_application_id"):
        ConnectConfig(sources={
            "a": SourceConfig(
                adapter="sqlite_readonly", path="a.sqlite",
                tables={"ITEM": item}, sink=sink, spool=spool,
            ),
            "b": SourceConfig(
                adapter="sqlite_readonly", path="source.sqlite",
                tables={"ITEM": _ai_hub_item_spec(object_type="erp.other")},
                sink=SinkConfig(
                    type="ai_hub",
                    url="http://127.0.0.1:8000",
                    source_application_id="shared-id",
                    allow_insecure_http=True,
                    allow_unauthenticated=True,
                ),
                spool=spool,
            ),
        })


def test_versions_live_in_state_db_and_survive_backup(tmp_path: Path):
    landing = LandingStore(tmp_path / "state.sqlite")
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock, state_path=landing.db_path)
    sink.begin_table("renamed-yaml-source", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    sink.complete_table("e10", ITEM, "done", 1, 1)
    sink._store.close()
    rows = landing.con.execute(
        "SELECT source_application_id, version FROM d2a_aihub_object_version"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "e10-adapter"
    assert rows[0][1] == 1
    backup = landing.backup_to(tmp_path / "bak.sqlite")
    restored = LandingStore(backup)
    restarted = _sink(tmp_path, mock, state_path=restored.db_path)
    restarted.begin_table("other-yaml-name", ITEM, mode="incremental")
    restarted.write("ignored", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b2")
    assert mock.batch_payloads[-1]["records"][0]["version"] == 1


def test_payload_max_bytes_is_per_record_canonical_size(tmp_path: Path):
    from data2agent.protocol.ai_hub_push import payload_size_bytes

    mock = AiHubPushMock()
    payload = {"ITEM_CODE": "I-1", "ITEM_NAME": "中文名称"}
    canonical = payload_size_bytes(payload)
    compact_utf8 = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    assert canonical > compact_utf8
    mock.payload_max_bytes = compact_utf8
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    with pytest.raises(AiHubPushRejected, match="payload_max_bytes"):
        sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "中文名称"}], "b1")
    assert mock.batch_payloads == []
    mock.payload_max_bytes = canonical
    sink._capabilities = None
    written = sink.write(
        "e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "中文名称"}], "b1"
    )
    assert written == 1
    assert mock.batch_payloads[0]["records"][0]["payload"]["ITEM_NAME"] == "中文名称"


def test_oidc_token_is_cached_until_expiry(monkeypatch):
    calls: list[int] = []

    class FakeResp:
        def read(self):
            return json.dumps({"access_token": "tok", "expires_in": 3600}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(1)
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    now = {"t": 1000.0}
    provider = oidc_client_credentials_provider(
        "https://idp.example/token",
        "client",
        "secret",
        audience="a",
        scope="s",
    )
    provider._clock = lambda: now["t"]
    assert provider() == "tok"
    assert provider() == "tok"
    assert len(calls) == 1
    now["t"] = 1000.0 + 3600
    assert provider() == "tok"
    assert len(calls) == 2
    provider.invalidate()
    assert provider() == "tok"
    assert len(calls) == 3


def test_ai_hub_sink_wires_ca_bundle(tmp_path: Path):
    with pytest.raises((FileNotFoundError, OSError)):
        AiHubObjectPushSink(
            "https://ai-hub.test",
            source_application_id="e10-adapter",
            bindings={"ITEM": BINDING},
            ca_bundle=str(tmp_path / "missing-ca.pem"),
            spool_directory=tmp_path / "spool",
        )


def test_build_sink_ai_hub_uses_state_db(tmp_path: Path):
    landing = LandingStore(tmp_path / "state.sqlite")
    scfg = SourceConfig(
        adapter="sqlite_readonly",
        path="source.sqlite",
        tables={"ITEM": _ai_hub_item_spec()},
        sink=SinkConfig(
            type="ai_hub",
            url="http://127.0.0.1:8000",
            source_application_id="e10-adapter",
            allow_insecure_http=True,
            allow_unauthenticated=True,
        ),
        spool=_ai_hub_encrypted_spool(tmp_path),
    )
    sink = build_sink(scfg, landing, source="e10")
    assert isinstance(sink, AiHubObjectPushSink)
    assert Path(sink._store.path) == Path(landing.db_path)


def test_global_version_watermark_does_not_regress_on_lookback(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "A", "ITEM_NAME": "a"}], "b1")
    sink.write("e10", ITEM, [{"ITEM_CODE": "B", "ITEM_NAME": "b"}], "b2")
    assert mock.batch_payloads[0]["records"][0]["version"] == 1
    assert mock.batch_payloads[1]["records"][0]["version"] == 2
    assert mock.batch_payloads[1]["high_watermark"] == 2
    sink.complete_table("e10", ITEM, "done", 2, 2)
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    restarted.write("e10", ITEM, [{"ITEM_CODE": "A", "ITEM_NAME": "a"}], "b3")
    assert mock.batch_payloads[-1]["records"][0]["version"] == 1
    assert mock.batch_payloads[-1]["high_watermark"] == 2


def test_begin_table_rejects_empty_pk_before_create(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    no_pk = TableInfo(
        name="ITEM",
        columns=[("ITEM_CODE", "text"), ("ITEM_NAME", "text")],
        pk=[],
    )
    with pytest.raises(ObjectMappingError, match="稳定主键"):
        sink.begin_table("e10", no_pk, mode="full_refresh")
    assert mock.generations == {}


def test_persisted_generation_resumes_sequence_and_complete_digest(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    generation_id = sink._active["ITEM"].generation_id
    external_id = sink._active["ITEM"].external_generation_id
    sink._store.close()
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    assert restarted._active["ITEM"].generation_id == generation_id
    assert restarted._active["ITEM"].external_generation_id == external_id
    assert restarted._active["ITEM"].next_sequence_no == 2
    assert len(mock.generations) == 1
    restarted.complete_table("e10", ITEM, "done", 1, 1)
    assert mock.complete_payloads[0]["expected_batch_count"] == 1
    assert next(iter(mock.generations.values()))["status"] == "COMPLETED"


def test_default_lease_refreshes_before_half_expiry(tmp_path: Path):
    sink = AiHubObjectPushSink(
        "http://ai-hub.test",
        source_application_id="e10-adapter",
        bindings={"ITEM": BINDING},
        token="t",
        state_path=tmp_path / "lease.sqlite",
        spool_directory=tmp_path / "spool",
    )
    assert sink.lease_seconds == 300
    assert sink.heartbeat_interval_seconds == 150


def test_durable_receipt_missing_fields_fail_closed(tmp_path: Path):
    mock = AiHubPushMock()
    mock.omit_receipt_fields = {"content_sha256"}
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    with pytest.raises(AiHubPushRejected, match="content_sha256"):
        sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    assert sink._active["ITEM"].next_sequence_no == 1
    assert sink._store.load_receipts("e10-adapter", "erp.item") == []


def test_empty_receipt_fail_closed(tmp_path: Path):
    mock = AiHubPushMock()
    mock.empty_receipt = True
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    with pytest.raises(AiHubPushRejected, match="批次回执为空"):
        sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    assert sink._active["ITEM"].next_sequence_no == 1


def test_generation_bytes_count_object_id_and_payload(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    expected = records_content_bytes(mock.batch_payloads[0]["records"])
    envelope = len(
        json.dumps(mock.batch_payloads[0], ensure_ascii=False).encode("utf-8")
    )
    assert sink._active["ITEM"].total_bytes == expected
    assert expected < envelope


def test_pending_batch_reconciled_from_remote_accepted(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation = sink._active["ITEM"]
    records = [map_row(ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a"}, BINDING, version=1)]
    payload = sink._batch_body(generation, BINDING, "b1", 1, records)
    mock.post(
        f"http://ai-hub.test/platform-api/v1/ingest/push/generations/{generation.generation_id}/batches",
        payload,
        "test-token",
        1,
    )
    assert sink._store.load_receipts("e10-adapter", "erp.item") == []
    sink._store.close()
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    assert restarted._active["ITEM"].next_sequence_no == 2
    assert len(restarted._store.load_receipts("e10-adapter", "erp.item")) == 1
    assert restarted._store.list_pending_batches("e10-adapter", "erp.item") == []


def test_full_refresh_restart_discards_stale_generation(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="full_refresh")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1", mode="full_refresh")
    old_external = sink._active["ITEM"].external_generation_id
    sink._store.close()
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="full_refresh")
    assert restarted._active["ITEM"].external_generation_id != old_external
    assert mock.aborts == 1


def test_expired_generation_resume_starts_fresh(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    generation_id = sink._active["ITEM"].generation_id
    old_external = sink._active["ITEM"].external_generation_id
    mock.generations[generation_id]["status"] = "EXPIRED"
    sink._store.close()
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    assert restarted._active["ITEM"].external_generation_id != old_external
    assert mock.generations[generation_id]["status"] == "EXPIRED"
    assert mock.aborts == 0


def test_completing_failure_allows_new_generation(tmp_path: Path):
    mock = AiHubPushMock()
    mock.complete_status = "FAILED"
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    with pytest.raises(AiHubPushRejected, match="generation failed"):
        sink.complete_table("e10", ITEM, "done", 1, 1)
    persisted = sink._store.load_generation("e10-adapter", "erp.item")
    assert persisted is not None
    assert persisted["status"] == "FAILED"
    mock.complete_status = "COMPLETED"
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    restarted.write("e10", ITEM, [{"ITEM_CODE": "I-2", "ITEM_NAME": "b"}], "b2")
    assert len(restarted._active) == 1


def test_batch_success_does_not_extend_lease_clock(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock, lease_seconds=300)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation_id = sink._active["ITEM"].generation_id
    last_refresh = sink._lease_refreshed_at[generation_id]
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-2", "ITEM_NAME": "b"}], "b2")
    assert sink._lease_refreshed_at[generation_id] == last_refresh
    assert mock.heartbeats == 0


def test_resume_writable_sends_immediate_heartbeat(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    sink._store.close()
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    assert mock.heartbeats == 1


def test_versions_for_batch_allocates_in_one_pass(tmp_path: Path):
    store = ObjectVersionStore(tmp_path / "versions.sqlite")
    items = [(f"id-{index}", f"ck-{index}") for index in range(100)]
    versions = store.versions_for_batch("e10-adapter", "erp.item", items)
    assert list(versions.values()) == list(range(1, 101))
    assert store.type_high_watermark("e10-adapter", "erp.item") == 100
    repeat = store.versions_for_batch("e10-adapter", "erp.item", items)
    assert list(repeat.values()) == list(range(1, 101))
    assert store.type_high_watermark("e10-adapter", "erp.item") == 100


def test_pending_metadata_excludes_business_payload(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "secret"}], "b1")
    rows = sink._store._conn.execute(
        "SELECT sequence_no, external_batch_id, content_sha256, record_count, spool_path "
        "FROM d2a_aihub_pending_batch"
    ).fetchall()
    assert rows == []
    blob = sink._store._conn.execute(
        "SELECT create_request_json, complete_request_json FROM d2a_aihub_generation"
    ).fetchone()
    assert blob is not None
    assert "secret" not in (blob[0] or "")
    assert blob[1] is None


def test_full_refresh_replays_create_before_abort_without_generation_id(
    tmp_path: Path,
):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    external = f"e10:erp.item:{uuid4().hex}"
    body = {
        "source_application_id": "e10-adapter",
        "object_type": "erp.item",
        "external_generation_id": external,
        "sync_mode": "full",
        "protocol_version": AI_HUB_PUSH_PROTOCOL_VERSION,
        "lease_seconds": 300,
        "purpose": "production",
    }
    sink._store.upsert_generation(
        source_application_id="e10-adapter",
        object_type="erp.item",
        table_name="ITEM",
        generation_id="",
        external_generation_id=external,
        sync_mode="full",
        status="OPEN",
        next_sequence_no=1,
        high_watermark=0,
        total_rows=0,
        total_bytes=0,
        started_at=time.time(),
        create_request=body,
    )
    mock.post("http://ai-hub.test/platform-api/v1/ingest/push/generations", body, "t", 1)
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="full_refresh")
    assert mock.aborts == 1
    assert restarted._active["ITEM"].external_generation_id != external


def test_abort_failure_preserves_recovery_state(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="full_refresh")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1", mode="full_refresh")

    def abort_fail(url, payload, token, timeout):
        if url.endswith("/abort"):
            raise RuntimeError("network down")
        return mock.post(url, payload, token, timeout)

    sink._store.close()
    restarted = _sink(tmp_path, mock)
    restarted._post = abort_fail
    with pytest.raises(RuntimeError, match="network down"):
        restarted.begin_table("e10", ITEM, mode="full_refresh")
    assert restarted._store.load_generation("e10-adapter", "erp.item") is not None


def test_complete_timeout_keeps_completing(tmp_path: Path):
    mock = AiHubPushMock()
    mock.complete_status = "COMPLETING"
    mock.poll_complete_status = "COMPLETING"
    sink = _sink(tmp_path, mock, timeout=0.01, complete_poll_interval=0.05)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    with pytest.raises(AiHubPushRejected, match="超时"):
        sink.complete_table("e10", ITEM, "done", 1, 1)
    persisted = sink._store.load_generation("e10-adapter", "erp.item")
    assert persisted is not None
    assert persisted["status"] == "COMPLETING"
    assert persisted["complete_request"] is not None


def test_required_generation_hard_limits(tmp_path: Path):
    mock = AiHubPushMock()

    def caps_missing_bytes(url, token, timeout):
        del token, timeout
        if url.endswith("/capabilities"):
            return {
                "enabled": True,
                "protocol_versions": [AI_HUB_PUSH_PROTOCOL_VERSION],
                "contract_required": True,
                "payload_max_bytes": mock.payload_max_bytes,
                "page_limit_max": mock.page_limit_max,
                "max_batches": mock.max_batches,
                "max_generation_rows": mock.max_generation_rows,
                "max_generation_lifetime_seconds": 3600,
            }
        raise AssertionError(url)

    sink = _sink(tmp_path, mock)
    sink._get_json = caps_missing_bytes
    with pytest.raises(AiHubProtocolError, match="max_generation_bytes"):
        sink.ensure_protocol()


def test_401_refresh_retries_without_consuming_budget(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    state = {"token": "old", "fail_once": True}

    def fake_post(url, payload, token, timeout):
        del payload, timeout
        calls.append(token or "")
        if state["fail_once"]:
            state["fail_once"] = False
            raise urllib.error.HTTPError(
                url,
                401,
                "unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"error_code":"unauthorized"}'),
            )
        return {"generation_id": "g1", "status": "OPEN", "next_sequence_no": 1}

    class Provider:
        def invalidate(self):
            state["token"] = "new"

        def __call__(self):
            return state["token"]

    sink = AiHubObjectPushSink(
        "http://ai-hub.test",
        source_application_id="e10-adapter",
        bindings={"ITEM": BINDING},
        token_provider=Provider(),
        retries=1,
        timeout=1,
        state_path=tmp_path / "auth.sqlite",
        spool_directory=tmp_path / "spool",
    )
    sink._post = fake_post
    result = sink._request("POST", "/generations", {"x": 1})
    assert result is not None
    assert calls == ["old", "new"]


def test_orphan_spool_removed_on_startup(tmp_path: Path):
    shared_root = tmp_path / "push-spool"
    spool_dir = AiHubObjectPushSink._spool_root_for(shared_root, "e10-adapter")
    spool_dir.mkdir(parents=True, exist_ok=True)
    orphan = spool_dir / "orphan.batch"
    orphan.write_text('{"records":[]}', encoding="utf-8")
    mock = AiHubPushMock()
    _sink(tmp_path, mock, spool_directory=shared_root)
    assert not orphan.exists()


def test_orphan_cleanup_does_not_touch_other_source_spool(tmp_path: Path):
    shared_root = tmp_path / "push-spool"
    other_dir = AiHubObjectPushSink._spool_root_for(shared_root, "other-adapter")
    other_dir.mkdir(parents=True, exist_ok=True)
    other_file = other_dir / "inflight.batch"
    other_file.write_text('{"records":[]}', encoding="utf-8")
    mock = AiHubPushMock()
    _sink(tmp_path, mock, spool_directory=shared_root)
    assert other_file.exists()


def test_empty_incremental_complete_keeps_committed_watermark(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    sink.complete_table("e10", ITEM, "done", 1, 1)
    assert mock.committed_high_watermark["erp.item"] == 1
    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    restarted.complete_table("e10", ITEM, "done", 0, 0)
    assert mock.complete_payloads[-1]["high_watermark"] == 1


def test_complete_rejection_on_writable_generation_aborts_remote(tmp_path: Path):
    mock = AiHubPushMock()
    mock.complete_http_reject = True
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    sink.write("e10", ITEM, [{"ITEM_CODE": "I-1", "ITEM_NAME": "a"}], "b1")
    generation_id = sink._active["ITEM"].generation_id
    with pytest.raises(AiHubPushRejected, match="generation_complete_mismatch"):
        sink.complete_table("e10", ITEM, "done", 1, 1)
    persisted = sink._store.load_generation("e10-adapter", "erp.item")
    assert persisted is not None
    assert persisted["status"] == "ABORTED"
    assert mock.generations[generation_id]["status"] == "ABORTED"
    assert mock.aborts == 1


def test_relative_pending_path_survives_restart_cleanup(tmp_path: Path):
    mock = AiHubPushMock()
    shared_root = tmp_path / "push-spool"
    sink = _sink(tmp_path, mock, spool_directory=shared_root)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation = sink._active["ITEM"]
    records = [map_row(ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a"}, BINDING, version=1)]
    payload = sink._batch_body(generation, BINDING, "b1", 1, records)
    spool_path = sink._batch_spool.write(
        sink.source_application_id, generation.object_type, 1, payload
    )
    relative = spool_path.name
    sink._store.persist_pending_batch(
        sink.source_application_id,
        generation.object_type,
        sequence_no=1,
        external_batch_id="b1",
        content_sha256=str(payload["content_sha256"]),
        record_count=1,
        spool_path=str(relative),
    )
    sink._store.close()
    restarted = _sink(tmp_path, mock, spool_directory=shared_root)
    pending = restarted._store.load_pending_batch("e10-adapter", "erp.item", 1)
    assert pending is not None
    restarted._batch_spool.read(pending["spool_path"])


def test_spool_delete_only_removes_files_under_root(tmp_path: Path, monkeypatch):
    mock = AiHubPushMock()
    shared_root = tmp_path / "push-spool"
    sink = _sink(tmp_path, mock, spool_directory=shared_root)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation = sink._active["ITEM"]
    records = [map_row(ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "a"}, BINDING, version=1)]
    payload = sink._batch_body(generation, BINDING, "b1", 1, records)
    spool_path = sink._batch_spool.write(
        sink.source_application_id, generation.object_type, 1, payload
    )
    decoy = tmp_path / spool_path.name
    decoy.write_text("decoy", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    sink._batch_spool.delete(spool_path.name)
    assert decoy.exists()
    assert not spool_path.exists()


def test_spool_write_removes_temp_on_failure(tmp_path: Path, monkeypatch):
    spool = PendingBatchSpool(tmp_path / "push-spool")
    payload = {"records": [{"object_id": "x", "version": 1, "payload": {}}]}

    def fail_replace(src: str, dst: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        spool.write("e10-adapter", "erp.item", 1, payload)
    assert list(spool.root.glob("*.tmp")) == []


def test_orphan_tmp_removed_on_startup(tmp_path: Path):
    shared_root = tmp_path / "push-spool"
    spool_dir = AiHubObjectPushSink._spool_root_for(shared_root, "e10-adapter")
    spool_dir.mkdir(parents=True, exist_ok=True)
    orphan = spool_dir / "orphan.tmp"
    orphan.write_text('{"records":[]}', encoding="utf-8")
    mock = AiHubPushMock()
    _sink(tmp_path, mock, spool_directory=shared_root)
    assert not orphan.exists()


def test_fsync_directory_skips_when_o_directory_unavailable(monkeypatch):
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    PendingBatchSpool._fsync_directory(Path("."))


def test_spool_source_keys_are_collision_free():
    assert AiHubObjectPushSink._spool_source_key("a/b") != (
        AiHubObjectPushSink._spool_source_key("a_b")
    )
    assert ".." not in AiHubObjectPushSink._spool_source_key("../escape")
    assert "\\" not in AiHubObjectPushSink._spool_source_key("C:\\windows")


def test_spool_root_stays_under_configured_directory(tmp_path: Path):
    base = tmp_path / "push-spool"
    root = AiHubObjectPushSink._spool_root_for(base, "a/b")
    root.relative_to(base.resolve())
    other = AiHubObjectPushSink._spool_root_for(base, "a_b")
    assert root != other


def test_spool_write_closes_fd_before_replace(tmp_path: Path, monkeypatch):
    spool = PendingBatchSpool(tmp_path / "push-spool")
    payload = {"records": [{"object_id": "x", "version": 1, "payload": {}}]}
    open_fds: list[int] = []
    real_replace = os.replace

    def tracking_replace(src: str, dst: str) -> None:
        for fd in open_fds:
            try:
                os.fstat(fd)
            except OSError:
                continue
            raise AssertionError("temp fd still open during os.replace")
        real_replace(src, dst)

    real_mkstemp = tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        open_fds.append(fd)
        return fd, path

    monkeypatch.setattr(os, "replace", tracking_replace)
    monkeypatch.setattr(tempfile, "mkstemp", tracking_mkstemp)
    path = spool.write("e10-adapter", "erp.item", 1, payload)
    assert path.exists()


def test_confirmed_source_batch_skipped_after_crash_before_cursor(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    row = {"ITEM_CODE": "I-1", "ITEM_NAME": "a"}
    assert sink.write("e10", ITEM, [row], "run1-0") == 1
    assert len(mock.batch_payloads) == 1
    generation = sink._active["ITEM"]
    assert generation.next_sequence_no == 2
    assert generation.total_rows == 1
    sink._store.close()

    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    # Same source rows, new caller batch_id (as after ERP cursor not yet advanced).
    assert restarted.write("e10", ITEM, [row], "run2-0") == 1
    assert len(mock.batch_payloads) == 1
    resumed = restarted._active["ITEM"]
    assert resumed.next_sequence_no == 2
    assert resumed.total_rows == 1
    restarted.complete_table("e10", ITEM, "done", 1, 1)
    assert mock.complete_payloads[0]["expected_batch_count"] == 1


def test_same_external_batch_id_is_not_resent(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    row = {"ITEM_CODE": "I-1", "ITEM_NAME": "a"}
    assert sink.write("e10", ITEM, [row], "b1") == 1
    assert sink.write("e10", ITEM, [row], "b1") == 1
    assert len(mock.batch_payloads) == 1
    assert sink._active["ITEM"].next_sequence_no == 2
    assert sink._active["ITEM"].total_rows == 1


def test_batch_file_names_are_portable_digests(tmp_path: Path):
    spool = PendingBatchSpool(tmp_path / "push-spool")
    path = spool.write(
        r"C:\evil/../src",
        "erp.item/../x",
        1,
        {"records": []},
    )
    assert path.parent == spool.root.resolve()
    assert path.suffix == ".batch"
    assert all(ch in "0123456789abcdef" for ch in path.stem)


def test_spool_read_rejects_paths_outside_root(tmp_path: Path):
    spool = PendingBatchSpool(tmp_path / "push-spool")
    outside = tmp_path / "outside.batch"
    outside.write_text('{"records":[]}', encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="not found under root"):
        spool.read(outside)
    with pytest.raises(FileNotFoundError, match="not found under root"):
        spool.read("../outside.batch")


def test_pending_drain_then_sends_changed_chunk(tmp_path: Path):
    mock = AiHubPushMock()
    sink = _sink(tmp_path, mock)
    sink.begin_table("e10", ITEM, mode="incremental")
    generation = sink._active["ITEM"]
    old_records = [
        map_row(ITEM, {"ITEM_CODE": "I-1", "ITEM_NAME": "old"}, BINDING, version=1)
    ]
    old_payload = sink._batch_body(generation, BINDING, "pending-old", 1, old_records)
    spool_path = sink._batch_spool.write(
        sink.source_application_id, generation.object_type, 1, old_payload
    )
    sink._store.persist_pending_batch(
        sink.source_application_id,
        generation.object_type,
        sequence_no=1,
        external_batch_id="pending-old",
        content_sha256=str(old_payload["content_sha256"]),
        record_count=1,
        spool_path=str(spool_path),
    )
    sink._store.close()

    restarted = _sink(tmp_path, mock)
    restarted.begin_table("e10", ITEM, mode="incremental")
    assert (
        restarted.write(
            "e10",
            ITEM,
            [{"ITEM_CODE": "I-1", "ITEM_NAME": "new"}],
            "run-new",
        )
        == 1
    )
    assert len(mock.batch_payloads) == 2
    assert mock.batch_payloads[0]["external_batch_id"] == "pending-old"
    assert mock.batch_payloads[0]["records"][0]["payload"]["ITEM_NAME"] == "old"
    assert mock.batch_payloads[1]["external_batch_id"] == "run-new"
    assert mock.batch_payloads[1]["records"][0]["payload"]["ITEM_NAME"] == "new"
    assert mock.batch_payloads[1]["sequence_no"] == 2
    assert restarted._active["ITEM"].next_sequence_no == 3
    assert restarted._active["ITEM"].total_rows == 2
    assert restarted._store.list_pending_batches("e10-adapter", "erp.item") == []
