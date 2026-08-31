"""AI Hub object-level Push sink (C1-B).

Speaks PUSH_AGENT v1 against `/platform-api/v1/ingest/push/*`.
Must not be used by swapping `HttpPushSink.url` onto AI Hub.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...protocol.ai_hub_push import (
    AI_HUB_PUSH_API_PREFIX,
    AI_HUB_PUSH_PROTOCOL_VERSION,
    batch_content_digest,
    ordered_batch_digest,
    payload_content_hash,
    payload_size_bytes,
    records_content_bytes,
)
from ...shared.config import validate_https_endpoint
from ...shared.store.table import TableInfo
from .ai_hub_object_map import ObjectBinding, ObjectMappingError, map_row, object_id_from_row
from .sink import SyncMode

GetJson = Callable[[str, str | None, float], dict]
PostFn = Callable[[str, dict | None, str | None, float], dict | None]
TokenProvider = Callable[[], str]

_REQUIRED_CAPS = (
    "page_limit_max",
    "payload_max_bytes",
    "max_generation_rows",
    "max_batches",
    "max_generation_bytes",
    "max_generation_lifetime_seconds",
)
_COMPLETE_SUCCESS = "COMPLETED"
_COMPLETE_FAILURE = frozenset({"FAILED", "ABORTED", "EXPIRED"})
_COMPLETE_PENDING = frozenset({"COMPLETING"})
_COMPLETE_UNCERTAIN = frozenset(
    {"generation_complete_timeout", "generation_complete_unknown"}
)


_OIDC_EXPIRY_SKEW_SECONDS = 60
_DEFAULT_LEASE_SECONDS = 300
_AIHUB_OBJECT_VERSION_TABLE = "d2a_aihub_object_version"
_AIHUB_TYPE_WATERMARK_TABLE = "d2a_aihub_type_watermark"
_AIHUB_GENERATION_TABLE = "d2a_aihub_generation"
_AIHUB_BATCH_RECEIPT_TABLE = "d2a_aihub_batch_receipt"
_AIHUB_PENDING_BATCH_TABLE = "d2a_aihub_pending_batch"
_WRITABLE_STATUSES = frozenset({"OPEN", "RECEIVING"})
_TERMINAL_STATUSES = frozenset({"FAILED", "ABORTED", "EXPIRED", "COMPLETED"})
_DURABLE_RECEIPT_FIELDS = (
    "sequence_no",
    "external_batch_id",
    "content_sha256",
    "record_count",
    "high_watermark",
)


def _ssl_context(ca_bundle: str | None) -> ssl.SSLContext | None:
    if not ca_bundle:
        return None
    return ssl.create_default_context(cafile=ca_bundle)


class AiHubProtocolError(RuntimeError):
    """Platform capabilities are missing, disabled, or incompatible."""


class AiHubPushRejected(RuntimeError):
    """Platform returned a non-retryable Push error."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code
        self.details = details or {}


