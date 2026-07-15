"""入口:python -m data2agent.ingest [--landing ...] [--host] [--port] [--token]

平台侧常驻,接收中间服务器推来的 raw 批次。内网部署建议配 D2A_INGEST_TOKEN。
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent 接收端(Pattern A 平台侧)")
    ap.add_argument("--landing", default="landing/factory.sqlite", help="落地库路径")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址(内网可信段)")
    ap.add_argument("--port", type=int, default=8850)
    ap.add_argument("--token", default=os.environ.get("D2A_INGEST_TOKEN", ""),
                    help="Bearer Token(默认取环境变量 D2A_INGEST_TOKEN;空 = 不认证)")
    args = ap.parse_args()

    import uvicorn

    from .app import create_app
    app = create_app(args.landing, args.token or None)
    print(f"ingest 接收端:http://{args.host}:{args.port}/ingest"
          f"({'Token 认证' if args.token else '⚠ 无认证,仅内网可信段'};落地 {args.landing})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
