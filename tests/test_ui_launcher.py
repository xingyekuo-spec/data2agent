"""便携包唯一入口启动器(scripts/launch_admin_ui.py)单元测试。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    assert mid["module"] == "data2agent.middle_admin"
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
    assert len(mid) == 1 and mid[0][0] == "connector"

    (tmp_path / "config" / "platform.yaml").write_text("x: 1", encoding="utf-8")
    plat = mod.worker_commands("platform", tmp_path, py)
    assert {n for n, _, _ in plat} == {"ingest", "apply", "mcp"}
    # apply 是纯落地库操作:不能传 --config(子命令不接受),必须显式带 --templates
    apply_cmd = next(cmd for n, _, cmd in plat if n == "apply")
    assert "--config" not in apply_cmd
    assert "--templates" in apply_cmd
    assert "--landing" in apply_cmd


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
    assert mod.admin_url("127.0.0.1", 8851, False).endswith("/config")
    assert mod.admin_url("127.0.0.1", 8851, True) == "http://127.0.0.1:8851/"


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


class _FakeProc:
    """poll() returns None while 'alive', else an exit code."""

    def __init__(self, alive: bool = True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


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