class CachedOidcTokenProvider:
    """Fetch client-credentials tokens once and reuse until near expiry."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        audience: str,
        scope: str,
        timeout: float = 30.0,
        allow_insecure_http: bool = False,
        ca_bundle: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        validate_https_endpoint(
            token_url,
            label="oidc_token_url",
            allow_insecure_http=allow_insecure_http,
        )
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._audience = audience
        self._scope = scope
        self._timeout = timeout
        self._ssl_context = ssl_context or _ssl_context(ca_bundle)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def __call__(self) -> str:
        now = self._clock()
        if self._token and now < self._expires_at:
            return self._token
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "audience": self._audience,
                "scope": self._scope,
            }
        ).encode()
        req = urllib.request.Request(
            self._token_url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(
            req, timeout=self._timeout, context=self._ssl_context
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise AiHubProtocolError("OIDC token 响应缺少 access_token")
        try:
            ttl = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        self._token = token
        self._expires_at = now + max(0, ttl - _OIDC_EXPIRY_SKEW_SECONDS)
        return token


def oidc_client_credentials_provider(
    token_url: str,
    client_id: str,
    client_secret: str,
    *,
    audience: str,
    scope: str,
    timeout: float = 30.0,
    allow_insecure_http: bool = False,
    ca_bundle: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> CachedOidcTokenProvider:
    return CachedOidcTokenProvider(
        token_url,
        client_id,
        client_secret,
        audience=audience,
        scope=scope,
        timeout=timeout,
        allow_insecure_http=allow_insecure_http,
        ca_bundle=ca_bundle,
        ssl_context=ssl_context,
    )


def _urllib_request(
    url: str,
    *,
    method: str,
    payload: dict | None,
    token: str | None,
    timeout: float,
    context: ssl.SSLContext | None = None,
) -> dict | None:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        body = resp.read()
        return json.loads(body.decode("utf-8")) if body else None


class PendingBatchSpool:
    """Write in-flight batch bodies outside the control state DB."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _batch_file_name(
        source_application_id: str,
        object_type: str,
        sequence_no: int,
    ) -> str:
        digest = hashlib.sha256(
            f"{source_application_id}\0{object_type}\0{sequence_no}".encode("utf-8")
        ).hexdigest()
        return f"{digest}.batch"

    def _contained_path(self, path: Path) -> Path:
        root_resolved = self.root.resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError(
                f"spool path escapes configured root {root_resolved}: {path}"
            ) from exc
        return resolved

    def write(
        self,
        source_application_id: str,
        object_type: str,
        sequence_no: int,
        payload: Mapping[str, Any],
    ) -> Path:
        name = self._batch_file_name(
            source_application_id, object_type, sequence_no
        )
        path = self._contained_path(self.root / name)
        data = json.dumps(
            dict(payload), ensure_ascii=True, sort_keys=True
        ).encode()
        fd, tmp_path = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{name}.",
            suffix=".tmp",
        )
        committed = False
        closed = False
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise OSError("spool temp file short write")
                offset += written
            os.fsync(fd)
            os.close(fd)
            closed = True
            # Windows cannot replace a file that still has an open mkstemp handle.
            os.replace(tmp_path, path)
            committed = True
            try:
                path.chmod(0o600)
            except OSError:
                pass
            self._fsync_directory(self.root)
            return self._contained_path(path)
        finally:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if not committed:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            dir_fd = os.open(path, os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    @staticmethod
    def _resolve_spool_path(path: str | Path) -> str:
        return str(Path(path).resolve())

    def _candidate_paths(self, path: str | Path) -> list[Path]:
        candidate = Path(path)
        if candidate.is_absolute():
            paths = [candidate]
        else:
            paths = [self.root / candidate, self.root / candidate.name]
        unique: list[Path] = []
        seen: set[str] = set()
        for item in paths:
            key = str(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def locate(self, path: str | Path) -> Path:
        root_resolved = self.root.resolve()
        for candidate in self._candidate_paths(path):
            try:
                located = candidate.resolve()
                located.relative_to(root_resolved)
            except ValueError:
                continue
            if located.is_file():
                return located
        raise FileNotFoundError(
            f"spool batch not found under root {root_resolved}: {path}"
        )

    def read(self, path: str | Path) -> dict[str, Any]:
        return json.loads(self.locate(path).read_text(encoding="utf-8"))

    def delete(self, path: str | Path | None) -> None:
        if not path:
            return
        root_resolved = self.root.resolve()
        for candidate in self._candidate_paths(path):
            try:
                located = candidate.resolve()
                located.relative_to(root_resolved)
            except ValueError:
                continue
            if not located.is_file():
                continue
            located.unlink(missing_ok=True)
            return

    def cleanup_orphans(self, referenced: set[str]) -> int:
        referenced_resolved: set[str] = set()
        for item in referenced:
            if not item:
                continue
            for candidate in self._candidate_paths(item):
                referenced_resolved.add(self._resolve_spool_path(candidate))
        removed = 0
        if not self.root.exists():
            return removed
        root_resolved = self.root.resolve()
        for path in self.root.glob("*.batch"):
            resolved = Path(self._resolve_spool_path(path))
            try:
                resolved.relative_to(root_resolved)
            except ValueError:
                continue
            if self._resolve_spool_path(path) in referenced_resolved:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        for path in self.root.glob("*.tmp"):
            try:
                path.resolve().relative_to(root_resolved)
            except ValueError:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed


class ObjectVersionStore:
    """Persist object versions, type watermarks, generations, and receipts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {_AIHUB_OBJECT_VERSION_TABLE} (
                source_application_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                change_key TEXT NOT NULL,
                PRIMARY KEY (source_application_id, object_type, object_id)
            );
            CREATE TABLE IF NOT EXISTS {_AIHUB_TYPE_WATERMARK_TABLE} (
                source_application_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                next_version INTEGER NOT NULL,
                high_watermark INTEGER NOT NULL,
                PRIMARY KEY (source_application_id, object_type)
            );
            CREATE TABLE IF NOT EXISTS {_AIHUB_GENERATION_TABLE} (
                source_application_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                table_name TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                external_generation_id TEXT NOT NULL,
                sync_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                next_sequence_no INTEGER NOT NULL,
                high_watermark INTEGER NOT NULL,
                total_rows INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                started_at REAL NOT NULL DEFAULT 0,
                create_request_json TEXT NOT NULL,
                complete_request_json TEXT,
                PRIMARY KEY (source_application_id, object_type)
            );
            CREATE TABLE IF NOT EXISTS {_AIHUB_BATCH_RECEIPT_TABLE} (
                source_application_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                external_batch_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                high_watermark INTEGER NOT NULL,
                PRIMARY KEY (source_application_id, object_type, sequence_no)
            );
            CREATE TABLE IF NOT EXISTS {_AIHUB_PENDING_BATCH_TABLE} (
                source_application_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                external_batch_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                spool_path TEXT NOT NULL,
                PRIMARY KEY (source_application_id, object_type, sequence_no)
            );
            """
        )
        self._ensure_generation_started_at_column()
        self._ensure_pending_batch_schema()
        self._conn.commit()

    def _ensure_generation_started_at_column(self) -> None:
        rows = self._conn.execute(
            f"PRAGMA table_info({_AIHUB_GENERATION_TABLE})"
        ).fetchall()
        names = {row[1] for row in rows}
        if "started_at" not in names:
            self._conn.execute(
                f"""
                ALTER TABLE {_AIHUB_GENERATION_TABLE}
                ADD COLUMN started_at REAL NOT NULL DEFAULT 0
                """
            )

    def _ensure_pending_batch_schema(self) -> None:
        rows = self._conn.execute(
            f"PRAGMA table_info({_AIHUB_PENDING_BATCH_TABLE})"
        ).fetchall()
        if not rows:
            return
        names = {row[1] for row in rows}
        if "spool_path" in names and "payload_json" not in names:
            return
        self._conn.execute(f"DROP TABLE IF EXISTS {_AIHUB_PENDING_BATCH_TABLE}")
        self._conn.execute(
            f"""
            CREATE TABLE {_AIHUB_PENDING_BATCH_TABLE} (
                source_application_id TEXT NOT NULL,
                object_type TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                external_batch_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                spool_path TEXT NOT NULL,
                PRIMARY KEY (source_application_id, object_type, sequence_no)
            )
            """
        )

    def versions_for_batch(
        self,
        source_application_id: str,
        object_type: str,
        items: Sequence[tuple[str, str]],
    ) -> dict[str, int]:
        """Allocate object versions for a source batch in one transaction."""
        if not items:
            return {}
        try:
            object_ids = [item[0] for item in items]
            placeholders = ",".join("?" for _ in object_ids)
            existing_rows = self._conn.execute(
                f"""
                SELECT object_id, version, change_key FROM {_AIHUB_OBJECT_VERSION_TABLE}
                WHERE source_application_id=? AND object_type=?
                  AND object_id IN ({placeholders})
                """,
                (source_application_id, object_type, *object_ids),
            ).fetchall()
            existing = {
                row[0]: (int(row[1]), row[2]) for row in existing_rows
            }
            cursor = self._conn.execute(
                f"""
                SELECT next_version FROM {_AIHUB_TYPE_WATERMARK_TABLE}
                WHERE source_application_id=? AND object_type=?
                """,
                (source_application_id, object_type),
            ).fetchone()
            next_version = (int(cursor[0]) if cursor is not None else 0) + 1
            assigned: dict[str, int] = {}
            upserts: list[tuple[str, int, str]] = []
            for object_id, change_key in items:
                cached = existing.get(object_id)
                if cached is not None and cached[1] == change_key:
                    assigned[object_id] = cached[0]
                    continue
                version = next_version
                next_version += 1
                assigned[object_id] = version
                upserts.append((object_id, version, change_key))
            if upserts:
                high_watermark = next_version - 1
                self._conn.execute(
                    f"""
                    INSERT INTO {_AIHUB_TYPE_WATERMARK_TABLE}(
                        source_application_id, object_type, next_version, high_watermark
                    )
                    VALUES (?,?,?,?)
                    ON CONFLICT(source_application_id, object_type)
                    DO UPDATE SET
                        next_version=excluded.next_version,
                        high_watermark=excluded.high_watermark
                    """,
                    (
                        source_application_id,
                        object_type,
                        high_watermark,
                        high_watermark,
                    ),
                )
                self._conn.executemany(
                    f"""
                    INSERT INTO {_AIHUB_OBJECT_VERSION_TABLE}(
                        source_application_id, object_type, object_id, version, change_key
                    )
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(source_application_id, object_type, object_id)
                    DO UPDATE SET version=excluded.version, change_key=excluded.change_key
                    """,
                    [
                        (
                            source_application_id,
                            object_type,
                            object_id,
                            version,
                            change_key,
                        )
                        for object_id, version, change_key in upserts
                    ],
                )
            self._conn.commit()
            return assigned
        except Exception:
            self._conn.rollback()
            raise

    def type_high_watermark(
        self, source_application_id: str, object_type: str
    ) -> int:
        row = self._conn.execute(
            f"""
            SELECT high_watermark FROM {_AIHUB_TYPE_WATERMARK_TABLE}
            WHERE source_application_id=? AND object_type=?
            """,
            (source_application_id, object_type),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def load_generation(
        self, source_application_id: str, object_type: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"""
            SELECT table_name, generation_id, external_generation_id, sync_mode,
                   status, next_sequence_no, high_watermark, total_rows, total_bytes,
                   started_at, create_request_json, complete_request_json
            FROM {_AIHUB_GENERATION_TABLE}
            WHERE source_application_id=? AND object_type=?
            """,
            (source_application_id, object_type),
        ).fetchone()
        if row is None:
            return None
        complete_raw = row[11]
        return {
            "table_name": row[0],
            "generation_id": row[1],
            "external_generation_id": row[2],
            "object_type": object_type,
            "sync_mode": row[3],
            "status": row[4],
            "next_sequence_no": int(row[5]),
            "high_watermark": int(row[6]),
            "total_rows": int(row[7]),
            "total_bytes": int(row[8]),
            "started_at": float(row[9] or 0.0),
            "create_request": json.loads(row[10]),
            "complete_request": (
                json.loads(complete_raw) if complete_raw else None
            ),
        }

    def upsert_generation(
        self,
        *,
        source_application_id: str,
        object_type: str,
        table_name: str,
        generation_id: str,
        external_generation_id: str,
        sync_mode: str,
        status: str,
        next_sequence_no: int,
        high_watermark: int,
        total_rows: int,
        total_bytes: int,
        started_at: float,
        create_request: Mapping[str, Any],
        complete_request: Mapping[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            f"""
            INSERT INTO {_AIHUB_GENERATION_TABLE}(
                source_application_id, object_type, table_name, generation_id,
                external_generation_id, sync_mode, status, next_sequence_no,
                high_watermark, total_rows, total_bytes, started_at,
                create_request_json, complete_request_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_application_id, object_type) DO UPDATE SET
                table_name=excluded.table_name,
                generation_id=excluded.generation_id,
                external_generation_id=excluded.external_generation_id,
                sync_mode=excluded.sync_mode,
                status=excluded.status,
                next_sequence_no=excluded.next_sequence_no,
                high_watermark=excluded.high_watermark,
                total_rows=excluded.total_rows,
                total_bytes=excluded.total_bytes,
                started_at=excluded.started_at,
                create_request_json=excluded.create_request_json,
                complete_request_json=excluded.complete_request_json
            """,
            (
                source_application_id,
                object_type,
                table_name,
                generation_id,
                external_generation_id,
                sync_mode,
                status,
                next_sequence_no,
                high_watermark,
                total_rows,
                total_bytes,
                started_at,
                json.dumps(dict(create_request), ensure_ascii=True, sort_keys=True),
                (
                    json.dumps(dict(complete_request), ensure_ascii=True, sort_keys=True)
                    if complete_request is not None
                    else None
                ),
            ),
        )
        self._conn.commit()

    def clear_receipts(
        self, source_application_id: str, object_type: str
    ) -> None:
        self._conn.execute(
            f"""
            DELETE FROM {_AIHUB_BATCH_RECEIPT_TABLE}
            WHERE source_application_id=? AND object_type=?
            """,
            (source_application_id, object_type),
        )
        self._conn.commit()

    def clear_pending_batches(
        self, source_application_id: str, object_type: str
    ) -> None:
        self._conn.execute(
            f"""
            DELETE FROM {_AIHUB_PENDING_BATCH_TABLE}
            WHERE source_application_id=? AND object_type=?
            """,
            (source_application_id, object_type),
        )
        self._conn.commit()

    def delete_generation(
        self, source_application_id: str, object_type: str
    ) -> None:
        self._conn.execute(
            f"""
            DELETE FROM {_AIHUB_GENERATION_TABLE}
            WHERE source_application_id=? AND object_type=?
            """,
            (source_application_id, object_type),
        )
        self._conn.commit()

    def list_pending_batches(
        self, source_application_id: str, object_type: str
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"""
            SELECT sequence_no, external_batch_id, content_sha256,
                   record_count, spool_path
            FROM {_AIHUB_PENDING_BATCH_TABLE}
            WHERE source_application_id=? AND object_type=?
            ORDER BY sequence_no
            """,
            (source_application_id, object_type),
        ).fetchall()
        return [
            {
                "sequence_no": int(row[0]),
                "external_batch_id": row[1],
                "content_sha256": row[2],
                "record_count": int(row[3]),
                "spool_path": row[4],
            }
            for row in rows
        ]

    def list_pending_spool_paths(
        self, source_application_id: str
    ) -> set[str]:
        rows = self._conn.execute(
            f"""
            SELECT spool_path FROM {_AIHUB_PENDING_BATCH_TABLE}
            WHERE source_application_id=?
            """,
            (source_application_id,),
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def list_all_pending_spool_paths(self) -> set[str]:
        rows = self._conn.execute(
            f"SELECT spool_path FROM {_AIHUB_PENDING_BATCH_TABLE}"
        ).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def persist_pending_batch(
        self,
        source_application_id: str,
        object_type: str,
        *,
        sequence_no: int,
        external_batch_id: str,
        content_sha256: str,
        record_count: int,
        spool_path: str,
    ) -> None:
        self._conn.execute(
            f"""
            INSERT INTO {_AIHUB_PENDING_BATCH_TABLE}(
                source_application_id, object_type, sequence_no,
                external_batch_id, content_sha256, record_count, spool_path
            )
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(source_application_id, object_type, sequence_no)
            DO UPDATE SET
                external_batch_id=excluded.external_batch_id,
                content_sha256=excluded.content_sha256,
                record_count=excluded.record_count,
                spool_path=excluded.spool_path
            """,
            (
                source_application_id,
                object_type,
                sequence_no,
                external_batch_id,
                content_sha256,
                record_count,
                spool_path,
            ),
        )
        self._conn.commit()

    def load_pending_batch(
        self,
        source_application_id: str,
        object_type: str,
        sequence_no: int,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"""
            SELECT external_batch_id, content_sha256, record_count, spool_path
            FROM {_AIHUB_PENDING_BATCH_TABLE}
            WHERE source_application_id=? AND object_type=? AND sequence_no=?
            """,
            (source_application_id, object_type, sequence_no),
        ).fetchone()
        if row is None:
            return None
        return {
            "sequence_no": sequence_no,
            "external_batch_id": row[0],
            "content_sha256": row[1],
            "record_count": int(row[2]),
            "spool_path": row[3],
        }

    def commit_batch(
        self,
        *,
        source_application_id: str,
        object_type: str,
        receipt: Mapping[str, Any],
        sequence_no: int,
        spool_path: str | None,
        generation_id: str,
        external_generation_id: str,
        table_name: str,
        sync_mode: str,
        status: str,
        next_sequence_no: int,
        high_watermark: int,
        total_rows: int,
        total_bytes: int,
        started_at: float,
        create_request: Mapping[str, Any],
        complete_request: Mapping[str, Any] | None,
    ) -> None:
        try:
            self._conn.execute(
                f"""
                INSERT INTO {_AIHUB_BATCH_RECEIPT_TABLE}(
                    source_application_id, object_type, sequence_no,
                    external_batch_id, content_sha256, record_count, high_watermark
                )
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(source_application_id, object_type, sequence_no)
                DO UPDATE SET
                    external_batch_id=excluded.external_batch_id,
                    content_sha256=excluded.content_sha256,
                    record_count=excluded.record_count,
                    high_watermark=excluded.high_watermark
                """,
                (
                    source_application_id,
                    object_type,
                    int(receipt["sequence_no"]),
                    str(receipt["external_batch_id"]),
                    str(receipt["content_sha256"]),
                    int(receipt["record_count"]),
                    int(receipt["high_watermark"]),
                ),
            )
            self._conn.execute(
                f"""
                INSERT INTO {_AIHUB_GENERATION_TABLE}(
                    source_application_id, object_type, table_name, generation_id,
                    external_generation_id, sync_mode, status, next_sequence_no,
                    high_watermark, total_rows, total_bytes, started_at,
                    create_request_json, complete_request_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_application_id, object_type) DO UPDATE SET
                    table_name=excluded.table_name,
                    generation_id=excluded.generation_id,
                    external_generation_id=excluded.external_generation_id,
                    sync_mode=excluded.sync_mode,
                    status=excluded.status,
                    next_sequence_no=excluded.next_sequence_no,
                    high_watermark=excluded.high_watermark,
                    total_rows=excluded.total_rows,
                    total_bytes=excluded.total_bytes,
                    started_at=excluded.started_at,
                    create_request_json=excluded.create_request_json,
                    complete_request_json=excluded.complete_request_json
                """,
                (
                    source_application_id,
                    object_type,
                    table_name,
                    generation_id,
                    external_generation_id,
                    sync_mode,
                    status,
                    next_sequence_no,
                    high_watermark,
                    total_rows,
                    total_bytes,
                    started_at,
                    json.dumps(dict(create_request), ensure_ascii=True, sort_keys=True),
                    (
                        json.dumps(dict(complete_request), ensure_ascii=True, sort_keys=True)
                        if complete_request is not None
                        else None
                    ),
                ),
            )
            self._conn.execute(
                f"""
                DELETE FROM {_AIHUB_PENDING_BATCH_TABLE}
                WHERE source_application_id=? AND object_type=? AND sequence_no=?
                """,
                (source_application_id, object_type, sequence_no),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def append_receipt(
        self,
        source_application_id: str,
        object_type: str,
        receipt: Mapping[str, Any],
    ) -> None:
        self._conn.execute(
            f"""
            INSERT INTO {_AIHUB_BATCH_RECEIPT_TABLE}(
                source_application_id, object_type, sequence_no,
                external_batch_id, content_sha256, record_count, high_watermark
            )
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(source_application_id, object_type, sequence_no)
            DO UPDATE SET
                external_batch_id=excluded.external_batch_id,
                content_sha256=excluded.content_sha256,
                record_count=excluded.record_count,
                high_watermark=excluded.high_watermark
            """,
            (
                source_application_id,
                object_type,
                int(receipt["sequence_no"]),
                str(receipt["external_batch_id"]),
                str(receipt["content_sha256"]),
                int(receipt["record_count"]),
                int(receipt["high_watermark"]),
            ),
        )
        self._conn.commit()

    def load_receipts(
        self, source_application_id: str, object_type: str
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            f"""
            SELECT sequence_no, external_batch_id, content_sha256,
                   record_count, high_watermark
            FROM {_AIHUB_BATCH_RECEIPT_TABLE}
            WHERE source_application_id=? AND object_type=?
            ORDER BY sequence_no
            """,
            (source_application_id, object_type),
        ).fetchall()
        return [
            {
                "sequence_no": int(row[0]),
                "external_batch_id": row[1],
                "content_sha256": row[2],
                "record_count": int(row[3]),
                "high_watermark": int(row[4]),
            }
            for row in rows
        ]

    def close(self) -> None:
        self._conn.close()


@dataclass
class _ActiveGeneration:
    generation_id: str
    external_generation_id: str
    object_type: str
    table: str
    sync_mode: str
    create_request: dict[str, Any]
    next_sequence_no: int = 1
    accepted: list[dict[str, Any]] = field(default_factory=list)
    high_watermark: int = 0
    total_rows: int = 0
    total_bytes: int = 0
    started_at: float = 0.0
    status: str = "OPEN"
    complete_request: dict[str, Any] | None = None


class AiHubObjectPushSink:
    """Push mapped objects to AI Hub. Does not speak table-level ingest v3."""

    def __init__(
        self,
        url: str,
        *,
        source_application_id: str,
        bindings: Mapping[str, ObjectBinding],
        token: str | None = None,
        token_provider: TokenProvider | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        state_path: str | Path | None = None,
        post: PostFn | None = None,
        get_json: GetJson | None = None,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        complete_poll_interval: float = 0.2,
        ca_bundle: str | None = None,
        spool_directory: str | Path | None = None,
    ) -> None:
        if not url:
            raise ValueError("AiHubObjectPushSink 需要 AI Hub 平台根 URL")
        if not source_application_id:
            raise ValueError("AiHubObjectPushSink 需要 source_application_id")
        if not bindings:
            raise ValueError("AiHubObjectPushSink 需要至少一条 object binding")
        if not spool_directory:
            raise ValueError(
                "AiHubObjectPushSink 需要 spool_directory;"
                "请配置 spool.policy=encrypted_temp_volume"
            )
        self.url = url.rstrip("/")
        self.source_application_id = source_application_id
        self.bindings = dict(bindings)
        self.token = token
        self._token_provider = token_provider
        self.timeout = timeout
        self.retries = max(1, retries)
        self.lease_seconds = max(5, int(lease_seconds))
        self._complete_poll_interval = max(0.0, complete_poll_interval)
        self._sleep = time.sleep
        context = _ssl_context(ca_bundle)
        self._ssl_context = context
        self._post = post or (
            lambda endpoint, payload, auth, request_timeout: _urllib_request(
                endpoint,
                method="POST",
                payload=payload,
                token=auth,
                timeout=request_timeout,
                context=context,
            )
        )
        self._get_json = get_json or (
            lambda endpoint, auth, request_timeout: _urllib_request(
                endpoint,
                method="GET",
                payload=None,
                token=auth,
                timeout=request_timeout,
                context=context,
            )
            or {}
        )
        self._capabilities: dict[str, Any] | None = None
        self._active: dict[str, _ActiveGeneration] = {}
        self._lease_refreshed_at: dict[str, float] = {}
        state_file = Path(state_path or "ai-hub-push-state.sqlite")
        self._store = ObjectVersionStore(state_file)
        spool_base = Path(spool_directory).resolve()
        spool_root = self._spool_root_for(spool_base, source_application_id)
        self._batch_spool = PendingBatchSpool(spool_root)
        self._cleanup_orphan_spools()
        self._last_retry_count = 0

    @staticmethod
    def _spool_source_key(source_application_id: str) -> str:
        """Stable, collision-free directory name under the configured spool root."""
        digest = hashlib.sha256(
            source_application_id.encode("utf-8")
        ).hexdigest()
        return f"src-{digest}"

    @classmethod
    def _spool_root_for(
        cls, spool_directory: str | Path, source_application_id: str
    ) -> Path:
        base = Path(spool_directory).resolve()
        root = (base / cls._spool_source_key(source_application_id)).resolve()
        try:
            root.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                "AI Hub spool 目录逃出配置的 encrypted spool 根目录"
            ) from exc
        return root

    def _cleanup_orphan_spools(self) -> None:
        referenced = self._store.list_pending_spool_paths(
            self.source_application_id
        )
        self._batch_spool.cleanup_orphans(referenced)

    @property
    def heartbeat_interval_seconds(self) -> float:
        return max(5.0, self.lease_seconds / 2)

    def _auth(self) -> str | None:
        if self._token_provider is not None:
            return self._token_provider()
        return self.token

    def _api(self, path: str) -> str:
        return f"{self.url}{AI_HUB_PUSH_API_PREFIX}{path}"

    def ensure_protocol(self) -> None:
        if self._capabilities is not None:
            return
        try:
            caps = self._get_json(
                self._api("/capabilities"), self._auth(), self.timeout
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise AiHubProtocolError(
                f"无法读取 AI Hub Push capabilities:{exc}"
            ) from exc
        versions = [str(v) for v in caps.get("protocol_versions") or []]
        if AI_HUB_PUSH_PROTOCOL_VERSION not in versions:
            raise AiHubProtocolError(
                f"AI Hub Push 协议不兼容:中间机发送 {AI_HUB_PUSH_PROTOCOL_VERSION}, "
                f"平台支持 {versions}"
            )
        if caps.get("enabled") is not True:
            raise AiHubProtocolError(
                "AI Hub Push 未启用(DATA_INGEST_PUSH_ENABLED 或 "
                "change-log isolation);不得改用表级 HttpPushSink"
            )
        if caps.get("contract_required") is False:
            raise AiHubProtocolError("AI Hub Push 必须要求 ACTIVE 契约")
        for name in _REQUIRED_CAPS:
            self._parse_cap(caps, name)
        self._capabilities = caps

    def _raise_http(self, error: urllib.error.HTTPError, path: str) -> None:
        body: dict[str, Any] = {}
        try:
            raw = error.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}
        code = str(body.get("error_code") or body.get("code") or "")
        message = str(body.get("message") or body.get("detail") or error)
        details = body.get("details") if isinstance(body.get("details"), dict) else {}
        raise AiHubPushRejected(
            f"AI Hub 拒绝 {path}(HTTP {error.code}"
            + (f", {code}" if code else "")
            + f"):{message[:240]}",
            error_code=code or None,
            status_code=int(error.code),
            details=details,
        ) from error

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict | None:
        last: Exception | None = None
        url = self._api(path)
        attempts_left = self.retries
        auth_refreshed = False
        attempt = 0
        while attempts_left > 0:
            try:
                if method == "GET":
                    result = self._get_json(url, self._auth(), self.timeout)
                else:
                    result = self._post(url, payload, self._auth(), self.timeout)
                self._last_retry_count = attempt
                return result
            except urllib.error.HTTPError as error:
                if error.code == 401 and not auth_refreshed:
                    invalidate = getattr(self._token_provider, "invalidate", None)
                    if callable(invalidate):
                        invalidate()
                        auth_refreshed = True
                        continue
                if error.code not in (408, 425, 429) and error.code < 500:
                    self._raise_http(error, path)
                last = error
            except AiHubPushRejected:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = error
            self._last_retry_count = attempt
            attempt += 1
            attempts_left -= 1
            if attempts_left > 0:
                delay = min(2 ** attempt, 10)
                time.sleep(delay + random.uniform(0, delay * 0.2))
        raise RuntimeError(
            f"AI Hub {path} 失败(重试 {self.retries} 次):{last}"
        )

    def begin_sync(self, source: str, tables: list[str], run_id: int) -> None:
        del run_id
        self.ensure_protocol()
        missing = [table for table in tables if table not in self.bindings]
        if missing:
            raise ObjectMappingError(
                f"未配置 AI Hub object binding 的表:{missing}"
            )

    def heartbeat_sync(self, source: str) -> None:
        del source
        for generation in list(self._active.values()):
            self._maybe_refresh_lease(generation, force=True)

    def begin_table(
        self,
        source: str,
        info: TableInfo,
        *,
        mode: SyncMode,
        snapshot_id: str | None = None,
    ) -> None:
        del snapshot_id
        if not info.pk:
            raise ObjectMappingError(
                f"{info.name}: AI Hub object_id 需要稳定主键,拒绝空 pk"
            )
        self.ensure_protocol()
        binding = self._binding(info.name)
        sync_mode = "full" if mode == "full_refresh" else "incremental"
        persisted = self._store.load_generation(
            self.source_application_id, binding.object_type
        )
        if persisted is not None:
            if persisted["status"] == "COMPLETING" and persisted["complete_request"]:
                try:
                    self._resume_completing(info.name, persisted)
                except AiHubPushRejected as exc:
                    if exc.error_code in _COMPLETE_UNCERTAIN:
                        raise
                persisted = self._store.load_generation(
                    self.source_application_id, binding.object_type
                )
            if persisted is not None and persisted["status"] == "COMPLETING":
                raise AiHubPushRejected(
                    "generation 仍在 completing,等待平台确认",
                    error_code="generation_completing",
                )
            if persisted is not None and persisted["status"] in _TERMINAL_STATUSES:
                self._clear_generation_recovery(persisted)
                self._store.delete_generation(
                    self.source_application_id, binding.object_type
                )
                persisted = None
            if (
                persisted is not None
                and persisted["sync_mode"] == "full"
                and persisted["status"] in _WRITABLE_STATUSES
            ):
                self._abort_and_clear_generation(persisted, replay_create=True)
                persisted = None
            if (
                persisted is not None
                and persisted["status"] in _WRITABLE_STATUSES
                and persisted["sync_mode"] == sync_mode
            ):
                try:
                    self._resume_writable(info.name, persisted)
                    return
                except AiHubPushRejected as exc:
                    if exc.error_code in {
                        "generation_receipt_incomplete",
                        "generation_not_writable",
                    }:
                        self._abort_and_clear_generation(
                            persisted, replay_create=True
                        )
                    else:
                        raise
        self._start_generation(source, info, binding, sync_mode)

    def write(
        self,
        source: str,
        info: TableInfo,
        rows: list[dict],
        batch_id: str,
        *,
        mode: SyncMode = "incremental",
        snapshot_id: str | None = None,
        table_run_id: str | None = None,
    ) -> int:
        del source, mode, snapshot_id, table_run_id
        self.ensure_protocol()
        generation = self._require_active(info.name)
        self._maybe_refresh_lease(generation)
        # Drain durable pending independently before evaluating the current
        # source chunk; changed ERP content must use the next sequence.
        self._drain_pending_batch(generation)
        binding = self._binding(info.name)
        version_items: list[tuple[str, str]] = []
        for row in rows:
            object_id = object_id_from_row(info, row)
            staged = map_row(info, row, binding, version=1)
            change_key = (
                payload_content_hash(staged.get("payload")) + ":" + staged["operation"]
            )
            version_items.append((object_id, change_key))
        versions = self._store.versions_for_batch(
            self.source_application_id, binding.object_type, version_items
        )
        records = []
        for row in rows:
            object_id = object_id_from_row(info, row)
            version = versions[object_id]
            records.append(map_row(info, row, binding, version=version))
        if not records:
            return 0
        chunks = self._chunk_records(generation, binding, batch_id, records)
        pending_chunks: list[tuple[str, list[dict[str, Any]]]] = []
        skipped = 0
        for chunk_id, chunk in chunks:
            if self._source_batch_already_confirmed(generation, chunk_id, chunk):
                skipped += len(chunk)
                continue
            pending_chunks.append((chunk_id, chunk))
        if not pending_chunks:
            return skipped
        pending_records = [record for _, chunk in pending_chunks for record in chunk]
        max_rows = self._cap("max_generation_rows")
        max_batches = self._cap("max_batches")
        max_bytes = self._cap("max_generation_bytes")
        self._check_generation_lifetime(generation)
        if generation.total_rows + len(pending_records) > max_rows:
            raise AiHubPushRejected(
                f"generation 行数将超过 max_generation_rows={max_rows}",
                error_code="max_generation_rows",
            )
        pending_batches = (generation.next_sequence_no - 1) + len(pending_chunks)
        if pending_batches > max_batches:
            raise AiHubPushRejected(
                f"generation 批次数将超过 max_batches={max_batches}",
                error_code="max_batches",
            )
        content_bytes = records_content_bytes(pending_records)
        if generation.total_bytes + content_bytes > max_bytes:
            raise AiHubPushRejected(
                f"generation 字节数将超过 max_generation_bytes={max_bytes}",
                error_code="max_generation_bytes",
            )
        written = skipped
        for index, (chunk_id, chunk) in enumerate(pending_chunks):
            if index:
                self._maybe_refresh_lease(generation)
            written += self._send_batch(
                generation, binding, chunk_id, chunk
            )
        return written

    def _source_batch_already_confirmed(
        self,
        generation: _ActiveGeneration,
        batch_id: str,
        records: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Skip source batches already durable for this generation.

        After a crash between durable receipt persistence and ERP cursor
        update, incremental_sync may re-read the same rows under a new
        caller batch_id. Match either the caller's external_batch_id or the
        content digest so confirmed batches are not resent.
        """
        digest = batch_content_digest(list(records))
        for item in generation.accepted:
            if str(item.get("external_batch_id") or "") == batch_id:
                return True
            if str(item.get("content_sha256") or "") == digest:
                return True
        return False

    def complete_table(
        self,
        source: str,
        info: TableInfo,
        completion_id: str,
        rows: int,
        batches: int,
        *,
        mode: SyncMode = "incremental",
        snapshot_id: str | None = None,
    ) -> None:
        del source, completion_id, snapshot_id, mode, rows, batches
        generation = self._require_active(info.name)
        self._maybe_refresh_lease(generation)
        self._complete(
            generation,
            rows=generation.total_rows,
            batches=len(generation.accepted),
        )

    def abort_table(
        self,
        source: str,
        info: TableInfo,
        *,
        mode: SyncMode,
        snapshot_id: str | None = None,
    ) -> None:
        del source, mode, snapshot_id
        generation = self._active.get(info.name)
        if generation is None:
            return
        self._abort_remote_generation(generation.generation_id)
        generation.status = "ABORTED"
        self._persist_generation(generation)
        self._active.pop(info.name, None)

    def complete_sync(self, source: str) -> None:
        del source
        for generation in list(self._active.values()):
            if generation.status in {"OPEN", "RECEIVING"}:
                self._complete(
                    generation,
                    rows=generation.total_rows,
                    batches=len(generation.accepted),
                )

    def abort_sync(self, source: str) -> None:
        del source
        for table, generation in list(self._active.items()):
            if generation.status in {"OPEN", "RECEIVING"}:
                self._abort_remote_generation(generation.generation_id)
                generation.status = "ABORTED"
                self._persist_generation(generation)
            self._active.pop(table, None)

    def _abort_remote_generation(self, generation_id: str) -> None:
        if not generation_id:
            return
        result = self._request(
            "POST", f"/generations/{generation_id}/abort"
        ) or {}
        status = str(result.get("status") or "")
        if status not in _TERMINAL_STATUSES:
            raise AiHubPushRejected(
                f"abort 未确认远端终态:{status or '(empty)'}",
                error_code="generation_abort_unconfirmed",
            )

    def _complete(self, generation: _ActiveGeneration, *, rows: int, batches: int) -> None:
        allocated = self._store.type_high_watermark(
            self.source_application_id, generation.object_type
        )
        high_watermark = max(generation.high_watermark, allocated)
        payload = generation.complete_request or {
            "expected_batch_count": batches,
            "total_rows": rows,
            "ordered_batch_digest": ordered_batch_digest(generation.accepted),
            "high_watermark": high_watermark,
            "confirm_empty_full": (
                generation.sync_mode == "full" and rows == 0 and batches == 0
            ),
        }
        generation.high_watermark = high_watermark
        generation.complete_request = payload
        generation.status = "COMPLETING"
        self._persist_generation(generation)
        try:
            result = self._request(
                "POST",
                f"/generations/{generation.generation_id}/complete",
                payload,
            ) or {}
            status = self._wait_completed(generation, result)
            generation.status = status
            self._persist_generation(generation)
            if generation.table in self._active:
                self._active[generation.table] = generation
        except AiHubPushRejected as exc:
            if exc.error_code in _COMPLETE_UNCERTAIN:
                generation.status = "COMPLETING"
                self._persist_generation(generation)
                self._active.pop(generation.table, None)
                raise
            remote_status = self._remote_generation_status(generation.generation_id)
            if remote_status in _WRITABLE_STATUSES:
                self._abort_remote_generation(generation.generation_id)
                generation.status = "ABORTED"
            elif remote_status in _COMPLETE_PENDING:
                generation.status = "COMPLETING"
            elif remote_status:
                generation.status = remote_status
            else:
                generation.status = "COMPLETING"
            self._persist_generation(generation)
            self._active.pop(generation.table, None)
            raise

    def _remote_generation_status(self, generation_id: str) -> str:
        if not generation_id:
            return ""
        remote = self._request("GET", f"/generations/{generation_id}") or {}
        return str(remote.get("status") or "")

    def _wait_completed(
        self, generation: _ActiveGeneration, result: Mapping[str, Any]
    ) -> str:
        status = str(result.get("status") or "")
        error_code = str(result.get("error_code") or "") or None
        if status == _COMPLETE_SUCCESS:
            return status
        if status in _COMPLETE_FAILURE:
            raise AiHubPushRejected(
                f"generation failed during completion:{status}",
                error_code=error_code or "generation_complete_mismatch",
            )
        if status not in _COMPLETE_PENDING:
            raise AiHubPushRejected(
                f"generation complete 返回未知或空状态:{status or '(empty)'}",
                error_code="generation_complete_unknown",
            )
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._complete_poll_interval:
                self._sleep(self._complete_poll_interval)
            polled = self._request(
                "GET", f"/generations/{generation.generation_id}"
            ) or {}
            status = str(polled.get("status") or "")
            error_code = str(polled.get("error_code") or "") or None
            if status == _COMPLETE_SUCCESS:
                return status
            if status in _COMPLETE_FAILURE:
                raise AiHubPushRejected(
                    f"generation failed during completion:{status}",
                    error_code=error_code or "generation_complete_mismatch",
                )
            if status not in _COMPLETE_PENDING:
                raise AiHubPushRejected(
                    f"generation complete 轮询到未知或空状态:{status or '(empty)'}",
                    error_code="generation_complete_unknown",
                )
        raise AiHubPushRejected(
            "generation complete 超时仍未到达 COMPLETED",
            error_code="generation_complete_timeout",
        )

    def _cap(self, name: str) -> int:
        return self._parse_cap(self._capabilities or {}, name)

    @staticmethod
    def _parse_cap(caps: Mapping[str, Any], name: str) -> int:
        try:
            value = int(caps[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise AiHubProtocolError(
                f"AI Hub capabilities 缺少有效 {name}"
            ) from exc
        if value < 1:
            raise AiHubProtocolError(f"AI Hub capabilities.{name} 必须为正整数")
        return value

    def _batch_body(
        self,
        generation: _ActiveGeneration,
        binding: ObjectBinding,
        batch_id: str,
        sequence_no: int,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        allocated = self._store.type_high_watermark(
            self.source_application_id, binding.object_type
        )
        high_watermark = max(generation.high_watermark, allocated)
        if records:
            high_watermark = max(high_watermark, max(r["version"] for r in records))
        return {
            "sequence_no": sequence_no,
            "external_batch_id": batch_id,
            "payload_contract_version": binding.payload_contract_version,
            "high_watermark": high_watermark,
            "content_sha256": batch_content_digest(records),
            "schema_fingerprint": binding.schema_fingerprint,
            "records": records,
        }

    def _chunk_records(
        self,
        generation: _ActiveGeneration,
        binding: ObjectBinding,
        batch_id: str,
        records: list[dict[str, Any]],
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        del generation, binding
        page_limit = self._cap("page_limit_max")
        payload_max = self._cap("payload_max_bytes")
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for record in records:
            size = payload_size_bytes(
                None if record.get("payload") is None else dict(record["payload"])
            )
            if size > payload_max:
                raise AiHubPushRejected(
                    "单条记录 payload 超过 payload_max_bytes",
                    error_code="payload_too_large",
                )
            if current and len(current) >= page_limit:
                chunks.append(current)
                current = []
            current.append(record)
        if current:
            chunks.append(current)
        if len(chunks) == 1:
            return [(batch_id, chunks[0])]
        return [(f"{batch_id}:{index + 1}", chunk) for index, chunk in enumerate(chunks)]

    def _send_batch(
        self,
        generation: _ActiveGeneration,
        binding: ObjectBinding,
        batch_id: str,
        records: list[dict[str, Any]],
    ) -> int:
        self._check_generation_lifetime(generation)
        desired_digest = batch_content_digest(records)
        drained = self._drain_pending_batch(generation)
        if drained is not None and str(drained["content_sha256"]) == desired_digest:
            return len(records)
        if self._source_batch_already_confirmed(generation, batch_id, records):
            return len(records)

        sequence_no = generation.next_sequence_no
        payload = self._batch_body(
            generation, binding, batch_id, sequence_no, records
        )
        spool_path = str(
            self._batch_spool.write(
                self.source_application_id,
                generation.object_type,
                sequence_no,
                payload,
            )
        )
        self._store.persist_pending_batch(
            self.source_application_id,
            generation.object_type,
            sequence_no=sequence_no,
            external_batch_id=str(payload["external_batch_id"]),
            content_sha256=str(payload["content_sha256"]),
            record_count=len(payload["records"]),
            spool_path=PendingBatchSpool._resolve_spool_path(spool_path),
        )
        receipt = self._request(
            "POST",
            f"/generations/{generation.generation_id}/batches",
            payload,
        )
        self._assert_durable_receipt(receipt, payload)
        accepted = {
            "sequence_no": int(payload["sequence_no"]),
            "external_batch_id": str(payload["external_batch_id"]),
            "content_sha256": str(payload["content_sha256"]),
            "record_count": len(payload["records"]),
            "high_watermark": int(payload["high_watermark"]),
        }
        generation.next_sequence_no = sequence_no + 1
        generation.high_watermark = int(payload["high_watermark"])
        generation.total_rows += len(payload["records"])
        generation.total_bytes += records_content_bytes(payload["records"])
        generation.status = "RECEIVING"
        generation.accepted.append(
            {
                "sequence_no": accepted["sequence_no"],
                "external_batch_id": accepted["external_batch_id"],
                "content_sha256": accepted["content_sha256"],
            }
        )
        self._store.commit_batch(
            source_application_id=self.source_application_id,
            object_type=generation.object_type,
            receipt=accepted,
            sequence_no=sequence_no,
            spool_path=spool_path,
            generation_id=generation.generation_id,
            external_generation_id=generation.external_generation_id,
            table_name=generation.table,
            sync_mode=generation.sync_mode,
            status=generation.status,
            next_sequence_no=generation.next_sequence_no,
            high_watermark=generation.high_watermark,
            total_rows=generation.total_rows,
            total_bytes=generation.total_bytes,
            started_at=generation.started_at,
            create_request=generation.create_request,
            complete_request=generation.complete_request,
        )
        self._batch_spool.delete(spool_path)
        return len(payload["records"])

    def _drain_pending_batch(
        self, generation: _ActiveGeneration
    ) -> dict[str, Any] | None:
        """Flush durable pending for next_sequence_no before sending a new chunk.

        Pending always wins for its sequence. After drain, callers compare the
        current chunk digest and send changed content as the following sequence.
        """
        sequence_no = generation.next_sequence_no
        pending = self._store.load_pending_batch(
            self.source_application_id,
            generation.object_type,
            sequence_no,
        )
        if pending is None:
            return None
        drained = {
            "content_sha256": str(pending["content_sha256"]),
            "record_count": int(pending["record_count"]),
            "external_batch_id": str(pending["external_batch_id"]),
        }
        if self._reconcile_receipt_from_remote(generation, sequence_no):
            return drained
        if self._reconcile_receipt_from_spool(generation, sequence_no):
            return drained
        raise AiHubPushRejected(
            f"无法排空 pending batch sequence_no={sequence_no}",
            error_code="pending_batch_drain_failed",
        )

    def _check_generation_lifetime(self, generation: _ActiveGeneration) -> None:
        if generation.started_at <= 0:
            return
        lifetime = self._cap("max_generation_lifetime_seconds")
        elapsed = time.time() - generation.started_at
        if elapsed > lifetime:
            raise AiHubPushRejected(
                f"generation 已超过 max_generation_lifetime_seconds={lifetime}",
                error_code="generation_limit_exceeded",
            )

    @staticmethod
    def _assert_durable_receipt(
        receipt: dict | None, payload: Mapping[str, Any]
    ) -> None:
        if not isinstance(receipt, dict) or not receipt:
            raise AiHubPushRejected(
                "批次回执为空",
                error_code="batch_receipt_missing",
            )
        expected = {
            "sequence_no": int(payload["sequence_no"]),
            "external_batch_id": str(payload["external_batch_id"]),
            "content_sha256": str(payload["content_sha256"]),
            "record_count": len(payload["records"]),
            "high_watermark": int(payload["high_watermark"]),
        }
        for key in _DURABLE_RECEIPT_FIELDS:
            if key not in receipt or receipt[key] is None:
                raise AiHubPushRejected(
                    f"批次回执缺少 {key}",
                    error_code="batch_receipt_missing",
                )
            actual = receipt[key]
            want = expected[key]
            if key == "content_sha256" or key == "external_batch_id":
                if str(actual) != str(want):
                    raise AiHubPushRejected(
                        f"批次回执 {key} 不匹配",
                        error_code="batch_receipt_mismatch",
                    )
                continue
            try:
                actual_n = int(actual)
            except (TypeError, ValueError) as exc:
                raise AiHubPushRejected(
                    f"批次回执 {key} 不匹配",
                    error_code="batch_receipt_mismatch",
                ) from exc
            if actual_n != want:
                raise AiHubPushRejected(
                    f"批次回执 {key} 不匹配",
                    error_code="batch_receipt_mismatch",
                )

    def _binding(self, table: str) -> ObjectBinding:
        binding = self.bindings.get(table)
        if binding is None:
            raise ObjectMappingError(f"{table}: 缺少 AI Hub object binding")
        return binding

    def _require_active(self, table: str) -> _ActiveGeneration:
        generation = self._active.get(table)
        if generation is None:
            raise AiHubPushRejected(f"{table}: generation 尚未开启")
        return generation

    def _start_generation(
        self,
        source: str,
        info: TableInfo,
        binding: ObjectBinding,
        sync_mode: str,
    ) -> None:
        external_generation_id = f"{source}:{binding.object_type}:{uuid.uuid4().hex}"
        body = {
            "source_application_id": self.source_application_id,
            "object_type": binding.object_type,
            "external_generation_id": external_generation_id,
            "sync_mode": sync_mode,
            "protocol_version": AI_HUB_PUSH_PROTOCOL_VERSION,
            "lease_seconds": self.lease_seconds,
            "purpose": "production",
        }
        self._store.clear_receipts(
            self.source_application_id, binding.object_type
        )
        allocated = self._store.type_high_watermark(
            self.source_application_id, binding.object_type
        )
        generation = _ActiveGeneration(
            generation_id="",
            external_generation_id=external_generation_id,
            object_type=binding.object_type,
            table=info.name,
            sync_mode=sync_mode,
            create_request=body,
            high_watermark=allocated,
            started_at=time.time(),
        )
        self._persist_generation(generation)
        result = self._request("POST", "/generations", body) or {}
        self._activate_remote_generation(info.name, generation, result, resume=False)

    def _resume_writable(self, table: str, persisted: Mapping[str, Any]) -> None:
        generation = self._generation_from_persisted(table, persisted)
        result = self._request(
            "POST", "/generations", generation.create_request
        ) or {}
        self._activate_remote_generation(
            table, generation, result, resume=True
        )

    def _resume_completing(self, table: str, persisted: Mapping[str, Any]) -> None:
        generation = self._generation_from_persisted(table, persisted)
        self._active[table] = generation
        self._complete(
            generation,
            rows=generation.total_rows,
            batches=len(generation.accepted),
        )

    def _activate_remote_generation(
        self,
        table: str,
        generation: _ActiveGeneration,
        result: Mapping[str, Any],
        *,
        resume: bool = False,
    ) -> None:
        generation_id = str(
            result.get("generation_id") or generation.generation_id or ""
        )
        status = str(result.get("status") or generation.status or "")
        if not generation_id:
            raise AiHubPushRejected("AI Hub 未返回 generation_id")
        if status not in _WRITABLE_STATUSES:
            generation.generation_id = generation_id
            generation.status = status
            self._persist_generation(generation)
            self._clear_generation_recovery(
                {
                    "object_type": generation.object_type,
                }
            )
            raise AiHubPushRejected(
                f"AI Hub generation 状态不可写:{status}",
                error_code=str(result.get("error_code") or "") or "generation_not_writable",
            )
        generation.generation_id = generation_id
        remote_next = int(result.get("next_sequence_no") or 1)
        if generation.started_at <= 0:
            generation.started_at = time.time()
        self._reconcile_missing_receipts(generation, remote_next)
        receipts = self._store.load_receipts(
            self.source_application_id, generation.object_type
        )
        if len(receipts) != remote_next - 1:
            raise AiHubPushRejected(
                "generation 回执不完整,无法恢复序号或 completion digest",
                error_code="generation_receipt_incomplete",
            )
        generation.generation_id = generation_id
        generation.status = status
        generation.next_sequence_no = remote_next
        generation.accepted = [
            {
                "sequence_no": item["sequence_no"],
                "external_batch_id": item["external_batch_id"],
                "content_sha256": item["content_sha256"],
            }
            for item in receipts
        ]
        if receipts:
            generation.high_watermark = max(
                generation.high_watermark,
                max(int(item["high_watermark"]) for item in receipts),
            )
            generation.total_rows = sum(int(item["record_count"]) for item in receipts)
        self._persist_generation(generation)
        self._active[table] = generation
        if resume:
            self._maybe_refresh_lease(generation, force=True)
        else:
            self._mark_lease_refreshed(generation)

    def _reconcile_missing_receipts(
        self, generation: _ActiveGeneration, remote_next: int
    ) -> None:
        receipts = self._store.load_receipts(
            self.source_application_id, generation.object_type
        )
        while len(receipts) < remote_next - 1:
            sequence_no = len(receipts) + 1
            if not self._reconcile_receipt_from_remote(generation, sequence_no):
                if not self._reconcile_receipt_from_spool(generation, sequence_no):
                    raise AiHubPushRejected(
                        "generation 回执不完整,无法恢复序号或 completion digest",
                        error_code="generation_receipt_incomplete",
                    )
            receipts = self._store.load_receipts(
                self.source_application_id, generation.object_type
            )

    def _reconcile_receipt_from_remote(
        self, generation: _ActiveGeneration, sequence_no: int
    ) -> bool:
        if not generation.generation_id:
            return False
        remote = self._request(
            "GET", f"/generations/{generation.generation_id}"
        ) or {}
        accepted = remote.get("accepted") or []
        for item in accepted:
            if int(item.get("sequence_no") or 0) != sequence_no:
                continue
            receipt = {
                "sequence_no": sequence_no,
                "external_batch_id": str(item["external_batch_id"]),
                "content_sha256": str(item["content_sha256"]),
                "record_count": int(item.get("record_count") or 0),
                "high_watermark": int(item.get("high_watermark") or 0),
            }
            pending = self._store.load_pending_batch(
                self.source_application_id,
                generation.object_type,
                sequence_no,
            )
            if pending is not None:
                if str(pending["content_sha256"]) != receipt["content_sha256"]:
                    raise AiHubPushRejected(
                        "远端回执与本地 pending 摘要不匹配",
                        error_code="batch_receipt_mismatch",
                    )
                receipt["record_count"] = int(pending["record_count"])
                spool_path = pending["spool_path"]
            else:
                spool_path = None
            self._apply_reconciled_receipt(
                generation, receipt, sequence_no, spool_path
            )
            return True
        return False

    def _reconcile_receipt_from_spool(
        self, generation: _ActiveGeneration, sequence_no: int
    ) -> bool:
        pending = self._store.load_pending_batch(
            self.source_application_id,
            generation.object_type,
            sequence_no,
        )
        if pending is None:
            return False
        if not generation.generation_id:
            return False
        payload = self._batch_spool.read(pending["spool_path"])
        receipt = self._request(
            "POST",
            f"/generations/{generation.generation_id}/batches",
            payload,
        )
        self._assert_durable_receipt(receipt, payload)
        accepted = {
            "sequence_no": int(payload["sequence_no"]),
            "external_batch_id": str(payload["external_batch_id"]),
            "content_sha256": str(payload["content_sha256"]),
            "record_count": len(payload["records"]),
            "high_watermark": int(payload["high_watermark"]),
        }
        self._apply_reconciled_receipt(
            generation,
            accepted,
            sequence_no,
            pending["spool_path"],
        )
        return True

    def _apply_reconciled_receipt(
        self,
        generation: _ActiveGeneration,
        receipt: Mapping[str, Any],
        sequence_no: int,
        spool_path: str | None,
    ) -> None:
        generation.high_watermark = max(
            generation.high_watermark, int(receipt["high_watermark"])
        )
        generation.total_rows += int(receipt["record_count"])
        if int(receipt["record_count"]) > 0 and spool_path:
            generation.total_bytes += records_content_bytes(
                self._batch_spool.read(spool_path)["records"]
            )
        generation.accepted.append(
            {
                "sequence_no": int(receipt["sequence_no"]),
                "external_batch_id": str(receipt["external_batch_id"]),
                "content_sha256": str(receipt["content_sha256"]),
            }
        )
        generation.next_sequence_no = sequence_no + 1
        generation.status = "RECEIVING"
        self._store.commit_batch(
            source_application_id=self.source_application_id,
            object_type=generation.object_type,
            receipt=receipt,
            sequence_no=sequence_no,
            spool_path=spool_path,
            generation_id=generation.generation_id,
            external_generation_id=generation.external_generation_id,
            table_name=generation.table,
            sync_mode=generation.sync_mode,
            status="RECEIVING",
            next_sequence_no=sequence_no + 1,
            high_watermark=generation.high_watermark,
            total_rows=generation.total_rows,
            total_bytes=generation.total_bytes,
            started_at=generation.started_at,
            create_request=generation.create_request,
            complete_request=generation.complete_request,
        )
        if spool_path:
            self._batch_spool.delete(spool_path)

    def _clear_generation_recovery(self, persisted: Mapping[str, Any]) -> None:
        object_type = str(persisted["object_type"])
        for pending in self._store.list_pending_batches(
            self.source_application_id, object_type
        ):
            self._batch_spool.delete(pending.get("spool_path"))
        self._store.clear_receipts(self.source_application_id, object_type)
        self._store.clear_pending_batches(self.source_application_id, object_type)

    def _resolve_remote_generation(
        self, persisted: Mapping[str, Any]
    ) -> tuple[str, str]:
        generation_id = str(persisted.get("generation_id") or "")
        if generation_id:
            remote = self._request(
                "GET", f"/generations/{generation_id}"
            ) or {}
            status = str(remote.get("status") or persisted.get("status") or "")
            return generation_id, status
        create_request = persisted.get("create_request")
        if not isinstance(create_request, Mapping):
            return "", ""
        result = self._request("POST", "/generations", dict(create_request)) or {}
        return (
            str(result.get("generation_id") or ""),
            str(result.get("status") or ""),
        )

    def _abort_and_clear_generation(
        self,
        persisted: Mapping[str, Any],
        *,
        replay_create: bool = False,
    ) -> None:
        if replay_create or not str(persisted.get("generation_id") or ""):
            generation_id, remote_status = self._resolve_remote_generation(
                persisted
            )
        else:
            generation_id = str(persisted.get("generation_id") or "")
            remote_status = str(persisted.get("status") or "")
            if generation_id:
                remote = self._request(
                    "GET", f"/generations/{generation_id}"
                ) or {}
                remote_status = str(remote.get("status") or remote_status)
        if generation_id:
            if remote_status in _WRITABLE_STATUSES:
                self._abort_remote_generation(generation_id)
            elif remote_status not in _TERMINAL_STATUSES:
                raise AiHubPushRejected(
                    f"远端 generation 状态不可清理:{remote_status or '(empty)'}",
                    error_code="generation_not_writable",
                )
        self._clear_generation_recovery(persisted)
        self._store.delete_generation(
            self.source_application_id, str(persisted["object_type"])
        )

    def _generation_from_persisted(
        self, table: str, persisted: Mapping[str, Any]
    ) -> _ActiveGeneration:
        receipts = self._store.load_receipts(
            self.source_application_id, persisted["object_type"]
        )
        object_type = str(persisted["object_type"])
        return _ActiveGeneration(
            generation_id=str(persisted["generation_id"] or ""),
            external_generation_id=str(persisted["external_generation_id"]),
            object_type=object_type,
            table=table,
            sync_mode=str(persisted["sync_mode"]),
            create_request=dict(persisted["create_request"]),
            next_sequence_no=int(persisted["next_sequence_no"]),
            accepted=[
                {
                    "sequence_no": item["sequence_no"],
                    "external_batch_id": item["external_batch_id"],
                    "content_sha256": item["content_sha256"],
                }
                for item in receipts
            ],
            high_watermark=int(persisted["high_watermark"]),
            total_rows=int(persisted["total_rows"]),
            total_bytes=int(persisted["total_bytes"]),
            started_at=float(persisted.get("started_at") or 0.0),
            status=str(persisted["status"]),
            complete_request=(
                dict(persisted["complete_request"])
                if persisted.get("complete_request")
                else None
            ),
        )

    def _persist_generation(self, generation: _ActiveGeneration) -> None:
        self._store.upsert_generation(
            source_application_id=self.source_application_id,
            object_type=generation.object_type,
            table_name=generation.table,
            generation_id=generation.generation_id,
            external_generation_id=generation.external_generation_id,
            sync_mode=generation.sync_mode,
            status=generation.status,
            next_sequence_no=generation.next_sequence_no,
            high_watermark=generation.high_watermark,
            total_rows=generation.total_rows,
            total_bytes=generation.total_bytes,
            started_at=generation.started_at,
            create_request=generation.create_request,
            complete_request=generation.complete_request,
        )

    def _maybe_refresh_lease(
        self, generation: _ActiveGeneration, *, force: bool = False
    ) -> None:
        if generation.status not in _WRITABLE_STATUSES or not generation.generation_id:
            return
        self._check_generation_lifetime(generation)
        now = time.monotonic()
        last = self._lease_refreshed_at.get(generation.generation_id, 0.0)
        if not force and (now - last) < self.heartbeat_interval_seconds:
            return
        self._request(
            "POST",
            f"/generations/{generation.generation_id}/heartbeat",
            {"lease_seconds": self.lease_seconds},
        )
        self._mark_lease_refreshed(generation)

    def _mark_lease_refreshed(self, generation: _ActiveGeneration) -> None:
        if generation.generation_id:
            self._lease_refreshed_at[generation.generation_id] = time.monotonic()
