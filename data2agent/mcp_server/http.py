"""HTTP 部署的安全件:Bearer 认证中间件、每工具限流、JSONL 审计。

设计 03 §5:stdio(本机进程)不需要这些;暴露 HTTP 即三件齐上 ——
认证默认强制(拒绝无 Token 启动,除非显式 --allow-anonymous),
限流防误用打穿落地库,审计与抽取侧 d2a_audit_log 对称。
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable


class BearerAuthMiddleware:
    """纯 ASGI 中间件:HTTP 请求须携带 Authorization: Bearer <token>。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan / websocket 透传
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        supplied = supplied.removeprefix("Bearer ").strip()
        if supplied != self.token:
            body = json.dumps(
                {"error": "unauthorized",
                 "detail": "需要有效 Token(Authorization: Bearer <token>)"},
                ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


class RateLimiter:
    """每工具滑动窗口限流(次/分钟;0 = 关闭)。"""

    def __init__(self, per_minute: int):
        self.per_minute = max(0, int(per_minute))
        self._calls: dict[str, deque] = defaultdict(deque)

    def check(self, tool: str) -> None:
        if not self.per_minute:
            return
        now = time.monotonic()
        q = self._calls[tool]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            raise ValueError(
                f"工具 '{tool}' 触发限流({self.per_minute} 次/分钟),请稍后重试")
        q.append(now)


def jsonl_audit_sink(path: str | Path) -> Callable[[dict], None]:
    """追加式 JSONL 审计(网关每次工具调用一条,可 grep / 可回放)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(record: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    return write
