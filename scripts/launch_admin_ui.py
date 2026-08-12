#!/usr/bin/env python3
"""便携包唯一入口:双击 data2agent.exe。

- 单实例:已在运行则只打开管理界面浏览器,不重复启动
- 托盘常驻:可「打开管理界面」或「退出」(停止本入口拉起的进程)
- 首次/日常:启动管理界面;已配置时顺带拉起业务进程
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("D2A_HOME", r"C:\d2a"))

# Populated while this process owns the instance; stopped on tray Quit.
_CHILDREN: list[subprocess.Popen] = []
_MUTEX_HANDLE = None

# Supervised processes (admin + workers): respawned on crash by the monitor
# thread, with a circuit breaker so a crash-looping worker stops hammering.
_MANAGED: list[dict] = []
_SUPERVISE_STOP = threading.Event()
_STARTUP_MODE = "unknown"
_AUTOSTART_LAST_CHECK_EPOCH = 0.0
SUPERVISE_INTERVAL = 5.0
SUPERVISE_MAX_RESTARTS = 5   # within SUPERVISE_WINDOW before giving up
SUPERVISE_WINDOW = 60.0
SUPERVISE_COOLDOWN = 900.0
ADMIN_STARTUP_TIMEOUT = 180.0
LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def _msg(title: str, text: str, error: bool = False) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            MB_OK = 0x00000000
            MB_ICONERROR = 0x00000010
            MB_ICONINFORMATION = 0x00000040
            MB_TOPMOST = 0x00040000
            flags = MB_OK | MB_TOPMOST | (MB_ICONERROR if error else MB_ICONINFORMATION)
            ctypes.windll.user32.MessageBoxW(None, text, title, flags)  # type: ignore[attr-defined]
            return
        except Exception:
            pass
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


def _notify(icon: object, title: str, text: str) -> bool:
    try:
        notify = getattr(icon, "notify")
    except Exception:
        return False
    try:
        notify(text, title)
        return True
    except Exception:
        return False


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


def _wait_admin_ready(
    host: str,
    port: int,
    proc: subprocess.Popen,
    *,
    timeout: float,
    home: Path,
) -> bool:
    start = time.time()
    deadline = start + timeout
    next_log = start
    while time.time() < deadline:
        if _port_open(host, port):
            _supervisor_log(
                home,
                f"admin ready port={host}:{port} elapsed={time.time() - start:.1f}s",
            )
            return True
        exit_code = proc.poll()
        if exit_code is not None:
            _supervisor_log(
                home,
                f"admin exited before port ready exit_code={exit_code} "
                f"elapsed={time.time() - start:.1f}s",
            )
            return False
        now = time.time()
        if now >= next_log:
            pid = getattr(proc, "pid", "?")
            _supervisor_log(
                home,
                f"waiting admin port={host}:{port} pid={pid} "
                f"elapsed={now - start:.1f}s timeout={timeout:g}s",
            )
            next_log = now + 5.0
        time.sleep(0.25)
    _supervisor_log(
        home,
        f"admin startup timeout port={host}:{port} elapsed={time.time() - start:.1f}s",
    )
    return False


def _admin_startup_timeout() -> float:
    raw = os.environ.get("D2A_ADMIN_STARTUP_TIMEOUT", "").strip()
    if not raw:
        return ADMIN_STARTUP_TIMEOUT
    try:
        return max(1.0, float(raw))
    except ValueError:
        return ADMIN_STARTUP_TIMEOUT


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


def portable_vue_dist_env(home: Path, env: dict | None = None) -> dict:
    """仅在未显式设置 D2A_VUE_DIST 时注入便携默认路径。"""
    current = env or {}
    if (current.get("D2A_VUE_DIST") or "").strip():
        return {}
    dist = Path(home) / "app" / "console-ui" / "dist"
    if (dist / "index.html").is_file():
        return {"D2A_VUE_DIST": str(dist.resolve())}
    return {}


def _role_config(role: str, home: Path) -> dict:
    if role == "middle":
        port = int(os.environ.get("D2A_MIDDLE_ADMIN_PORT", "8851"))
        return {
            "title": "data2agent 中间机",
            # Global 防止开机任务(Session 0)与登录用户双击各启一套。
            "mutex": "Global\\data2agent-middle-launcher",
            "port": port,
            "module": "data2agent.middle.admin",
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
            "mutex": "Global\\data2agent-platform-launcher",
            "port": port,
            "module": "data2agent.platform.console",
            "config_file": home / "config" / "platform.yaml",
            "extra_args": [
                "--home", str(home),
                "--host", "127.0.0.1",
                "--port", str(port),
            ],
        }
    raise SystemExit(f"unknown role: {role}")


def _display_cmd(cmd: list[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in cmd])


def _admin_startup_failure_message(
    *,
    reason: str,
    host: str,
    port: int,
    home: Path,
    log_name: str,
    cmd: list[str],
) -> str:
    logs = home / "data" / "logs"
    return (
        f"{reason}(未监听 {host}:{port})。\n"
        f"Home: {home}\n\n"
        "请查看日志:\n"
        f"  {logs / (log_name + '.log')}\n"
        f"  {logs / 'd2a-launcher.log'}\n\n"
        "也可在命令行手动启动以查看完整报错:\n"
        f"  {_display_cmd(cmd)}"
    )


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
        raw = v.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = raw[1:-1]
        elif len(raw) >= 2 and raw[0] == raw[-1] == "'":
            raw = raw[1:-1]
        else:
            raw = raw.replace("\\n", "\n").replace("\\\\", "\\")
        env[k.strip()] = raw
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
    log_path: Path | None = None
    if log_name:
        logs = home / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / f"{log_name}.log"
        out = subprocess.PIPE
    proc = subprocess.Popen(
        cmd,
        cwd=str(home),
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT if log_name else subprocess.DEVNULL,
        creationflags=_creationflags(),
        start_new_session=(sys.platform != "win32"),
    )
    if log_path is not None and proc.stdout is not None:
        threading.Thread(
            target=_pump_process_log,
            args=(proc.stdout, log_path),
            name=f"d2a-log-{log_name}", daemon=True,
        ).start()
    _CHILDREN.append(proc)
    return proc


def _rotate_log(path: Path) -> None:
    """轮转 path -> path.1，最多保留 LOG_BACKUP_COUNT 份。"""
    oldest = path.with_name(f"{path.name}.{LOG_BACKUP_COUNT}")
    oldest.unlink(missing_ok=True)
    for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
        current = path.with_name(f"{path.name}.{index}")
        if current.exists():
            current.replace(path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def _pump_process_log(stream, path: Path) -> None:
    """由 launcher 消费子进程 pipe，使 Windows 上也能在运行期轮转日志。"""
    handle = None
    try:
        handle = path.open("ab", buffering=0)
        size = path.stat().st_size if path.exists() else 0
        while True:
            reader = getattr(stream, "read1", stream.read)
            chunk = reader(64 * 1024)
            if not chunk:
                break
            if size and size + len(chunk) > LOG_MAX_BYTES:
                handle.close()
                handle = None
                _rotate_log(path)
                handle = path.open("ab", buffering=0)
                size = 0
            handle.write(chunk)
            size += len(chunk)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


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


def _supervisor_log(home: Path, msg: str) -> None:
    try:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
        logs = home / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "d2a-launcher.log"
        if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > LOG_MAX_BYTES:
            _rotate_log(path)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        pass


def spawn_managed(name: str, cmd: list[str], *, home: Path, env: dict[str, str],
                  log_name: str, listen: int | None = None) -> subprocess.Popen:
    """Spawn and register a process for crash-restart supervision."""
    proc = _spawn(cmd, home=home, env=env, log_name=log_name)
    _MANAGED.append({
        "name": name, "cmd": cmd, "home": home, "env": env,
        "log_name": log_name, "listen": listen, "proc": proc,
        "restarts": 0, "window_start": time.time(), "failed": False,
        "last_exit_code": None,
        "loaded_config_revision": _config_revision_for_cmd(cmd),
    })
    return proc


def health_summary() -> tuple[int, int, list[str]]:
    """Return (up, total, down_names) across supervised processes."""
    up = 0
    down: list[str] = []
    for m in _MANAGED:
        if m["proc"].poll() is None:
            up += 1
        else:
            down.append(m["name"] + (" 已停止" if m["failed"] else " 重启中"))
    return up, len(_MANAGED), down


def _config_revision_for_cmd(cmd: list[str]) -> str | None:
    try:
        index = cmd.index("--config")
        path = Path(cmd[index + 1])
        content = path.read_bytes()
        return "sha256:" + hashlib.sha256(content).hexdigest()
    except (ValueError, IndexError, OSError, TypeError):
        return None


def _managed_config_revision(managed: dict) -> str | None:
    """进程启动时捕获的 revision；不能读取当前文件冒充已加载版本。"""
    return managed.get("loaded_config_revision")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, pending_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending_name, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(pending_name).unlink(missing_ok=True)
        raise


def _refresh_middle_autostart_status(home: Path, *, now: float | None = None) -> None:
    """Windows 上定期查询真实计划任务，避免安装标记长期冒充实际状态。"""
    global _AUTOSTART_LAST_CHECK_EPOCH
    current = time.time() if now is None else now
    if sys.platform != "win32" or not (home / "config" / "connect.yaml").is_file():
        return
    if current - _AUTOSTART_LAST_CHECK_EPOCH < 60:
        return
    _AUTOSTART_LAST_CHECK_EPOCH = current
    path = home / "data" / "run" / "autostart-status.json"
    task_name = "data2agent-middle"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(previous, dict) and previous.get("task_name"):
            task_name = str(previous["task_name"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name],
            capture_output=True, timeout=10, check=False,
            creationflags=_creationflags(),
        )
        payload = {
            "installed": result.returncode == 0,
            "task_name": task_name,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "check_source": "schtasks",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload = {
            "installed": None,
            "task_name": task_name,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "check_source": "schtasks",
            "error_code": "autostart_check_failed",
            "error_type": type(exc).__name__,
        }
    _write_json_atomic(path, payload)


def write_process_status(home: Path) -> None:
    """将 launcher 真实管理的 PID/存活状态发布给本机管理 API。"""
    run_dir = home / "data" / "run"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        _refresh_middle_autostart_status(home)
        payload = {
            "updated_at_epoch": time.time(),
            "launcher_pid": os.getpid(),
            "startup_mode": _STARTUP_MODE,
            "processes": [
                {
                    "name": managed["name"],
                    "pid": getattr(managed["proc"], "pid", None),
                    "alive": managed["proc"].poll() is None,
                    "failed": bool(managed["failed"]),
                    "restarts": int(managed["restarts"]),
                    "last_exit_code": managed.get("last_exit_code"),
                    "failed_at_epoch": managed.get("failed_at"),
                    "cooldown_until_epoch": (
                        float(managed["failed_at"]) + SUPERVISE_COOLDOWN
                        if managed.get("failed") and managed.get("failed_at") else None
                    ),
                    "loaded_config_revision": _managed_config_revision(managed),
                }
                for managed in _MANAGED
            ],
        }
        _write_json_atomic(run_dir / "process-status.json", payload)
    except Exception:
        pass


def _pid_is_alive(pid: int) -> bool:
    """跨平台只读探测 PID；Windows 不使用 os.kill，避免控制信号副作用。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            try:
                return bool(kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _ObservedProcess:
    """可被 supervisor 接管的既有子进程最小适配器。"""

    def __init__(self, pid: int):
        self.pid = pid

    def poll(self) -> int | None:
        return None if _pid_is_alive(self.pid) else 1


def adopt_recorded_processes(
    home: Path,
    specs: list[tuple[str, int | None, list[str], str]],
    *,
    env: dict[str, str],
    max_age_seconds: float = 300.0,
) -> list[str]:
    """launcher 异常退出后，从短期状态快照接管仍存活的子进程。

    Global mutex 已证明旧 launcher 不在；仅接受新鲜快照和配置内已知角色，
    避免按陈旧 PID 误接管无关进程。接管后可继续健康检查、停止及崩溃重启。
    """
    path = home / "data" / "run" / "process-status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated = float(payload["updated_at_epoch"])
        if time.time() - updated > max_age_seconds:
            return []
        records = {
            str(item.get("name")): item
            for item in payload.get("processes", [])
            if isinstance(item, dict)
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return []
    adopted: list[str] = []
    existing = {managed["name"] for managed in _MANAGED}
    for name, listen, cmd, log_name in specs:
        if name in existing:
            continue
        item = records.get(name)
        try:
            pid = int(item.get("pid")) if item is not None else 0
        except (TypeError, ValueError):
            pid = 0
        if not item or not item.get("alive") or not _pid_is_alive(pid):
            continue
        proc = _ObservedProcess(pid)
        _CHILDREN.append(proc)  # type: ignore[arg-type]
        _MANAGED.append({
            "name": name, "cmd": cmd, "home": home, "env": env,
            "log_name": log_name, "listen": listen, "proc": proc,
            "restarts": int(item.get("restarts") or 0),
            "window_start": time.time(), "failed": False,
            "last_exit_code": item.get("last_exit_code"),
            "loaded_config_revision": item.get("loaded_config_revision"),
        })
        adopted.append(name)
        _supervisor_log(home, f"adopted orphan {name} pid={pid}")
    return adopted


def supervise_once(home: Path, *, max_restarts: int = SUPERVISE_MAX_RESTARTS,
                   window: float = SUPERVISE_WINDOW) -> None:
    """One monitoring pass: respawn dead workers unless the breaker tripped."""
    now = time.time()
    for m in _MANAGED:
        if m["failed"]:
            if now - float(m.get("failed_at", now)) < SUPERVISE_COOLDOWN:
                continue
            m["failed"] = False
            m["restarts"] = 0
            m["window_start"] = now
            _supervisor_log(
                home, f"{m['name']} 熵断冷却 {SUPERVISE_COOLDOWN:.0f}s 后自动试探重启")
        exit_code = m["proc"].poll()
        if exit_code is None:
            continue
        m["last_exit_code"] = exit_code
        if now - m["window_start"] > window:
            m["window_start"] = now
            m["restarts"] = 0
        if m["restarts"] >= max_restarts:
            if not m["failed"]:
                m["failed"] = True
                m["failed_at"] = now
                _supervisor_log(home, f"{m['name']} 在 {window:.0f}s 内退出 "
                                      f"{max_restarts} 次,停止重启(需人工排查日志)")
            continue
        # Port still held (old process not fully gone) — retry next pass.
        if m["listen"] is not None and _port_open("127.0.0.1", m["listen"]):
            continue
        m["restarts"] += 1
        try:
            try:
                _CHILDREN.remove(m["proc"])
            except ValueError:
                pass
            m["proc"] = _spawn(m["cmd"], home=home, env=m["env"],
                               log_name=m["log_name"])
            m["loaded_config_revision"] = _config_revision_for_cmd(m["cmd"])
            _supervisor_log(home, f"重启 {m['name']}(第 {m['restarts']} 次)")
        except OSError as e:
            _supervisor_log(home, f"重启 {m['name']} 失败:{e}")


def ensure_configured_workers(
    role: str, home: Path, python: Path, base_env: dict[str, str],
) -> list[str]:
    """配置文件在启动后创建时也要拉起 worker。

    首次安装会先启动管理页再在浏览器写 connect.yaml，因此不能
    只在 launcher 启动瞬间判断一次 configured。
    """
    existing = {m["name"] for m in _MANAGED}
    started: list[str] = []
    env = _merge_secrets_env(home, dict(base_env))
    landing = landing_db_path(role, home)
    if landing is not None and worker_commands(role, home, python):
        ensure_landing_db(python, landing, home=home, env=env)
    for name, listen, cmd in worker_commands(role, home, python):
        if name in existing:
            continue
        if listen is not None and _port_open("127.0.0.1", listen):
            _supervisor_log(
                home, f"skip unmanaged {name}: port 127.0.0.1:{listen} already open")
            continue
        try:
            proc = spawn_managed(
                name, cmd, home=home, env=env,
                log_name=f"d2a-{name}", listen=listen)
            started.append(name)
            _supervisor_log(
                home, f"started configured worker {name} pid={getattr(proc, 'pid', '?')}")
        except OSError as exc:
            _supervisor_log(home, f"start configured worker {name} failed:{exc}")
    return started


def restart_configured_workers(
    role: str, home: Path, python: Path, base_env: dict[str, str],
) -> list[str]:
    """响应管理端的凭据变更标记，仅重启业务 worker，不动 admin。"""
    flag = home / "data" / "restart-workers.flag"
    if not flag.is_file():
        return []
    # 先消费标记再停止进程；若标记因权限/磁盘问题无法删除，保留现有
    # worker，避免 supervisor 每轮重复杀进程形成永久重启风暴。
    try:
        flag.unlink()
    except OSError as exc:
        _supervisor_log(home, f"remove restart flag failed:{exc}")
        return []
    for managed in list(_MANAGED):
        if managed["name"] == "admin":
            continue
        _kill_process_tree(managed["proc"])
        try:
            _CHILDREN.remove(managed["proc"])
        except ValueError:
            pass
        _MANAGED.remove(managed)
    _supervisor_log(home, "configuration/secrets changed; restarting workers")
    return ensure_configured_workers(role, home, python, base_env)


def start_supervisor(
    home: Path, *,
    worker_context: tuple[str, Path, dict[str, str]] | None = None,
) -> threading.Thread:
    _SUPERVISE_STOP.clear()

    def loop() -> None:
        while not _SUPERVISE_STOP.wait(SUPERVISE_INTERVAL):
            if worker_context is not None:
                role, python, base_env = worker_context
                restart_configured_workers(role, home, python, base_env)
                ensure_configured_workers(role, home, python, base_env)
            supervise_once(home)
            write_process_status(home)

    t = threading.Thread(target=loop, name="d2a-supervisor", daemon=True)
    t.start()
    return t


def acquire_single_instance(mutex_name: str) -> bool:
    """Return True if this process is the primary instance; False if another holds it."""
    global _MUTEX_HANDLE
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            # 无法建立 Global mutex 时必须 fail closed；继续启动会让开机任务
            # 与登录用户各跑一套 connector，破坏调度和维护的单实例假设。
            return False
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
        return [
            ("connector", None, [
                py, "-m", "data2agent.middle.extract", "serve",
                "--config", str(cfg),
            ]),
            ("maintenance", None, [
                py, "-m", "data2agent.middle.maintenance",
                "--config", str(cfg),
                "--backup-dir", str(home / "data" / "backups"),
                "--status-file", str(home / "data" / "run" / "maintenance-status.json"),
                "--every", "86400",
            ]),
        ]

    cfg = home / "config" / "platform.yaml"
    if not cfg.is_file():
        return []
    landing = str(home / "data" / "factory.sqlite")
    templates = str(home / "app" / "templates")
    return [
        ("ingest", 8850, [py, "-m", "data2agent.platform.ingest",
                          "--landing", landing, "--host", "0.0.0.0", "--port", "8850"]),
        # apply 是纯落地库操作:不接受 --config,须显式给 --templates(cwd 无 templates)
        ("apply", None, [py, "-m", "data2agent.middle.extract", "apply",
                         "--landing", landing, "--templates", templates,
                         "--every", "30", "--committed-only", "--all-sources"]),
        ("maintenance", None, [
            py, "-m", "data2agent.platform.maintenance",
            "--landing", landing,
            "--backup-dir", str(home / "data" / "backups"),
            "--every", "86400",
        ]),
        ("mcp", 8848, [py, "-m", "data2agent.platform.mcp_server",
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
         "import sys; from data2agent.shared.store.landing import LandingStore; "
         "LandingStore(sys.argv[1])", str(landing)],
        cwd=str(home), env=env, capture_output=True, check=False,
        creationflags=_creationflags(),
    )


def admin_url(host: str, port: int, configured: bool, *, role: str) -> str:
    """Return the canonical UI route for the installed role.

    Platform is Vue-only.  The root path is the canonical console entry.
    """
    base = f"http://{host}:{port}"
    if role == "platform":
        return base + ("/" if configured else "/setup")
    return base + "/" + ("" if configured else "config")


def open_admin(url: str) -> None:
    webbrowser.open(url)


def _tray_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=(18, 49, 79, 255))
    draw.rectangle((20, 28, 44, 48), fill=(232, 244, 253, 255))
    return img


def _health_text(title: str) -> str:
    up, total, down = health_summary()
    if total == 0:
        return title
    if not down:
        return f"{title} — 后台 {up}/{total} 正常"
    return f"{title} — {up}/{total} 正常;异常:{', '.join(down)}"


def run_tray(
    *, title: str, url: str, home: Path | None = None,
    worker_context: tuple[str, Path, dict[str, str]] | None = None,
) -> int:
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

    if home is not None:
        start_supervisor(home, worker_context=worker_context)

    def on_open(icon, item):  # noqa: ARG001
        open_admin(url)

    def on_status(icon, item):  # noqa: ARG001
        up, total, down = health_summary()
        if total == 0:
            body = "未拉起后台进程(仅管理界面)。"
        elif not down:
            body = f"后台进程全部正常({up}/{total})。"
        else:
            body = (f"{up}/{total} 正常。\n异常:{', '.join(down)}\n\n"
                    "日志见 data\\logs\\(d2a-launcher.log 记录重启),"
                    "也可在管理界面「日志」页查看。")
        if not _notify(icon, title, body):
            _msg(title, body)

    def on_quit(icon, item):  # noqa: ARG001
        _SUPERVISE_STOP.set()
        stop_children()
        write_process_status(home or Path.cwd())
        release_single_instance()
        icon.stop()

    menu = Menu(
        MenuItem("打开管理界面", on_open, default=True),
        MenuItem("运行状态…", on_status),
        MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("data2agent", _tray_image(), title, menu)

    def _open_soon():
        time.sleep(0.4)
        open_admin(url)

    def _refresh_title():
        while not _SUPERVISE_STOP.wait(SUPERVISE_INTERVAL):
            try:
                icon.title = _health_text(title)
            except Exception:
                pass

    if not getattr(run_tray, "_skip_auto_open", False):
        threading.Thread(target=_open_soon, daemon=True).start()
    if _MANAGED:
        threading.Thread(target=_refresh_title, daemon=True).start()

    icon.run()
    _SUPERVISE_STOP.set()
    stop_children()
    if home is not None:
        write_process_status(home)
    release_single_instance()
    return 0


def run_headless(
    *, home: Path,
    worker_context: tuple[str, Path, dict[str, str]] | None,
) -> int:
    """Windows 开机任务/Session 0 模式：无托盘，但保留进程监控与重启。"""
    start_supervisor(home, worker_context=worker_context)
    try:
        while not _SUPERVISE_STOP.wait(60.0):
            write_process_status(home)
    except KeyboardInterrupt:
        pass
    finally:
        _SUPERVISE_STOP.set()
        stop_children()
        write_process_status(home)
        release_single_instance()
    return 0


def main(argv: list[str] | None = None) -> int:
    global _STARTUP_MODE
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
    ap.add_argument("--headless", action="store_true",
                    help="无 GUI 常驻并监控 worker(开机任务/Session 0)")
    args = ap.parse_args(argv)
    _STARTUP_MODE = (
        "headless" if args.headless else
        "manual" if args.no_tray else
        "tray"
    )

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
    url = admin_url(host, port, configured, role=args.role)
    _supervisor_log(
        home,
        f"launcher start role={args.role} home={home} "
        f"port={port} configured={configured}",
    )

    # Secondary instance: just open the already-running admin UI.
    if not acquire_single_instance(cfg["mutex"]):
        _supervisor_log(home, "single instance already held")
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
    # Windows 控制台默认 GBK,日志中的 UTF-8 中文 Traceback 会乱码
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["D2A_HOME"] = str(home)
    env = _merge_secrets_env(home, env)
    env.update(portable_vue_dist_env(home, env))

    admin_log = "d2a-console" if args.role == "platform" else "d2a-middle-admin"
    admin_cmd = [str(venv_py), "-m", cfg["module"], *cfg["extra_args"]]
    admin_already_up = _port_open(host, port)
    if not admin_already_up:
        try:
            _supervisor_log(home, f"spawning admin cmd={_display_cmd(admin_cmd)}")
            admin_proc = spawn_managed(
                "admin", admin_cmd, home=home, env=env,
                log_name=admin_log, listen=port)
            _supervisor_log(
                home,
                f"spawned admin pid={getattr(admin_proc, 'pid', '?')} "
                f"log={home / 'data' / 'logs' / (admin_log + '.log')}",
            )
        except OSError as e:
            release_single_instance()
            _supervisor_log(home, f"admin spawn failed error={e}")
            _msg(title, f"无法启动:\n{e}", error=True)
            return 3
        timeout = _admin_startup_timeout()
        if not _wait_admin_ready(
            host, port, admin_proc, timeout=timeout, home=home,
        ):
            exit_code = admin_proc.poll()
            if exit_code is None:
                reason = f"启动超时(等待 {timeout:g} 秒)"
            else:
                reason = f"启动失败(进程已退出,退出码 {exit_code})"
            failure_message = _admin_startup_failure_message(
                reason=reason,
                host=host,
                port=port,
                home=home,
                log_name=admin_log,
                cmd=admin_cmd,
            )
            _supervisor_log(
                home,
                "admin startup failed message="
                + failure_message.replace("\n", " | "),
            )
            stop_children()
            release_single_instance()
            _msg(title, failure_message, error=True)
            return 4

    worker_context = None
    if admin_already_up:
        specs = [("admin", port, admin_cmd, admin_log)]
        specs.extend(
            (name, listen, cmd, f"d2a-{name}")
            for name, listen, cmd in worker_commands(
                args.role, home, venv_py)
        )
        adopted = adopt_recorded_processes(home, specs, env=env)
        if "admin" not in adopted:
            _supervisor_log(
                home, "admin port already open but no fresh process record; "
                "leaving external admin untouched")
    if not args.no_workers:
        worker_context = (args.role, venv_py, env)
        # 已配置的安装立即启动；未配置的安装由 supervisor 在
        # connect.yaml/platform.yaml 落盘后自动发现并启动。若 launcher
        # 异常退出后重启，上一段会先接管仍存活的 worker，避免重复拉起。
        started = ensure_configured_workers(args.role, home, venv_py, env)
        if configured and not started and not worker_commands(args.role, home, venv_py):
            _msg(title, "配置已存在，但未生成后台任务命令。", error=True)
    write_process_status(home)

    if args.headless:
        return run_headless(home=home, worker_context=worker_context)

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
        _MANAGED.clear()   # no supervision in no-tray mode
        write_process_status(home)
        release_single_instance()
        return 0

    run_tray._skip_auto_open = bool(args.no_browser)  # type: ignore[attr-defined]
    return run_tray(
        title=title, url=url, home=home, worker_context=worker_context)


if __name__ == "__main__":
    raise SystemExit(main())
