#!/usr/bin/env python3
"""便携包唯一入口:双击 data2agent.exe。

- 启动管理界面并打开浏览器(无配置时进首次配置页)
- 若已完成配置:顺带拉起主业务进程(中间机 connector / 平台 ingest+apply+mcp)
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


def detect_portable_root(start: Path | None = None) -> Path | None:
    """Return folder that contains runtime/python.exe (portable layout)."""
    if start is None:
        if getattr(sys, "frozen", False):
            start = Path(sys.executable).resolve().parent
        else:
            start = Path.cwd()
    start = start.resolve()
    for cand in (start, *start.parents):
        if (cand / "runtime" / "python.exe").is_file():
            return cand
        if (cand / "runtime" / "bin" / "python").is_file():
            return cand
    return None


def _role_config(role: str, home: Path) -> dict:
    if role == "middle":
        port = int(os.environ.get("D2A_MIDDLE_ADMIN_PORT", "8851"))
        return {
            "title": "data2agent",
            "port": port,
            "module": "data2agent.middle_admin",
            "config_file": home / "config" / "connect.yaml",
            "extra_args": [
                "--home", str(home),
                "--host", "127.0.0.1",
                "--port", str(port),
            ],
        }
    if role == "platform":
        port = int(os.environ.get("D2A_CONSOLE_PORT", "8849"))
        return {
            "title": "data2agent",
            "port": port,
            "module": "data2agent.console",
            "config_file": home / "config" / "platform.yaml",
            "extra_args": [
                "--home", str(home),
                "--host", "127.0.0.1",
                "--port", str(port),
            ],
        }
    raise SystemExit(f"unknown role: {role}")


def _resolve_python(home: Path) -> Path | None:
    for cand in (
        home / "runtime" / "python.exe",
        home / "runtime" / "bin" / "python",
        home / "venv" / "Scripts" / "python.exe",
        home / "venv" / "bin" / "python",
    ):
        if cand.is_file():
            return cand
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).resolve().parent
        for cand in (
            here / "runtime" / "python.exe",
            here / "venv" / "Scripts" / "python.exe",
        ):
            if cand.is_file():
                return cand
    return Path(sys.executable) if Path(sys.executable).is_file() else None


def _merge_secrets_env(home: Path, env: dict[str, str]) -> dict[str, str]:
    secrets = home / "config" / "secrets.env"
    if not secrets.is_file():
        return env
    for line in secrets.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip("\"'")
    return env


def _creationflags() -> int:
    if sys.platform != "win32":
        return 0
    CREATE_NO_WINDOW = 0x08000000
    return CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _spawn(cmd: list[str], *, home: Path, env: dict[str, str]) -> None:
    subprocess.Popen(
        cmd,
        cwd=str(home),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creationflags(),
        start_new_session=(sys.platform != "win32"),
    )


def worker_commands(role: str, home: Path, python: Path) -> list[tuple[str, int | None, list[str]]]:
    """Return (name, listen_port_or_None, argv) for configured workers."""
    py = str(python)
    if role == "middle":
        cfg = home / "config" / "connect.yaml"
        if not cfg.is_file():
            return []
        return [("connector", None, [py, "-m", "data2agent.connect", "serve",
                                     "--config", str(cfg)])]

    cfg = home / "config" / "platform.yaml"
    if not cfg.is_file():
        return []
    landing = str(home / "data" / "factory.sqlite")
    templates = str(home / "app" / "templates")
    return [
        ("ingest", 8850, [py, "-m", "data2agent.ingest",
                          "--landing", landing, "--host", "0.0.0.0", "--port", "8850"]),
        ("apply", None, [py, "-m", "data2agent.connect", "apply",
                         "--config", str(cfg), "--landing", landing, "--every", "1800"]),
        ("mcp", 8848, [py, "-m", "data2agent.mcp_server",
                       "--db", landing, "--templates", templates,
                       "--transport", "http", "--host", "0.0.0.0", "--port", "8848"]),
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="data2agent portable launcher")
    ap.add_argument("--role", choices=("middle", "platform"), required=True)
    ap.add_argument("--home", type=Path, default=None,
                    help=r"便携根目录(默认:exe 所在目录或 D2A_HOME)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-workers", action="store_true",
                    help="只开管理界面,不拉起 connector/ingest 等")
    args = ap.parse_args(argv)

    if args.home is not None:
        home = args.home
    else:
        portable = detect_portable_root()
        home = portable if portable is not None else DEFAULT_HOME

    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "data" / "logs").mkdir(parents=True, exist_ok=True)

    cfg = _role_config(args.role, home)
    title = cfg["title"]
    port = cfg["port"]
    host = "127.0.0.1"
    configured = Path(cfg["config_file"]).is_file()
    url = f"http://{host}:{port}/" + ("" if configured else "config")

    venv_py = _resolve_python(home)
    if venv_py is None or not venv_py.is_file():
        _msg(
            title,
            f"未找到运行环境。\n\n请确认解压完整,目录中应有:\n  {home}\\runtime\\python.exe",
            error=True,
        )
        return 2

    env = os.environ.copy()
    env["D2A_HOME"] = str(home)
    env = _merge_secrets_env(home, env)

    already = _port_open(host, port)
    if not already:
        try:
            _spawn([str(venv_py), "-m", cfg["module"], *cfg["extra_args"]],
                   home=home, env=env)
        except OSError as e:
            _msg(title, f"无法启动:\n{e}", error=True)
            return 3
        if not _wait_port(host, port, timeout=25.0):
            _msg(title, f"启动超时(未监听 {host}:{port})。\nHome: {home}", error=True)
            return 4

    if not args.no_workers and configured:
        for name, listen, cmd in worker_commands(args.role, home, venv_py):
            if listen is not None and _port_open("127.0.0.1", listen):
                continue
            try:
                _spawn(cmd, home=home, env=env)
            except OSError:
                _msg(title, f"无法启动 {name}", error=True)

    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
