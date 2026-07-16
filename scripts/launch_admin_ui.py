#!/usr/bin/env python3
"""Windows 管理界面启动器:拉起 middle_admin / console 并打开浏览器。

设计目标:
- 打成独立 exe(PyInstaller one-file),双击即可用;
- 不打包业务代码:调用已安装的 C:\\d2a\\venv 里的 Python 模块;
- 端口已被占用时视为服务已在跑,只打开浏览器。

用法:
  python scripts/launch_admin_ui.py --role middle
  python scripts/launch_admin_ui.py --role platform

PyInstaller(在 Windows 上):
  pyinstaller --onefile --noconsole --name d2a-middle-ui scripts/launch_admin_ui.py -- \\
    --role middle
  # 更好:用 --role 写进启动器包装,见 deploy/build_ui_launchers.ps1
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("D2A_HOME", r"C:\d2a"))


def _msg(title: str, text: str, error: bool = False) -> None:
    """弹窗;无 GUI 环境时退回 stderr。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if error:
            messagebox.showerror(title, text)
        else:
            messagebox.showinfo(title, text)
        root.destroy()
    except Exception:
        print(f"{title}: {text}", file=sys.stderr)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def _role_config(role: str, home: Path) -> dict:
    if role == "middle":
        return {
            "title": "data2agent 中间机管理",
            "port": int(os.environ.get("D2A_MIDDLE_ADMIN_PORT", "8851")),
            "module": "data2agent.middle_admin",
            "config": home / "config" / "connect.yaml",
            "log_path": home / "data" / "logs" / "d2a-connector.log",
            "token_env": "D2A_MIDDLE_ADMIN_TOKEN",
            "extra_args": lambda cfg, log: [
                "--config", str(cfg),
                "--host", "127.0.0.1",
                "--port", str(int(os.environ.get("D2A_MIDDLE_ADMIN_PORT", "8851"))),
                "--log-path", str(log),
            ],
        }
    if role == "platform":
        return {
            "title": "data2agent 平台管理",
            "port": int(os.environ.get("D2A_CONSOLE_PORT", "8849")),
            "module": "data2agent.console",
            "config": home / "config" / "platform.yaml",
            "log_path": home / "data" / "logs",
            "token_env": "D2A_CONSOLE_TOKEN",
            "extra_args": lambda cfg, log: [
                "--config", str(cfg),
                "--host", "127.0.0.1",
                "--port", str(int(os.environ.get("D2A_CONSOLE_PORT", "8849"))),
                "--log-dir", str(log),
            ],
        }
    raise SystemExit(f"unknown role: {role}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="data2agent admin UI launcher")
    ap.add_argument("--role", choices=("middle", "platform"), required=True)
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME,
                    help=r"安装根目录(默认 C:\d2a 或环境变量 D2A_HOME)")
    ap.add_argument("--no-browser", action="store_true",
                    help="只启动服务,不打开浏览器")
    args = ap.parse_args(argv)

    home: Path = args.home
    cfg = _role_config(args.role, home)
    title = cfg["title"]
    port = cfg["port"]
    host = "127.0.0.1"
    url = f"http://{host}:{port}/"

    venv_py = home / "venv" / "Scripts" / "python.exe"
    if not venv_py.is_file():
        # 开发机 / 非 Windows 回退
        alt = home / "venv" / "bin" / "python"
        venv_py = alt if alt.is_file() else Path(sys.executable)

    config_path: Path = cfg["config"]
    log_path: Path = cfg["log_path"]

    if not config_path.is_file():
        _msg(
            title,
            f"未找到配置文件:\n{config_path}\n\n"
            f"请先完成安装并运行 setup-{'middle' if args.role == 'middle' else 'platform'}.ps1 "
            f"生成配置后再打开本程序。",
            error=True,
        )
        return 2

    if not (home / "venv").exists() and venv_py == Path(sys.executable):
        # 仍可用当前解释器(开发冒烟),但提示
        pass
    elif not venv_py.is_file():
        _msg(
            title,
            f"未找到虚拟环境 Python:\n{home / 'venv'}\n\n"
            "请先按安装文档创建 C:\\d2a\\venv 并离线安装 data2agent。",
            error=True,
        )
        return 2

    already = _port_open(host, port)
    if not already:
        cmd = [str(venv_py), "-m", cfg["module"], *cfg["extra_args"](config_path, log_path)]
        # Token 从机器级环境变量继承;不在命令行回显
        creationflags = 0
        if sys.platform == "win32":
            # 无控制台窗口的后台子进程
            CREATE_NO_WINDOW = 0x08000000
            creationflags = CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(
                cmd,
                cwd=str(home / "app") if (home / "app").is_dir() else str(home),
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=(sys.platform != "win32"),
            )
        except OSError as e:
            _msg(title, f"无法启动管理界面:\n{e}", error=True)
            return 3

        if not _wait_port(host, port, timeout=25.0):
            _msg(
                title,
                f"服务启动超时(未监听 {host}:{port})。\n\n"
                f"请检查:\n"
                f"1. 已安装对应 extras(middle_admin 或 console)\n"
                f"2. 环境变量 {cfg['token_env']} 是否已设置\n"
                f"3. 配置文件是否合法: {config_path}",
                error=True,
            )
            return 4

    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
