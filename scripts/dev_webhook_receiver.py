#!/usr/bin/env python3
"""本机开发用 d2a-push v1 接收端:把 webhook 推送收进内存并打印摘要。

仅用于联调 / 演示,不做持久化。接收逻辑复用 tests/support/webhook_receiver.py
(签名校验、幂等去重、增量 upsert、全量快照 complete 时替换)。

用法(仓库根目录):
    python scripts/dev_webhook_receiver.py --port 8860 [--token T] [--secret S]
    python -m data2agent.middle.extract sync --config local-test/config/connect-push.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.webhook_receiver import WebhookReceiver  # noqa: E402


def make_handler(receiver: WebhookReceiver, path: str):
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server 约定
            if self.path != path:
                self._reply(404, {"detail": f"POST {path}"})
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {k: v for k, v in self.headers.items()}
            try:
                with lock:
                    _, reply = receiver.send(path, body, headers, 0)
                    # 打印本次收到的事件(重复投递时 receiver.events 不追加)。
                    event = json.loads(body)
                    kind = event.get("type", "").rsplit(".v1.", 1)[-1]
                    table = event.get("subject") or "*"
                    dup = " (duplicate)" if b"duplicate" in reply else ""
                    print(f"{kind:<15} {table:<16} id={event.get('id')}{dup}", flush=True)
                    if kind == "sync.complete":
                        summary = {t: len(rows) for t, rows in receiver.tables.items()}
                        print(f"  当前各表行数: {summary}", flush=True)
            except urllib.error.HTTPError as e:
                self._reply(e.code, {"detail": e.reason})
                return
            self._reply(200, {"accepted": True})

        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="本机 d2a-push v1 webhook 接收端(开发用)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8860)
    ap.add_argument("--path", default="/events")
    ap.add_argument("--token", default=None, help="要求 Authorization: Bearer <token>")
    ap.add_argument("--secret", default=None, help="要求 HMAC 签名(X-D2A-Signature)")
    args = ap.parse_args()
    receiver = WebhookReceiver(token=args.token, secret=args.secret)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(receiver, args.path))
    print(f"webhook 接收端:http://{args.host}:{args.port}{args.path}(Ctrl+C 退出)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
