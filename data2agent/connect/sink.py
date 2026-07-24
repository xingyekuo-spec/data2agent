"""落地出口抽象(§12.3):抽取的 raw 批次去哪。

- LocalSink:写本地落地库 —— 同机 / 开发;
- HttpPushSink:POST 给数据平台接收端点 —— Pattern A 中间服务器用。

LocalSink 与 HttpPushSink 共用同一生命周期:
  begin_table → write → complete_table
全量(full_refresh)走 snapshot staging;增量走 upsert。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Literal, Protocol

from ..ingest.protocol import INGEST_PROTOCOL_VERSION
from .adapters.base import TableInfo
from .landing import LandingStore, normalize_value

SyncMode = Literal["incremental", "full_refresh"]


class Sink(Protocol):
    def begin_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None: ...

    def write(self, source: str, info: TableInfo, rows: list[dict], batch_id: str, *,
              mode: SyncMode = "incremental",
              snapshot_id: str | None = None) -> int: ...

    def complete_table(self, source: str, info: TableInfo, completion_id: str,
                       rows: int, batches: int, *, mode: SyncMode = "incremental",
                       snapshot_id: str | None = None) -> None: ...

    def abort_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None: ...


class LocalSink:
    """写本地落地库(与 HTTP 推送同一生命周期语义)。"""

    def __init__(self, landing: LandingStore):
        self.landing = landing

    def begin_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None:
        if mode == "full_refresh":
            if not snapshot_id:
                raise ValueError(f"{info.name}: full_refresh 需要 snapshot_id")
            self.landing.begin_snapshot(source, info, snapshot_id)
            return
        self.landing.ensure_raw_table(source, info)

    def write(self, source: str, info: TableInfo, rows: list[dict], batch_id: str, *,
              mode: SyncMode = "incremental",
              snapshot_id: str | None = None) -> int:
        if mode == "full_refresh":
            if not snapshot_id:
                raise ValueError(f"{info.name}: full_refresh 需要 snapshot_id")
            result = self.landing.write_snapshot_batch(
                source, info, snapshot_id, batch_id, rows)
            return int(result["ingested"])
        return self.landing.upsert_rows(source, info, rows, batch_id)

    def complete_table(self, source: str, info: TableInfo, completion_id: str,
                       rows: int, batches: int, *, mode: SyncMode = "incremental",
                       snapshot_id: str | None = None) -> None:
        if mode == "full_refresh":
            if not snapshot_id:
                raise ValueError(f"{info.name}: full_refresh 需要 snapshot_id")
            self.landing.complete_snapshot(
                source, info, snapshot_id, rows, batches)
            return
        # 增量:完成证据由 incremental_sync 的 d2a_run_step 记录
        return None

    def abort_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None:
        if mode == "full_refresh" and snapshot_id:
            self.landing.abort_snapshot(source, info.name, snapshot_id)


def _urllib_post(url: str, payload: dict, token: str | None, timeout: float) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def _urllib_get_json(url: str, token: str | None, timeout: float) -> dict:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ProtocolVersionError(RuntimeError):
    """平台不支持本端 ingest 发送协议(或不兼容)。"""


class HttpPushSink:
    """把 raw 批次 POST 给平台接收端点(中间服务器用,stdlib urllib)。"""

    def __init__(self, url: str, token: str | None = None, *,
                 timeout: float = 30.0, retries: int = 3,
                 post: Callable[[str, dict, str | None, float], None] | None = None,
                 get_json: Callable[[str, str | None, float], dict] | None = None):
        if not url:
            raise ValueError("HttpPushSink 需要平台接收端点 url")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = max(1, retries)
        self._post = post or _urllib_post
        self._get_json = get_json or _urllib_get_json
        self._protocol_checked = False

    def ensure_protocol(self) -> None:
        """同步前确认本端发送协议落在平台 supported 列表内;否则 fail-fast。

        新平台返回 supported_ingest_protocol_versions;旧平台仅有
        ingest_protocol_version 时回退为精确相等(兼容尚未升级的平台)。
        """
        if self._protocol_checked:
            return
        try:
            health = self._get_json(
                f"{self.url}/ingest/health", self.token, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise ProtocolVersionError(
                f"无法读取平台 ingest 协议版本:{e}。"
                f"建议：确认 sink.url 可达、Token 有效，以及平台 ingest 服务已启动"
            ) from e
        mine = INGEST_PROTOCOL_VERSION
        supported = health.get("supported_ingest_protocol_versions")
        if isinstance(supported, list) and supported:
            supported_s = [str(v) for v in supported]
            if mine not in supported_s:
                raise ProtocolVersionError(
                    f"ingest 协议不兼容:中间机发送 {mine}, "
                    f"平台支持 {supported_s}。"
                    f"建议：按 Release 说明同步升级平台与中间机，"
                    f"或换用平台仍支持的中间机包版本"
                )
        else:
            remote = health.get("active_ingest_protocol_version")
            if remote is None:
                remote = health.get("ingest_protocol_version")
            if remote != mine:
                raise ProtocolVersionError(
                    f"ingest 协议版本不一致:中间机要求 {mine}, "
                    f"平台返回 {remote!r}。"
                    f"建议：升级平台或中间机使协议号一致后再同步"
                )
        self._protocol_checked = True

    def _post_with_retry(self, path: str, payload: dict) -> None:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self._post(f"{self.url}{path}", payload, self.token, self.timeout)
                return
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(
            f"推送 {path} 失败(重试 {self.retries} 次):{last}。"
            f"建议：检查平台接收端点、网络与 ingest Token 后重试"
        )

    def _base_payload(self, source: str, info: TableInfo, mode: SyncMode,
                      snapshot_id: str | None) -> dict:
        return {
            "ingest_protocol_version": INGEST_PROTOCOL_VERSION,
            "source": source,
            "table": info.name,
            "mode": mode,
            "columns": [[c, t] for c, t in info.columns],
            "pk": list(info.pk),
            "snapshot_id": snapshot_id,
        }

    def begin_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None:
        self.ensure_protocol()
        payload = self._base_payload(source, info, mode, snapshot_id)
        self._post_with_retry("/ingest/table-begin", payload)

    def write(self, source: str, info: TableInfo, rows: list[dict], batch_id: str, *,
              mode: SyncMode = "incremental",
              snapshot_id: str | None = None) -> int:
        self.ensure_protocol()
        payload = {
            **self._base_payload(source, info, mode, snapshot_id),
            "batch_id": batch_id,
            "rows": [{c: normalize_value(r.get(c)) for c, _ in info.columns} for r in rows],
        }
        self._post_with_retry("/ingest/batch", payload)
        return len(rows)

    def complete_table(self, source: str, info: TableInfo, completion_id: str,
                       rows: int, batches: int, *, mode: SyncMode = "incremental",
                       snapshot_id: str | None = None) -> None:
        self.ensure_protocol()
        payload = {
            **self._base_payload(source, info, mode, snapshot_id),
            "completion_id": completion_id,
            "rows": rows,
            "batches": batches,
        }
        self._post_with_retry("/ingest/table-complete", payload)

    def abort_table(self, source: str, info: TableInfo, *, mode: SyncMode,
                    snapshot_id: str | None = None) -> None:
        if mode != "full_refresh" or not snapshot_id:
            return
        self.ensure_protocol()
        payload = self._base_payload(source, info, mode, snapshot_id)
        self._post_with_retry("/ingest/table-abort", payload)
