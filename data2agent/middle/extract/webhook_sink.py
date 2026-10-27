"""通用 HTTP 批量推送出口(d2a-push v1):任何平台实现一个 HTTPS 端点即可接收。

与 ingest v3(HttpPushSink)的取舍:
- 不需要握手 / generation 租约 / 对账端点;接收方只需按事件类型处理 JSON;
- 每个请求是一条 CloudEvents 1.0 结构化事件(``application/cloudevents+json``),
  事件 ``id`` 即幂等键(同时放在 ``Idempotency-Key`` 头,全局唯一,以 sync_id 开头),
  重试复用同一 id 与 body;
- 投递语义为至少一次、按序串行:上一事件 2xx 后才发送下一事件;
- 无远端对账,配置层禁止 reconcile_at;需要删除检测的表应使用 full_refresh。

事件类型(``type`` 字段,前缀 ``io.data2agent.push.v1.``):
  sync.begin → (table.begin → batch* → table.complete | table.abort)* → sync.complete | sync.abort
  另有 ping,仅用于管理页连通测试。

协议说明(字段、签名校验、接收方实现要点)见 docs/runbook/webhook-push.md。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from ...protocol.ingest import BINARY_ENCODING, encode_transport_rows
from ...shared.store.landing import normalize_value
from ...shared.store.table import TableInfo
from .sink import SyncMode, _brief_error_str

if TYPE_CHECKING:
    from ...shared.store.landing import LandingStore

PUSH_SPEC_VERSION = "1"
EVENT_TYPE_PREFIX = "io.data2agent.push.v1."
CONTENT_TYPE = "application/cloudevents+json; charset=utf-8"
SIGNATURE_HEADER = "X-D2A-Signature"

# send(url, body, headers, timeout) -> (status, response_body);非 2xx 抛 HTTPError。
SendFn = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


def _urllib_send(
    url: str, body: bytes, headers: dict[str, str], timeout: float,
    context: ssl.SSLContext | None = None,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return resp.status, resp.read()


def sign_body(secret: str, timestamp: int, body: bytes) -> str:
    """签名头取值:``t=<unix 秒>,v1=<hex(HMAC-SHA256(secret, "<t>." + body))>``。"""
    mac = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body,
        hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify_signature(
    secret: str, header: str, body: bytes, *,
    tolerance_seconds: int = 300, now: float | None = None,
) -> bool:
    """接收方参考实现:校验签名并拒绝超出时间容差的重放。"""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = sign_body(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, given)


class WebhookSink:
    """把抽取生命周期与 raw 批次以 CloudEvents 推送到任意 HTTPS 端点。"""

    def __init__(self, url: str, token: str | None = None, *,
                 hmac_secret: str | None = None,
                 timeout: float = 30.0, retries: int = 3,
                 ca_bundle: str | None = None,
                 send: SendFn | None = None,
                 landing: "LandingStore | None" = None,
                 source: str = "",
                 run_id: int | None = None,
                 clock: Callable[[], float] = time.time):
        if not url:
            raise ValueError("WebhookSink 需要接收端点 url")
        self.url = url
        self.token = token
        self.hmac_secret = hmac_secret
        self.timeout = timeout
        self.retries = max(1, retries)
        context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None
        self._send = send or (
            lambda endpoint, body, headers, request_timeout:
            _urllib_send(endpoint, body, headers, request_timeout, context))
        self._clock = clock
        self._landing = landing
        self._source = source
        self._run_id = run_id
        self._sync_id: str | None = None
        self._last_retry_count = 0
        self._last_error_category: str | None = None
        self._last_retryable: bool | None = None

    # ---- 事件构造与投递 ----

    def _event(self, event_type: str, event_id: str, source: str,
               data: dict, subject: str | None = None) -> dict:
        event = {
            "specversion": "1.0",
            "id": event_id,
            "source": f"data2agent/{source}",
            "type": EVENT_TYPE_PREFIX + event_type,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "datacontenttype": "application/json",
            "d2apushversion": PUSH_SPEC_VERSION,
            "data": data,
        }
        if subject:
            event["subject"] = subject
        return event

    def _headers(self, event_id: str, body: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": CONTENT_TYPE,
            "Idempotency-Key": event_id,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.hmac_secret:
            headers[SIGNATURE_HEADER] = sign_body(
                self.hmac_secret, int(self._clock()), body)
        return headers

    def _deliver(
        self, event: dict,
        on_retry: Callable[[int, float], None] | None = None,
    ) -> None:
        body = json.dumps(
            event, ensure_ascii=False, separators=(",", ":"),
            default=str).encode("utf-8")
        last: Exception | None = None
        self._last_retry_count = 0
        self._last_error_category = None
        self._last_retryable = None
        for attempt in range(self.retries):
            try:
                # 每次尝试重新签名(时间戳更新),body 与幂等键保持不变。
                self._send(
                    self.url, body, self._headers(event["id"], body), self.timeout)
                self._last_retry_count = attempt
                return
            except urllib.error.HTTPError as e:
                if e.code not in (408, 425, 429) and e.code < 500:
                    self._last_retry_count = attempt
                    self._last_retryable = False
                    self._last_error_category = (
                        "auth" if e.code in (401, 403) else "request_validation")
                    raise RuntimeError(
                        f"推送事件 {event['type']} 被接收端拒绝(HTTP {e.code})") from e
                last = e
                self._last_error_category = (
                    "rate_limit" if e.code == 429 else "platform_unavailable")
                self._last_retryable = True
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
                last = e
                self._last_error_category = "network"
                self._last_retryable = True
            self._last_retry_count = attempt
            if attempt + 1 < self.retries:
                delay = min(2 ** attempt, 10)
                if on_retry is not None:
                    try:
                        on_retry(attempt + 1, delay)
                    except Exception:
                        pass
                time.sleep(delay + random.uniform(0, delay * 0.2))
        raise RuntimeError(
            f"推送事件 {event['type']} 失败(重试 {self.retries} 次):{last}。"
            "建议：检查接收端点、网络与鉴权配置后重试") from last

    def _log_push(self, step_kind: str, table: str, mode: str, *,
                  batch_id: str | None = None, rows_count: int | None = None,
                  status: str = "ok", error_detail: str | None = None,
                  retry_count: int = 0, duration_ms: float | None = None) -> None:
        if self._landing is None:
            return
        try:
            self._landing.record_push_log(
                source=self._source, step_kind=step_kind, table=table,
                mode=mode, run_id=self._run_id, batch_id=batch_id,
                rows_count=rows_count, status=status,
                error_detail=error_detail,
                retry_count=max(retry_count, self._last_retry_count),
                duration_ms=duration_ms, generation_id=self._sync_id,
                error_category=(
                    self._last_error_category if status != "ok" else None),
                retryable=self._last_retryable if status != "ok" else None,
                receipt_received=True if status == "ok" else None)
        except Exception:
            pass  # push log 写入失败不应中断同步

    def _send_logged(self, event: dict, step_kind: str, table: str, mode: str, *,
                     batch_id: str | None = None,
                     rows_count: int | None = None) -> None:
        t0 = time.time()

        def on_retry(retry_count: int, delay: float) -> None:
            self._log_push(
                step_kind, table, mode, batch_id=batch_id, rows_count=rows_count,
                status="retrying",
                error_detail=f"请求失败，正在退避约 {delay:g} 秒后重试",
                retry_count=retry_count, duration_ms=(time.time() - t0) * 1000)

        try:
            self._deliver(event, on_retry=on_retry)
        except Exception:
            self._log_push(
                step_kind, table, mode, batch_id=batch_id, rows_count=rows_count,
                status="failed", error_detail=_brief_error_str(),
                duration_ms=(time.time() - t0) * 1000)
            raise
        self._log_push(
            step_kind, table, mode, batch_id=batch_id, rows_count=rows_count,
            status="ok", duration_ms=(time.time() - t0) * 1000)

    def _table_data(self, info: TableInfo, mode: SyncMode,
                    snapshot_id: str | None) -> dict:
        return {
            "sync_id": self._sync_id,
            "table": info.name,
            "schema": info.schema,
            "mode": mode,
            "columns": [[c, t] for c, t in info.columns],
            "pk": list(info.pk),
            "snapshot_id": snapshot_id,
        }

    def _require_sync(self) -> str:
        # 未经 begin_sync 直接写表时(如单表工具调用)也要有稳定的批次归属。
        if self._sync_id is None:
            self._sync_id = f"sync-{uuid.uuid4().hex}"
        return self._sync_id

    # ---- Sink 生命周期 ----

    def bind_run(self, run_id: int) -> None:
        self._run_id = run_id

    def ping(self, source: str) -> None:
        """连通测试:发送一条 ping 事件,接收端返回 2xx 即视为可达且鉴权通过。"""
        self._deliver(self._event(
            "ping", f"ping-{uuid.uuid4().hex}", source, {}))

    def begin_sync(self, source: str, tables: list[str], run_id: int) -> None:
        self._sync_id = f"sync-{uuid.uuid4().hex}"
        if self._landing is not None:
            self._landing.set_run_generation(run_id, self._sync_id)
        self._send_logged(
            self._event("sync.begin", f"{self._sync_id}:sync.begin", source,
                        {"sync_id": self._sync_id, "tables": tables}),
            "begin_generation", "*", "generation")

    def complete_sync(self, source: str) -> None:
        if self._sync_id is None:
            return
        self._send_logged(
            self._event("sync.complete", f"{self._sync_id}:sync.complete", source,
                        {"sync_id": self._sync_id}),
            "complete_generation", "*", "generation")

    def abort_sync(self, source: str) -> None:
        if self._sync_id is None:
            return
        self._send_logged(
            self._event("sync.abort", f"{self._sync_id}:sync.abort", source,
                        {"sync_id": self._sync_id}),
            "abort_generation", "*", "generation")

    def begin_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None:
        sync_id = self._require_sync()
        self._send_logged(
            self._event(
                "table.begin",
                f"{sync_id}:{info.name}:{snapshot_id or 'incremental'}:table.begin",
                source, self._table_data(info, mode, snapshot_id),
                subject=info.name),
            "begin_table", info.name, mode)

    def write(self, source: str, info: TableInfo, rows: list[dict], batch_id: str, *,
              mode: SyncMode = "incremental",
              snapshot_id: str | None = None,
              table_run_id: str | None = None) -> int:
        sync_id = self._require_sync()
        normalized = [
            {c: normalize_value(r.get(c)) for c, _ in info.columns}
            for r in rows
        ]
        encoded = encode_transport_rows(info.columns, normalized)
        data = {
            **self._table_data(info, mode, snapshot_id),
            "table_run_id": table_run_id or batch_id,
            "batch_id": batch_id,
            "row_count": len(encoded),
            "binary_encoding": BINARY_ENCODING,
            "rows": encoded,
        }
        # batch_id 只有 48 位随机前缀;拼上 sync_id 才能作为接收方长期保存的幂等键。
        self._send_logged(
            self._event("batch", f"{sync_id}:{batch_id}", source, data,
                        subject=info.name),
            "write", info.name, mode,
            batch_id=table_run_id or batch_id, rows_count=len(rows))
        return len(rows)

    def complete_table(self, source: str, info: TableInfo, completion_id: str,
                       rows: int, batches: int, *, mode: SyncMode = "incremental",
                       snapshot_id: str | None = None) -> None:
        sync_id = self._require_sync()
        data = {
            **self._table_data(info, mode, snapshot_id),
            "table_run_id": completion_id,
            "rows": rows,
            "batches": batches,
        }
        self._send_logged(
            self._event("table.complete",
                        f"{sync_id}:{completion_id}:table.complete",
                        source, data, subject=info.name),
            "complete_table", info.name, mode,
            batch_id=completion_id, rows_count=rows)

    def abort_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None:
        # 增量批次已逐批生效,只有全量快照需要通知接收方丢弃 staging。
        if mode != "full_refresh" or not snapshot_id:
            return
        sync_id = self._require_sync()
        self._send_logged(
            self._event("table.abort", f"{sync_id}:{snapshot_id}:table.abort",
                        source, self._table_data(info, mode, snapshot_id),
                        subject=info.name),
            "abort_table", info.name, mode, batch_id=snapshot_id)
