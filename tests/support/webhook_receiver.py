"""d2a-push v1 参考接收端(仅测试用):演示第三方平台的最小实现要点。

- 校验 HMAC 签名(配置 secret 时)与 Bearer Token(配置 token 时);
- 按事件 ``id`` 幂等去重:重复投递直接 2xx,不重复生效;
- 增量批次按主键 upsert 立即生效;
- 全量快照写入 staging,仅在 table.complete 且批次 / 行数核对一致时整表替换,
  table.abort 丢弃 staging。

``send`` 方法签名与 ``WebhookSink`` 的 send 注入点一致,免起真实 HTTP 服务。
"""

from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass, field

from data2agent.middle.extract.webhook_sink import (
    EVENT_TYPE_PREFIX,
    SIGNATURE_HEADER,
    verify_signature,
)
from data2agent.protocol.ingest import decode_transport_rows


def _http_error(url: str, code: int, detail: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, code, detail, {}, io.BytesIO(json.dumps({"detail": detail}).encode()))


@dataclass
class _Staging:
    rows: dict[tuple, dict] = field(default_factory=dict)
    batches: int = 0


@dataclass
class WebhookReceiver:
    token: str | None = None
    secret: str | None = None
    # 前 N 次请求返回该状态码(模拟限流 / 短暂故障),用于验证重试幂等。
    fail_first: int = 0
    fail_status: int = 503
    events: list[dict] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    deliveries: int = 0
    tables: dict[str, dict[tuple, dict]] = field(default_factory=dict)
    _staging: dict[str, _Staging] = field(default_factory=dict)
    _incremental_batches: dict[str, int] = field(default_factory=dict)

    def send(self, url: str, body: bytes, headers: dict[str, str],
             timeout: float) -> tuple[int, bytes]:
        self.deliveries += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise _http_error(url, self.fail_status, "temporarily unavailable")
        if self.token and headers.get("Authorization") != f"Bearer {self.token}":
            raise _http_error(url, 401, "bad token")
        if self.secret and not verify_signature(
            self.secret, headers.get(SIGNATURE_HEADER, ""), body,
        ):
            raise _http_error(url, 401, "bad signature")
        event = json.loads(body)
        if headers.get("Idempotency-Key") != event["id"]:
            raise _http_error(url, 400, "Idempotency-Key 与事件 id 不一致")
        if event["id"] in self.seen_ids:
            return 200, b'{"duplicate": true}'
        self._apply(url, event)
        self.seen_ids.add(event["id"])
        self.events.append(event)
        return 200, b'{"accepted": true}'

    @property
    def event_types(self) -> list[str]:
        return [e["type"].removeprefix(EVENT_TYPE_PREFIX) for e in self.events]

    def count(self, table: str) -> int:
        return len(self.tables.get(table, {}))

    def _apply(self, url: str, event: dict) -> None:
        kind = event["type"].removeprefix(EVENT_TYPE_PREFIX)
        data = event["data"]
        if kind in ("ping", "sync.begin", "sync.complete", "sync.abort"):
            return
        table = data["table"]
        pk = data["pk"]
        if kind == "table.begin":
            if data["mode"] == "full_refresh":
                self._staging[data["snapshot_id"]] = _Staging()
            return
        if kind == "batch":
            rows = decode_transport_rows(data["rows"], data["columns"])
            if data["mode"] == "full_refresh":
                staging = self._staging[data["snapshot_id"]]
                for i, row in enumerate(rows):
                    # 无 pk 的快照不去重:重复行也是快照内容,否则行数核对必然失败。
                    key = (tuple(row[c] for c in pk) if pk
                           else (data["batch_id"], i))
                    staging.rows[key] = row
                staging.batches += 1
            else:
                target = self.tables.setdefault(table, {})
                for row in rows:
                    target[tuple(row[c] for c in pk)] = row
                run = data["table_run_id"]
                self._incremental_batches[run] = (
                    self._incremental_batches.get(run, 0) + 1)
            return
        if kind == "table.complete":
            if data["mode"] == "full_refresh":
                staging = self._staging.pop(data["snapshot_id"])
                if (staging.batches, len(staging.rows)) != (
                    data["batches"], data["rows"],
                ):
                    raise _http_error(url, 409, "快照批次 / 行数核对不一致")
                self.tables[table] = staging.rows
            else:
                self.tables.setdefault(table, {})
                got = self._incremental_batches.pop(data["table_run_id"], 0)
                if got != data["batches"]:
                    raise _http_error(url, 409, "增量批次数核对不一致")
            return
        if kind == "table.abort":
            self._staging.pop(data["snapshot_id"], None)
            return
        raise _http_error(url, 400, f"未知事件类型 {kind}")
