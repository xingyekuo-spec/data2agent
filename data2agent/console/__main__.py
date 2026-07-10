"""入口:python -m data2agent.console [--config connect.yaml] [--landing ...] [--port 8849]

带 --config 为完整模式(动作可用);不带则纯只读。
Token 认证:--token 或环境变量 D2A_CONSOLE_TOKEN,内网部署建议启用。
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent 运维控制台")
    ap.add_argument("--config", help="connect.yaml 路径(加载后启用动作,并以其 landing/templates 为准)")
    ap.add_argument("--landing", default="landing/factory.sqlite", help="落地库路径(只读模式用)")
    ap.add_argument("--templates", default="templates", help="模板包目录(只读模式用)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8849)
    ap.add_argument("--token", default=os.environ.get("D2A_CONSOLE_TOKEN", ""),
                    help="控制台 Token(默认取环境变量 D2A_CONSOLE_TOKEN;空 = 不认证)")
    args = ap.parse_args()

    config = None
    if args.config:
        from ..connect.config import load_config
        config = load_config(args.config)

    import uvicorn

    from .app import create_app
    app = create_app(args.landing, args.templates, config, token=args.token or None)
    print(f"运维控制台:http://{args.host}:{args.port}"
          f"({'完整模式' if config else '只读模式'};"
          f"{'Token 认证已启用' if args.token else '未启用认证,内网部署建议配 D2A_CONSOLE_TOKEN'})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
