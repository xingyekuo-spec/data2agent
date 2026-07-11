"""入口:python -m data2agent.mcp_server [--db 路径] [--transport stdio|http] …

stdio(默认):本机 Agent 场景,继承进程权限,无需认证;
http:内网部署,默认强制 Token(--token / D2A_MCP_TOKEN;--allow-anonymous
      仅限展厅演示)、每工具限流、JSONL 查询审计(与抽取侧审计对称)。
"""

from __future__ import annotations

import argparse
import os
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
    ap.add_argument("--token", default=os.environ.get("D2A_MCP_TOKEN", ""),
                    help="http 模式 Bearer Token(默认取环境变量 D2A_MCP_TOKEN)")
    ap.add_argument("--allow-anonymous", action="store_true",
                    help="http 模式免认证(仅限展厅演示;内网部署必须配 Token)")
    ap.add_argument("--rate-per-minute", type=int, default=120,
                    help="每工具限流(次/分钟,0 关闭;仅 http 模式生效)")
    ap.add_argument("--audit-log", default=None,
                    help="查询审计 JSONL 路径(http 模式默认写在落地库旁;'off' 关闭)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        ap.error(f"落地库不存在:{args.db}。展厅链路:python -m data2agent.showroom.seed && "
                 "python -m data2agent.connect sync --sqlite showroom/e10.sqlite && "
                 "python -m data2agent.connect apply")

    from .server import create_server

    if args.transport == "stdio":
        sink = None
        if args.audit_log and args.audit_log != "off":
            from .http import jsonl_audit_sink
            sink = jsonl_audit_sink(args.audit_log)
        create_server(args.db, args.templates, args.source, audit_sink=sink).run()
        return 0

    # http:默认安全 —— 无 Token 拒绝启动,除非显式声明匿名(展厅)
    if not args.token and not args.allow_anonymous:
        ap.error("http 模式必须配 Token(--token / D2A_MCP_TOKEN),"
                 "或显式 --allow-anonymous(仅限展厅演示)")
    audit_path = args.audit_log
    if audit_path is None:
        audit_path = str(Path(args.db).parent / "gateway_audit.jsonl")
    sink = None
    if audit_path != "off":
        from .http import jsonl_audit_sink
        sink = jsonl_audit_sink(audit_path)

    server = create_server(args.db, args.templates, args.source,
                           host=args.host, port=args.port,
                           rate_per_minute=args.rate_per_minute, audit_sink=sink)
    app = server.streamable_http_app()
    if args.token:
        from .http import BearerAuthMiddleware
        app = BearerAuthMiddleware(app, args.token)
    print(f"MCP(http):{args.host}:{args.port}"
          f"({'Token 认证' if args.token else '⚠ 匿名(仅限展厅)'};"
          f"限流 {args.rate_per_minute}/分钟;审计 {audit_path})")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
