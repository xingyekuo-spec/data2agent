"""便携包唯一入口启动器(scripts/launch_admin_ui.py)单元测试。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "launch_admin_ui.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("launch_admin_ui", LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["launch_admin_ui"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_role_config_uses_home(tmp_path):
    mod = _load_launcher()
    mid = mod._role_config("middle", tmp_path)
    assert mid["port"] == 8851
    assert mid["module"] == "data2agent.middle.admin"
    assert "--home" in mid["extra_args"]
    assert mid["config_file"] == tmp_path / "config" / "connect.yaml"
    assert "middle" in mid["mutex"]


def test_detect_portable_root(tmp_path):
    mod = _load_launcher()
    assert mod.detect_portable_root(tmp_path) is None
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "python.exe").write_bytes(b"")
    nested = tmp_path / "config"
    nested.mkdir()
    assert mod.detect_portable_root(nested) == tmp_path.resolve()


def test_worker_commands_empty_until_configured(tmp_path):
    mod = _load_launcher()
    py = tmp_path / "runtime" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    assert mod.worker_commands("middle", tmp_path, py) == []
    assert mod.worker_commands("platform", tmp_path, py) == []

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "connect.yaml").write_text("x: 1", encoding="utf-8")
    mid = mod.worker_commands("middle", tmp_path, py)
    assert {name for name, _, _ in mid} == {"connector", "maintenance"}
    maintenance_cmd = next(cmd for name, _, cmd in mid if name == "maintenance")
    assert "--status-file" in maintenance_cmd

    (tmp_path / "config" / "platform.yaml").write_text("x: 1", encoding="utf-8")
    plat = mod.worker_commands("platform", tmp_path, py)
    assert {n for n, _, _ in plat} == {
        "ingest", "apply", "maintenance", "mcp"}
    # apply 是纯落地库操作:不能传 --config(子命令不接受),必须显式带 --templates
    apply_cmd = next(cmd for n, _, cmd in plat if n == "apply")
    assert "--config" not in apply_cmd
    assert "--templates" in apply_cmd
    assert "--landing" in apply_cmd


def test_supervisor_discovers_middle_config_created_after_startup(
    tmp_path, monkeypatch,
):
    """首次启动时尚无 connect.yaml，浏览器配置落盘后必须自动启动 connector。"""
    mod = _load_launcher()
    mod._MANAGED.clear()
    py = tmp_path / "runtime" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    spawned: list[str] = []

    def fake_spawn_managed(name, cmd, **kwargs):
        spawned.append(name)
        proc = _FakeProc(alive=True)
        mod._MANAGED.append({
            "name": name, "cmd": cmd, "home": tmp_path,
            "env": kwargs["env"], "log_name": kwargs["log_name"],
            "listen": kwargs.get("listen"), "proc": proc,
            "restarts": 0, "window_start": 0.0, "failed": False,
        })
        return proc

    monkeypatch.setattr(mod, "spawn_managed", fake_spawn_managed)
    assert mod.ensure_configured_workers("middle", tmp_path, py, {}) == []
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "connect.yaml").write_text("x: 1", encoding="utf-8")
    assert mod.ensure_configured_workers("middle", tmp_path, py, {}) == [
        "connector", "maintenance"]
    assert spawned == ["connector", "maintenance"]
    # 后续巡检不得重复拉起。
    assert mod.ensure_configured_workers("middle", tmp_path, py, {}) == []
    mod._MANAGED.clear()


def test_restart_flag_restarts_worker_with_fresh_secrets(tmp_path, monkeypatch):
    mod = _load_launcher()
    mod._MANAGED.clear()
    py = tmp_path / "runtime" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "connect.yaml").write_text("x: 1", encoding="utf-8")
    (tmp_path / "config" / "secrets.env").write_text(
        "D2A_INGEST_TOKEN=fresh\n", encoding="utf-8")
    old = _FakeProc(alive=True)
    mod._MANAGED.append({
        "name": "connector", "cmd": ["old"], "home": tmp_path, "env": {},
        "log_name": "d2a-connector", "listen": None, "proc": old,
        "restarts": 0, "window_start": 0.0, "failed": False,
    })
    killed: list[object] = []
    captured_env: list[dict[str, str]] = []
    monkeypatch.setattr(mod, "_kill_process_tree", lambda proc: killed.append(proc))

    def fake_spawn_managed(name, cmd, **kwargs):
        captured_env.append(kwargs["env"])
        proc = _FakeProc(alive=True)
        mod._MANAGED.append({
            "name": name, "cmd": cmd, "home": tmp_path,
            "env": kwargs["env"], "log_name": kwargs["log_name"],
            "listen": kwargs.get("listen"), "proc": proc,
            "restarts": 0, "window_start": 0.0, "failed": False,
        })
        return proc

    monkeypatch.setattr(mod, "spawn_managed", fake_spawn_managed)
    flag = tmp_path / "data" / "restart-workers.flag"
    flag.parent.mkdir()
    flag.touch()
    assert mod.restart_configured_workers("middle", tmp_path, py, {}) == [
        "connector", "maintenance"]
    assert killed == [old]
    assert not flag.exists()
    assert captured_env[0]["D2A_INGEST_TOKEN"] == "fresh"
    mod._MANAGED.clear()


def test_restart_flag_unlink_failure_keeps_workers_running(tmp_path, monkeypatch):
    mod = _load_launcher()
    mod._MANAGED.clear()
    old = _FakeProc(alive=True)
    mod._MANAGED.append({
        "name": "connector", "cmd": ["old"], "home": tmp_path, "env": {},
        "log_name": "d2a-connector", "listen": None, "proc": old,
        "restarts": 0, "window_start": 0.0, "failed": False,
    })
    flag = tmp_path / "data" / "restart-workers.flag"
    flag.parent.mkdir(parents=True)
    flag.touch()
    killed: list[object] = []
    real_unlink = Path.unlink

    def fail_flag_unlink(path, *args, **kwargs):
        if path == flag:
            raise PermissionError("read only")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_flag_unlink)
    monkeypatch.setattr(mod, "_kill_process_tree", lambda proc: killed.append(proc))

    assert mod.restart_configured_workers(
        "middle", tmp_path, Path(sys.executable), {}) == []
    assert killed == []
    assert mod._MANAGED[0]["proc"] is old
    mod._MANAGED.clear()


def test_adopt_recorded_processes_avoids_duplicate_workers(tmp_path, monkeypatch):
    mod = _load_launcher()
    mod._MANAGED.clear()
    mod._CHILDREN.clear()
    run_dir = tmp_path / "data" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "process-status.json").write_text(json.dumps({
        "updated_at_epoch": time.time(),
        "launcher_pid": 999,
        "processes": [
            {"name": "connector", "pid": 101, "alive": True, "restarts": 2},
            {"name": "maintenance", "pid": 102, "alive": True, "restarts": 0},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(mod, "_pid_is_alive", lambda pid: pid in {101, 102})
    specs = [
        ("connector", None, ["python", "connector"], "d2a-connector"),
        ("maintenance", None, ["python", "maintenance"], "d2a-maintenance"),
    ]

    assert mod.adopt_recorded_processes(
        tmp_path, specs, env={}) == ["connector", "maintenance"]
    assert {item["name"] for item in mod._MANAGED} == {
        "connector", "maintenance"}
    assert mod.health_summary()[:2] == (2, 2)
    mod._MANAGED.clear()
    mod._CHILDREN.clear()


def test_windows_autostart_probe_replaces_stale_install_marker(tmp_path, monkeypatch):
    mod = _load_launcher()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "connect.yaml").write_text(
        "sources: {}\n", encoding="utf-8")
    run_dir = tmp_path / "data" / "run"
    run_dir.mkdir(parents=True)
    marker = run_dir / "autostart-status.json"
    marker.write_text(json.dumps({
        "installed": True, "task_name": "custom-middle-task",
    }), encoding="utf-8")
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    mod._AUTOSTART_LAST_CHECK_EPOCH = 0
    mod._refresh_middle_autostart_status(tmp_path, now=1000)
    status = json.loads(marker.read_text(encoding="utf-8"))
    assert status["installed"] is False
    assert status["task_name"] == "custom-middle-task"
    assert status["check_source"] == "schtasks"


def test_windows_mutex_creation_failure_is_fail_closed(monkeypatch):
    mod = _load_launcher()

    class Kernel32:
        @staticmethod
        def CreateMutexW(*args):
            return 0

    import ctypes
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=Kernel32()), raising=False)

    assert mod.acquire_single_instance("Global\\data2agent-test") is False


def test_landing_db_path_platform_only(tmp_path):
    mod = _load_launcher()
    assert mod.landing_db_path("middle", tmp_path) is None
    assert mod.landing_db_path("platform", tmp_path) == tmp_path / "data" / "factory.sqlite"


def test_ensure_landing_db_precreates(tmp_path):
    """预建落地库:mcp 启动即要求库存在,不能等首个推送。"""
    mod = _load_launcher()
    landing = tmp_path / "data" / "factory.sqlite"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")])
    mod.ensure_landing_db(Path(sys.executable), landing, home=tmp_path, env=env)
    assert landing.is_file(), "落地库应被预先创建"


def test_admin_url():
    mod = _load_launcher()
    assert mod.admin_url("127.0.0.1", 8851, False, role="middle").endswith("/config")
    assert mod.admin_url("127.0.0.1", 8851, True, role="middle") == "http://127.0.0.1:8851/"
    assert mod.admin_url("127.0.0.1", 8849, False, role="platform").endswith("/setup")
    assert mod.admin_url("127.0.0.1", 8849, True, role="platform").endswith("/")


def test_second_instance_opens_existing_admin(tmp_path, monkeypatch):
    mod = _load_launcher()
    opened: list[str] = []
    monkeypatch.setattr(mod, "open_admin", lambda u: opened.append(u))
    monkeypatch.setattr(mod, "_msg", lambda *a, **k: None)
    monkeypatch.setattr(mod, "acquire_single_instance", lambda name: False)
    monkeypatch.setattr(mod, "_wait_port", lambda *a, **k: True)
    code = mod.main(["--role", "middle", "--home", str(tmp_path), "--no-tray"])
    assert code == 0
    assert opened and opened[0].startswith("http://127.0.0.1:8851/")


def test_missing_python_exits_2(tmp_path, monkeypatch):
    mod = _load_launcher()
    monkeypatch.setattr(mod, "_msg", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_python", lambda home: Path("/nonexistent/python.exe"))
    monkeypatch.setattr(mod, "acquire_single_instance", lambda name: True)
    monkeypatch.setattr(mod, "release_single_instance", lambda: None)
    code = mod.main(["--role", "middle", "--home", str(tmp_path), "--no-tray", "--no-browser"])
    assert code == 2


def test_port_open_localhost_negative():
    mod = _load_launcher()
    assert mod._port_open("127.0.0.1", 1) is False


def test_notify_uses_icon_notification_api():
    mod = _load_launcher()
    calls: list[tuple[str, str]] = []

    class Icon:
        def notify(self, message, title):
            calls.append((message, title))

    assert mod._notify(Icon(), "data2agent 平台", "后台进程全部正常(1/1)。") is True
    assert calls == [("后台进程全部正常(1/1)。", "data2agent 平台")]


def test_notify_returns_false_when_notification_unavailable():
    mod = _load_launcher()

    class BrokenIcon:
        def notify(self, message, title):
            raise RuntimeError("notification backend unavailable")

    assert mod._notify(object(), "title", "body") is False
    assert mod._notify(BrokenIcon(), "title", "body") is False


class _FakeProc:
    """poll() returns None while 'alive', else an exit code."""

    def __init__(self, alive: bool = True):
        self._alive = alive
        self.pid = 12345

    def poll(self):
        return None if self._alive else 1


def test_admin_startup_timeout_default_and_env(monkeypatch):
    mod = _load_launcher()
    monkeypatch.delenv("D2A_ADMIN_STARTUP_TIMEOUT", raising=False)
    assert mod._admin_startup_timeout() == mod.ADMIN_STARTUP_TIMEOUT
    monkeypatch.setenv("D2A_ADMIN_STARTUP_TIMEOUT", "120")
    assert mod._admin_startup_timeout() == 120
    monkeypatch.setenv("D2A_ADMIN_STARTUP_TIMEOUT", "bad")
    assert mod._admin_startup_timeout() == mod.ADMIN_STARTUP_TIMEOUT


def test_admin_startup_timeout_reports_logs_and_manual_command(tmp_path, monkeypatch):
    mod = _load_launcher()
    py = tmp_path / "runtime" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    messages: list[tuple[str, str, bool]] = []
    waits: list[float] = []

    monkeypatch.setattr(mod, "_msg",
                        lambda title, text, error=False: messages.append((title, text, error)))
    monkeypatch.setattr(mod, "_resolve_python", lambda home: py)
    monkeypatch.setattr(mod, "acquire_single_instance", lambda name: True)
    monkeypatch.setattr(mod, "release_single_instance", lambda: None)
    monkeypatch.setattr(mod, "spawn_managed", lambda *a, **k: _FakeProc(alive=True))
    monkeypatch.setattr(mod, "stop_children", lambda: None)

    def fake_wait_admin_ready(host, port, proc, *, timeout, home):
        waits.append(timeout)
        return False

    monkeypatch.setattr(mod, "_wait_admin_ready", fake_wait_admin_ready)
    code = mod.main([
        "--role", "platform", "--home", str(tmp_path),
        "--no-tray", "--no-browser",
    ])

    assert code == 4
    assert waits == [mod.ADMIN_STARTUP_TIMEOUT]
    assert messages and messages[0][2] is True
    text = messages[0][1]
    assert "启动超时" in text
    assert "d2a-console.log" in text
    assert "d2a-launcher.log" in text
    assert "-m data2agent.platform.console" in text


def test_wait_admin_ready_logs_progress_and_exit(tmp_path, monkeypatch):
    mod = _load_launcher()
    monkeypatch.setattr(mod, "_port_open", lambda host, port: False)
    monkeypatch.setattr(mod.time, "sleep", lambda seconds: None)
    proc = _FakeProc(alive=False)

    assert mod._wait_admin_ready(
        "127.0.0.1", 8849, proc, timeout=10, home=tmp_path,
    ) is False

    log = tmp_path / "data" / "logs" / "d2a-launcher.log"
    text = log.read_text(encoding="utf-8")
    assert "admin exited before port ready" in text


def test_supervise_restarts_dead_worker(tmp_path, monkeypatch):
    mod = _load_launcher()
    mod._MANAGED.clear()
    dead = _FakeProc(alive=False)
    mod._MANAGED.append({
        "name": "connector", "cmd": ["x"], "home": tmp_path, "env": {},
        "log_name": "connector", "listen": None, "proc": dead,
        "restarts": 0, "window_start": 0.0, "failed": False,
    })
    spawned: list = []

    def fake_spawn(cmd, *, home, env, log_name=None):
        spawned.append(log_name)
        return _FakeProc(alive=True)

    monkeypatch.setattr(mod, "_spawn", fake_spawn)
    mod.supervise_once(tmp_path)
    assert spawned == ["connector"], "死掉的 worker 应被重启一次"
    up, total, down = mod.health_summary()
    assert up == 1 and total == 1 and down == []
    mod._MANAGED.clear()


def test_supervise_circuit_breaker(tmp_path, monkeypatch):
    mod = _load_launcher()
    mod._MANAGED.clear()
    mod._MANAGED.append({
        "name": "mcp", "cmd": ["x"], "home": tmp_path, "env": {},
        "log_name": "mcp", "listen": None, "proc": _FakeProc(alive=False),
        "restarts": 0, "window_start": 9e18, "failed": False,  # 未来窗口:不重置计数
    })
    monkeypatch.setattr(mod, "_spawn",
                        lambda cmd, **k: _FakeProc(alive=False))  # 每次重启即死
    for _ in range(mod.SUPERVISE_MAX_RESTARTS + 2):
        mod.supervise_once(tmp_path)
    assert mod._MANAGED[0]["failed"] is True, "反复崩溃应触发熔断,停止重启"
    assert mod._MANAGED[0]["restarts"] == mod.SUPERVISE_MAX_RESTARTS
    mod._MANAGED.clear()


def test_portable_vue_dist_env_sets_d2a_vue_dist(tmp_path):
    """platform 便携包 dist 位于 home/app/console-ui/dist,启动环境必须传给子进程。"""
    mod = _load_launcher()
    dist = tmp_path / "app" / "console-ui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    env = mod.portable_vue_dist_env(tmp_path, {"D2A_HOME": str(tmp_path)})
    assert env["D2A_VUE_DIST"] == str(dist.resolve())
    assert mod.portable_vue_dist_env(tmp_path / "empty", {}) == {}


def test_portable_vue_dist_env_preserves_explicit_override(tmp_path):
    """显式 D2A_VUE_DIST 优先,不得被便携默认路径覆盖。"""
    mod = _load_launcher()
    dist = tmp_path / "app" / "console-ui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    explicit = "/custom/vue/dist"
    assert mod.portable_vue_dist_env(
        tmp_path, {"D2A_VUE_DIST": explicit},
    ) == {}


def test_open_log_append_writes_bom_only_for_new_file(tmp_path):
    """新建日志写 UTF-8 BOM;追加已有文件不重复写;轮转后新文件重新带 BOM。"""
    mod = _load_launcher()
    log = tmp_path / "worker.log"
    handle, size = mod._open_log_append(log)
    handle.write("中文行\n".encode("utf-8"))
    handle.close()
    assert size == 3
    assert log.read_bytes().startswith(b"\xef\xbb\xbf")

    # 追加已有文件:不再写 BOM
    before = log.read_bytes()
    handle, size = mod._open_log_append(log)
    handle.write(b"second\n")
    handle.close()
    assert log.read_bytes().count(b"\xef\xbb\xbf") == 1
    assert log.read_bytes().startswith(before)
