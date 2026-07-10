"""入口:python -m data2agent.mcp_server [--db 路径] [--templates 目录] [--source 源]"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent MCP Server(lite,只读 stdio)")
    ap.add_argument("--db", default="landing/factory.sqlite",
                    help="落地库路径(完整管道:seed → connect sync → connect apply)")
    ap.add_argument("--templates", default="templates", help="模板包目录")
    ap.add_argument("--source", default="digiwin_e10", help="binding 数据源名")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                    help="stdio(本机 Agent)/ http(streamable-http,内网 / 展厅 compose)")
    ap.add_argument("--host", default="127.0.0.1", help="http 模式监听地址")
    ap.add_argument("--port", type=int, default=8848, help="http 模式端口")
    args = ap.parse_args()

    if not Path(args.db).exists():
        ap.error(f"落地库不存在:{args.db}。展厅链路:python -m data2agent.showroom.seed && "
                 "python -m data2agent.connect sync --sqlite showroom/e10.sqlite && "
                 "python -m data2agent.connect apply")

    from .server import create_server
    server = create_server(args.db, args.templates, args.source,
                           host=args.host, port=args.port)
    server.run(transport="streamable-http" if args.transport == "http" else "stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
