"""便携包唯一入口启动器(scripts/launch_admin_ui.py)单元测试。"""

from __future__ import annotations

import importlib.util
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


def test_missing_python_exits_2(tmp_path, monkeypatch):
    mod = _load_launcher()
    monkeypatch.setattr(mod, "_msg", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_python", lambda home: Path("/nonexistent/python.exe"))
    code = mod.main(["--role", "middle", "--home", str(tmp_path), "--no-browser"])
    assert code == 2


def test_port_open_localhost_negative():
    mod = _load_launcher()
    assert mod._port_open("127.0.0.1", 1) is False
