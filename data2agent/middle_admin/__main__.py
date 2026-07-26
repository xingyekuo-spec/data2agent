"""入口:python -m data2agent.middle_admin [--home C:\\d2a] [--config connect.yaml]

推荐现场:仅传 --home,无配置时浏览器打开 /config 完成首次配置(无需 PowerShell 脚本)。
Token:--token / 环境变量 / home/config/secrets.env 中的 D2A_MIDDLE_ADMIN_TOKEN。
"""

from __future__ import annotations

import argparse
import sys

from ..admin_common.auth_token import resolve_token
from ..admin_common.home_layout import HomeLayout, default_home
from ..admin_common.secrets_file import apply_secrets_to_environ
from ..admin_common.windows_asyncio import patch_windows_socketpair


def _warn_if_insecure(host: str, token: str | None) -> None:
    loopback = {"127.0.0.1", "::1", "localhost"}
    if not token and host not in loopback:
        print(
            "警告:未配置 Token 且绑定非回环地址 —— "
            "请设置 D2A_MIDDLE_ADMIN_TOKEN 或 --token",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="data2agent 中间机管理界面")
    ap.add_argument("--home", default=None,
                    help=r"安装根目录(默认环境变量 D2A_HOME 或 C:\d2a);启用浏览器首次配置")
    ap.add_argument("--config", default=None, help="connect.yaml 路径(可与 --home 二选一)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8851)
    ap.add_argument("--token", default=None,
                    help="Bearer Token(默认取环境变量 / secrets.env)")
    ap.add_argument("--log-path", default=None, help="d2a-connector 日志路径")
    args = ap.parse_args(argv)

    home_path = args.home
    if home_path is None and args.config is None:
        home_path = str(default_home())

    home = HomeLayout.from_path(home_path) if home_path else None
    if home is not None:
        home.ensure_dirs()
        if home.secrets_env.is_file():
            apply_secrets_to_environ(home.secrets_env)

    token = resolve_token(args.token, "D2A_MIDDLE_ADMIN_TOKEN")
    _warn_if_insecure(args.host, token)

    log_path = args.log_path
    if log_path is None and home is not None:
        log_path = str(home.logs_dir / "d2a-connector.log")

    import uvicorn

    from .app import create_app
    app = create_app(
        config_path=args.config,
        token=token,
        log_path=log_path,
        home=home.root if home else None,
    )
    setup = "首次配置模式" if (home and not home.connect_yaml.is_file()) else (
        "Token 已启用" if token else "未启用认证")
    print(f"中间机管理:http://{args.host}:{args.port} ({setup})")
    patch_windows_socketpair()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
