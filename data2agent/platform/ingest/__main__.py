"""入口:python -m data2agent.platform.ingest [--landing ...] [--host] [--port] [--token]

平台侧常驻,接收中间服务器推来的 raw 批次。非本机监听强制 Bearer Token。
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    from ...shared.admin.secrets_file import load_home_secrets_if_present
    load_home_secrets_if_present()

    ap = argparse.ArgumentParser(description="data2agent 接收端(Pattern A 平台侧)")
    ap.add_argument("--landing", default="landing/factory.sqlite", help="落地库路径")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址(内网可信段)")
    ap.add_argument("--port", type=int, default=8850)
    ap.add_argument("--token", default=None,
                    help="Bearer Token(默认取环境变量 D2A_INGEST_TOKEN;空 = 不认证)")
    ap.add_argument(
        "--allow-unauthenticated", action="store_true",
        help="显式允许非本机监听不鉴权（仅开发环境，生产禁止）")
    args = ap.parse_args()
    token = args.token if args.token is not None else os.environ.get("D2A_INGEST_TOKEN", "")
    if (
        args.host not in ("127.0.0.1", "::1", "localhost")
        and not token
        and not args.allow_unauthenticated
    ):
        ap.error(
            "非本机监听必须设置 --token/D2A_INGEST_TOKEN；"
            "仅开发环境可显式使用 --allow-unauthenticated")

    import uvicorn

    from .app import create_app
    app = create_app(args.landing, token or None)
    print(f"ingest 接收端:http://{args.host}:{args.port}/ingest"
          f"({'Token 认证' if token else '⚠ 无认证,仅内网可信段'};落地 {args.landing})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
