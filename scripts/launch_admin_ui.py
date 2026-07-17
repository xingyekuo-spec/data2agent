#!/usr/bin/env python3
"""便携包唯一入口:双击 data2agent.exe。

- 单实例:已在运行则只打开管理界面浏览器,不重复启动
- 托盘常驻:可「打开管理界面」或「退出」(停止本入口拉起的进程)
- 首次/日常:启动管理界面;已配置时顺带拉起业务进程
"""

from __future__ import annotations

import argparse
import atexit
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("D2A_HOME", r"C:\d2a"))

# Populated while this process owns the instance; stopped on tray Quit.
_CHILDREN: list[subprocess.Popen] = []
_MUTEX_HANDLE = None


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
            "title": "data2agent 中间机",
            "mutex": "Local\\data2agent-middle-launcher",
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
            "title": "data2agent 平台",
            "mutex": "Local\\data2agent-platform-launcher",
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


def _spawn(cmd: list[str], *, home: Path, env: dict[str, str],
           log_name: str | None = None) -> subprocess.Popen:
    # 子进程输出落到 data/logs/<name>.log,现场可诊断(此前吞进 DEVNULL,
    # 子进程崩溃无从排查 —— 便携包首个版本因此隐藏了两个致命 bug)。
    out: int | object = subprocess.DEVNULL
    if log_name:
        logs = home / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out = open(logs / f"{log_name}.log", "a", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        cmd,
        cwd=str(home),
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT if log_name else subprocess.DEVNULL,
        creationflags=_creationflags(),
        start_new_session=(sys.platform != "win32"),
    )
    _CHILDREN.append(proc)
    return proc


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
            creationflags=_creationflags(),
        )
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def stop_children() -> None:
    for proc in list(_CHILDREN):
        _kill_process_tree(proc)
    _CHILDREN.clear()


def acquire_single_instance(mutex_name: str) -> bool:
    """Return True if this process is the primary instance; False if another holds it."""
    global _MUTEX_HANDLE
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            return True
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True

    # Non-Windows: lock file under temp / home
    lock_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"{mutex_name.replace(chr(92), '_')}.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return True
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _MUTEX_HANDLE = fd
    return True


def release_single_instance() -> None:
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE is None:
        return
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.CloseHandle(_MUTEX_HANDLE)  # type: ignore[attr-defined]
    else:
        try:
            import fcntl

            fcntl.flock(_MUTEX_HANDLE, fcntl.LOCK_UN)
            os.close(_MUTEX_HANDLE)
        except Exception:
            pass
    _MUTEX_HANDLE = None


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
        # apply 是纯落地库操作:不接受 --config,须显式给 --templates(cwd 无 templates)
        ("apply", None, [py, "-m", "data2agent.connect", "apply",
                         "--landing", landing, "--templates", templates,
                         "--every", "1800"]),
        ("mcp", 8848, [py, "-m", "data2agent.mcp_server",
                       "--db", landing, "--templates", templates,
                       "--transport", "http", "--host", "0.0.0.0", "--port", "8848"]),
    ]


def landing_db_path(role: str, home: Path) -> Path | None:
    """平台侧落地库路径(mcp/apply 的前置依赖)。"""
    if role != "platform":
        return None
    return home / "data" / "factory.sqlite"


def ensure_landing_db(python: Path, landing: Path, *, home: Path,
                      env: dict[str, str]) -> None:
    """预建落地库:首次上线时尚无推送,ingest 惰性建库,但 mcp 启动即要求库存在。

    先建好空库(基础表),mcp/apply 才能在首个批次到达前正常起来。
    """
    if landing.is_file():
        return
    landing.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(python), "-c",
         "import sys; from data2agent.connect.landing import LandingStore; "
         "LandingStore(sys.argv[1])", str(landing)],
        cwd=str(home), env=env, capture_output=True, check=False,
        creationflags=_creationflags(),
    )


def admin_url(host: str, port: int, configured: bool) -> str:
    return f"http://{host}:{port}/" + ("" if configured else "config")


def open_admin(url: str) -> None:
    webbrowser.open(url)


def _tray_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=(18, 49, 79, 255))
    draw.rectangle((20, 28, 44, 48), fill=(232, 244, 253, 255))
    return img


