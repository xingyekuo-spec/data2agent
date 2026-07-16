"""管理界面启动器(scripts/launch_admin_ui.py)单元测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "launch_admin_ui.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("launch_admin_ui", LAUNCHER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["launch_admin_ui"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_role_config_paths(tmp_path):
    mod = _load_launcher()
    mid = mod._role_config("middle", tmp_path)
    assert mid["port"] == 8851
    assert mid["module"] == "data2agent.middle_admin"
    assert mid["config"] == tmp_path / "config" / "connect.yaml"

    plat = mod._role_config("platform", tmp_path)
    assert plat["port"] == 8849
    assert plat["module"] == "data2agent.console"
    assert plat["config"] == tmp_path / "config" / "platform.yaml"


def test_missing_config_exits_2(tmp_path, monkeypatch):
    mod = _load_launcher()
    monkeypatch.setattr(mod, "_msg", lambda *a, **k: None)
    code = mod.main(["--role", "middle", "--home", str(tmp_path), "--no-browser"])
    assert code == 2


def test_port_open_localhost_negative():
    mod = _load_launcher()
    assert mod._port_open("127.0.0.1", 1) is False
