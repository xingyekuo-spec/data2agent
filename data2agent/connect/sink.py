"""落地出口抽象(§12.3):抽取的 raw 批次去哪。

- LocalSink:写本地落地库 —— 同机 / 开发,现行为不变;
- HttpPushSink:POST 给数据平台接收端点 —— Pattern A 里中间服务器用,只出站、
  本地不落 raw(仅瞬态)。

connect 的抽取 / 增量 / 水位逻辑只认 Sink,不关心 raw 落在本地还是推给平台;
水位状态仍由 incremental_sync 的本地 state(中间服务器上,仅元数据不含 raw)持有。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

from .adapters.base import TableInfo
from .landing import LandingStore, normalize_value


class Sink(Protocol):
    def ensure_table(self, source: str, info: TableInfo) -> None: ...
    def write(self, source: str, info: TableInfo, rows: list[dict], batch_id: str) -> int: ...
    def complete_table(self, source: str, info: TableInfo, completion_id: str,
                       rows: int, batches: int) -> None: ...


class LocalSink:
    """写本地落地库(现行为)。"""

    def __init__(self, landing: LandingStore):
        self.landing = landing

    def ensure_table(self, source: str, info: TableInfo) -> None:
        self.landing.ensure_raw_table(source, info)

    def write(self, source: str, info: TableInfo, rows: list[dict], batch_id: str) -> int:
        return self.landing.upsert_rows(source, info, rows, batch_id)

    def complete_table(self, source: str, info: TableInfo, completion_id: str,
                       rows: int, batches: int) -> None:
        # 本地模式的 d2a_run_step(table) 由 incremental_sync 写入，已是完成证据。
        return None


def _urllib_post(url: str, payload: dict, token: str | None, timeout: float) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


class HttpPushSink:
    """把 raw 批次 POST 给平台接收端点(中间服务器用,stdlib urllib,零额外依赖)。

    值在推送前归一化(datetime / Decimal → 可移植),平台端 upsert 幂等、
    重推安全;失败按指数退避重试。post 可注入(测试用)。
    """

    def __init__(self, url: str, token: str | None = None, *,
                 timeout: float = 30.0, retries: int = 3,
                 post: Callable[[str, dict, str | None, float], None] | None = None):
        if not url:
            raise ValueError("HttpPushSink 需要平台接收端点 url")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = max(1, retries)
        self._post = post or _urllib_post

    def ensure_table(self, source: str, info: TableInfo) -> None:
        pass  # 平台在收到首个批次时 ensure_raw_table(幂等 CREATE IF NOT EXISTS)

    def write(self, source: str, info: TableInfo, rows: list[dict], batch_id: str) -> int:
        payload = {
            "source": source,
            "table": info.name,
            "columns": [[c, t] for c, t in info.columns],
            "pk": list(info.pk),
            "batch_id": batch_id,
            "rows": [{c: normalize_value(r.get(c)) for c, _ in info.columns} for r in rows],
        }
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self._post(f"{self.url}/ingest/batch", payload, self.token, self.timeout)
                return len(rows)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"推送批次失败(重试 {self.retries} 次):{last}")

    def complete_table(self, source: str, info: TableInfo, completion_id: str,
                       rows: int, batches: int) -> None:
        """全部批次确认后声明一张表完成；零行表也必须发送此事件。"""
        payload = {
            "source": source,
            "table": info.name,
            "columns": [[c, t] for c, t in info.columns],
            "pk": list(info.pk),
            "completion_id": completion_id,
            "rows": rows,
            "batches": batches,
        }
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self._post(f"{self.url}/ingest/table-complete", payload, self.token, self.timeout)
                return
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"推送表完成事件失败(重试 {self.retries} 次):{last}")