def run_tray(*, title: str, url: str) -> int:
    """Block on system tray until user quits. Returns process exit code."""
    try:
        import pystray
        from pystray import Menu, MenuItem
    except ImportError:
        _msg(
            title,
            "缺少托盘组件(pystray)。请使用 Release 便携包中的 data2agent.exe。",
            error=True,
        )
        # Keep children running; do not exit them if tray unavailable.
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            stop_children()
            return 0

    def on_open(icon, item):  # noqa: ARG001
        open_admin(url)

    def on_quit(icon, item):  # noqa: ARG001
        stop_children()
        release_single_instance()
        icon.stop()

    menu = Menu(
        MenuItem("打开管理界面", on_open, default=True),
        MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("data2agent", _tray_image(), title, menu)

    def _open_soon():
        time.sleep(0.4)
        open_admin(url)

    if not getattr(run_tray, "_skip_auto_open", False):
        threading.Thread(target=_open_soon, daemon=True).start()

    icon.run()
    stop_children()
    release_single_instance()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="data2agent portable launcher")
    ap.add_argument("--role", choices=("middle", "platform"), required=True)
    ap.add_argument("--home", type=Path, default=None,
                    help=r"便携根目录(默认:exe 所在目录或 D2A_HOME)")
    ap.add_argument("--no-browser", action="store_true",
                    help="启动时不自动打开浏览器(托盘仍可打开)")
    ap.add_argument("--no-workers", action="store_true",
                    help="只开管理界面,不拉起 connector/ingest 等")
    ap.add_argument("--no-tray", action="store_true",
                    help="不进入托盘(测试/无 GUI);启动后立即返回")
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
    url = admin_url(host, port, configured)

    # Secondary instance: just open the already-running admin UI.
    if not acquire_single_instance(cfg["mutex"]):
        if _wait_port(host, port, timeout=20.0):
            if not args.no_browser:
                open_admin(url)
            return 0
        _msg(title, "检测到程序已在运行,但管理界面尚未就绪,请稍后再双击打开。", error=True)
        return 1

    atexit.register(release_single_instance)
    atexit.register(stop_children)

    venv_py = _resolve_python(home)
    if venv_py is None or not venv_py.is_file():
        release_single_instance()
        _msg(
            title,
            f"未找到运行环境。\n\n请确认解压完整,目录中应有:\n  {home}\\runtime\\python.exe",
            error=True,
        )
        return 2

    env = os.environ.copy()
    env["D2A_HOME"] = str(home)
    env = _merge_secrets_env(home, env)

    admin_already_up = _port_open(host, port)
    if not admin_already_up:
        try:
            _spawn([str(venv_py), "-m", cfg["module"], *cfg["extra_args"]],
                   home=home, env=env, log_name="admin")
        except OSError as e:
            release_single_instance()
            _msg(title, f"无法启动:\n{e}", error=True)
            return 3
        if not _wait_port(host, port, timeout=25.0):
            stop_children()
            release_single_instance()
            _msg(title, f"启动超时(未监听 {host}:{port})。\nHome: {home}", error=True)
            return 4

    # Only start workers when we ourselves brought admin up. If admin was
    # already listening (e.g. tray crashed), avoid duplicate connector/etc.
    if not args.no_workers and configured and not admin_already_up:
        landing = landing_db_path(args.role, home)
        if landing is not None:
            ensure_landing_db(venv_py, landing, home=home, env=env)
        for name, listen, cmd in worker_commands(args.role, home, venv_py):
            if listen is not None and _port_open("127.0.0.1", listen):
                continue
            try:
                _spawn(cmd, home=home, env=env, log_name=name)
            except OSError:
                _msg(title, f"无法启动 {name}", error=True)

    if args.no_tray:
        if not args.no_browser:
            open_admin(url)
        # Test mode: leave children; caller/tests do not need tray.
        # Detach atexit kill so short-lived test process does not kill servers
        # when using --no-tray for one-shot open only... Actually tests use
        # missing python path. For --no-tray we should still stop children on
        # exit of launcher unless we want fire-and-forget.
        # Old behavior was fire-and-forget. With tray, primary owns lifecycle.
        # --no-tray for tests: fire-and-forget (clear atexit stop).
        try:
            atexit.unregister(stop_children)
        except Exception:
            pass
        _CHILDREN.clear()  # do not kill on exit in no-tray mode
        release_single_instance()
        return 0

    run_tray._skip_auto_open = bool(args.no_browser)  # type: ignore[attr-defined]
    return run_tray(title=title, url=url)


if __name__ == "__main__":
    raise SystemExit(main())
