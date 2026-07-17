"""入口:python -m data2agent.console [--home C:\\d2a] [--config platform.yaml]

推荐现场:仅传 --home,无配置时浏览器打开 /config 完成首次配置(无需 PowerShell 脚本)。
Token:--token / 环境变量 / home/config/secrets.env 中的 D2A_CONSOLE_TOKEN。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..admin_common.auth_token import resolve_token
from ..admin_common.home_layout import HomeLayout, default_home
from ..admin_common.secrets_file import apply_secrets_to_environ


def _default_log_dir(landing: str, config_path: str | None) -> Path:
    if env := os.environ.get("D2A_LOG_DIR"):
        return Path(env)
    if config_path:
        from ..connect.config import load_config
        return Path(load_config(config_path).landing).parent / "logs"
    return Path(landing).parent / "logs"


def _warn_if_insecure(host: str, token: str | None) -> None:
    loopback = {"127.0.0.1", "::1", "localhost"}
    if not token and host not in loopback:
        print(
            "警告:未配置 Token 且绑定非回环地址 —— "
            "请设置 D2A_CONSOLE_TOKEN 或 --token",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="data2agent 运维控制台")
    ap.add_argument("--home", default=None,
                    help=r"安装根目录(默认环境变量 D2A_HOME 或 C:\d2a);启用浏览器首次配置")
    ap.add_argument("--config",
                    help="platform.yaml / connect.yaml 路径(加载后启用动作)")
    ap.add_argument("--landing", default=None, help="落地库路径(只读模式用)")
    ap.add_argument("--templates", default="templates", help="模板包目录(只读模式用)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8849)
    ap.add_argument("--log-dir", default=None,
                    help="日志目录(默认 landing 同级 logs;文档部署常用 C:\\d2a\\data\\logs)")
    ap.add_argument("--token", default=None,
                    help="控制台 Token(默认取环境变量 / secrets.env)")
    args = ap.parse_args(argv)

    home_path = args.home
    if home_path is None and args.config is None and args.landing is None:
        home_path = str(default_home())

    home = HomeLayout.from_path(home_path) if home_path else None
    if home is not None:
        home.ensure_dirs()
        if home.secrets_env.is_file():
            apply_secrets_to_environ(home.secrets_env)

    token = resolve_token(args.token, "D2A_CONSOLE_TOKEN")
    _warn_if_insecure(args.host, token)

    config = None
    config_path = args.config
    if config_path:
        from ..connect.config import load_config
        config = load_config(config_path)
    elif home is not None and home.platform_yaml.is_file():
        from ..connect.config import load_config
        config_path = str(home.platform_yaml)
        config = load_config(config_path)

    landing = args.landing
    if landing is None and config is not None:
        landing = config.landing
    if landing is None and home is None:
        landing = "landing/factory.sqlite"

    log_dir = Path(args.log_dir) if args.log_dir else None
    if log_dir is None and home is not None:
        log_dir = home.logs_dir
    elif log_dir is None and landing:
        log_dir = _default_log_dir(landing, config_path)

    import uvicorn

    from .app import create_app
    app = create_app(
        landing, args.templates, config, token=token,
        config_path=config_path, log_dir=log_dir,
        home=home.root if home else None,
    )
    mode = "首次配置模式" if (home and not home.platform_yaml.is_file()) else (
        "完整模式" if config else "只读模式")
    print(f"运维控制台:http://{args.host}:{args.port}"
          f"({mode};"
          f"{'Token 认证已启用' if token else '未启用认证,内网部署建议配 D2A_CONSOLE_TOKEN'})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
