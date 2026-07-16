"""入口:python -m data2agent.middle_admin --config connect.yaml [--port 8851]

Token 认证:--token 或环境变量 D2A_MIDDLE_ADMIN_TOKEN;内网部署建议启用。
"""

from __future__ import annotations

import argparse
import sys

from ..admin_common.auth_token import resolve_token


def _warn_if_insecure(host: str, token: str | None) -> None:
    loopback = {"127.0.0.1", "::1", "localhost"}
    if not token and host not in loopback:
        print(
            "警告:未配置 Token 且绑定非回环地址 —— "
            "请设置 D2A_MIDDLE_ADMIN_TOKEN 或 --token",
            file=sys.stderr,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent 中间机管理界面")
    ap.add_argument("--config", required=True, help="connect.yaml 路径")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8851)
    ap.add_argument("--token", default=None,
                    help="Bearer Token(默认取环境变量 D2A_MIDDLE_ADMIN_TOKEN)")
    ap.add_argument("--log-path", default=r"C:\d2a\data\logs\d2a-connector.log",
                    help="d2a-connector 日志路径(Task 4 日志 API 使用)")
    args = ap.parse_args()

    token = resolve_token(args.token, "D2A_MIDDLE_ADMIN_TOKEN")
    _warn_if_insecure(args.host, token)

    import uvicorn

    from .app import create_app
    app = create_app(args.config, token=token, log_path=args.log_path)
    print(f"中间机管理:http://{args.host}:{args.port}"
          f"({'Token 已启用' if token else '未启用认证'})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
