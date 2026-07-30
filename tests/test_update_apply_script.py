"""换包脚本 apply-update.ps1 的端到端测试(需要 pwsh)。

覆盖评审要求的两类场景:
- 换包中途失败 → 已移动/改名的条目必须恢复,不留新旧混杂;
- 健康检查失败 → 自动回滚旧版本。

本机 macOS 无 pwsh 时自动 skip;GitHub ubuntu / windows runner 自带 pwsh。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from data2agent.platform.updater.apply_script import write_update_scripts
from data2agent.platform.updater.core import UpdateManager
from tests.test_updater import _make_portable_zip

PWSH = shutil.which("pwsh")
pytestmark = [
    pytest.mark.skipif(PWSH is None, reason="需要 pwsh 执行换包脚本"),
    pytest.mark.unit,
]

OLD_EXE = "old-exe"
OLD_PY = "old-py"
OLD_INFO = {"release_version": "v0.5.1", "role": "platform",
            "supported_ingest_protocol_versions": ["2"]}


def _make_install(home: Path) -> None:
    """构造旧版本安装目录(含 config/data,换包不得触碰)。"""
    (home / "runtime").mkdir(parents=True)
    (home / "runtime" / "python.exe").write_text(OLD_PY, encoding="utf-8")
    (home / "app" / "templates").mkdir(parents=True)
    (home / "app" / "templates" / "old.yaml").write_text("old", encoding="utf-8")
    (home / "data2agent.exe").write_text(OLD_EXE, encoding="utf-8")
    (home / "BUILD-INFO.json").write_text(json.dumps(OLD_INFO), encoding="utf-8")
    (home / "config").mkdir()
    (home / "config" / "platform.yaml").write_text("landing: data/factory.sqlite",
                                                   encoding="utf-8")
    (home / "data" / "logs").mkdir(parents=True)
    (home / "data" / "factory.sqlite").write_text("db", encoding="utf-8")


def _stage_new_package(home: Path, tmp_path: Path, version: str = "v0.6.0") -> Path:
    zip_path = _make_portable_zip(tmp_path, version)
    pkg_root = UpdateManager(home).stage(zip_path, expected_version=version)
    write_update_scripts(home, home / "data" / "updates")
    return pkg_root


def _run_apply(home: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoProfile", "-File",
         str(home / "data" / "updates" / "apply-update.ps1"),
         "-InstallDir", str(home), "-Staging", str(home / "data" / "updates" / "staging"),
         *extra],
        capture_output=True, encoding="utf-8", errors="replace", timeout=180)


def _assert_old_install_intact(home: Path) -> None:
    assert (home / "data2agent.exe").read_text(encoding="utf-8") == OLD_EXE
    assert (home / "runtime" / "python.exe").read_text(encoding="utf-8") == OLD_PY
    assert (home / "app" / "templates" / "old.yaml").is_file()
    assert json.loads((home / "BUILD-INFO.json").read_text(encoding="utf-8"))[
        "release_version"] == "v0.5.1"
    # config / data 全程不动
    assert (home / "config" / "platform.yaml").is_file()
    assert (home / "data" / "factory.sqlite").read_text(encoding="utf-8") == "db"
    # 不留新旧混杂:.old 应已全部恢复
    assert not list(home.glob("*.old"))


def test_refuses_while_app_running(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _make_install(home)
    _stage_new_package(home, tmp_path)
    # 模拟 8849 仍在监听(程序未退出);本机已有 console 占用时跳过
    import socket
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 8849))
    except OSError:
        sock.close()
        pytest.skip("本机 8849 已被占用,无法模拟「程序仍在运行」")
    sock.listen(1)
    try:
        result = _run_apply(home)
    finally:
        sock.close()
    assert result.returncode != 0
    assert "退出" in result.stdout
    _assert_old_install_intact(home)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX:用目录权限注入 Move 失败")
def test_swap_failure_rolls_back_partial_move(tmp_path):
    """换包中途 Move 失败:已改名 .old 的条目必须恢复(P1)。

    把暂存包根目录设为只读:data2agent.exe 已被改名 data2agent.exe.old,
    随后的 Move-Item 失败 → catch → 回滚 → 旧 exe 必须回到原位。
    """
    home = tmp_path / "home"
    home.mkdir()
    _make_install(home)
    pkg_root = _stage_new_package(home, tmp_path)
    os.chmod(pkg_root, stat.S_IRUSR | stat.S_IXUSR)  # 只读:不允许移出条目
    try:
        result = _run_apply(home)
    finally:
        os.chmod(pkg_root, stat.S_IRWXU)
    assert result.returncode != 0
    assert "换包失败" in result.stdout
    _assert_old_install_intact(home)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX:新版本用 shell 假 exe 模拟")
def test_health_check_failure_rolls_back(tmp_path):
    """新版本起不来(健康检查超时)→ 自动回滚并报告失败(P1)。"""
    home = tmp_path / "home"
    home.mkdir()
    _make_install(home)
    pkg_root = _stage_new_package(home, tmp_path)
    # 让新 exe 是一个能启动但不监听 8849 的可执行脚本
    fake_exe = pkg_root / "data2agent.exe"
    fake_exe.write_text("#!/bin/sh\nsleep 20\n", encoding="utf-8")
    fake_exe.chmod(0o755)

    result = _run_apply(home, "-HealthTimeoutSec", "4")

    assert result.returncode != 0
    assert "回滚" in result.stdout
    _assert_old_install_intact(home)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows:文件锁注入换包失败")
def test_locked_file_rolls_back_partial_move(tmp_path):
    """Windows 现场真实场景:runtime 文件被占用 → 部分换包后回滚。

    data2agent.exe 先成功移入,随后 runtime 因有打开句柄改名失败,
    回滚必须把已移入的新 exe 撤掉、旧 exe 恢复。
    """
    home = tmp_path / "home"
    home.mkdir()
    _make_install(home)
    _stage_new_package(home, tmp_path)
    handle = open(home / "runtime" / "python.exe", "rb")  # noqa: SIM115
    try:
        result = _run_apply(home)
    finally:
        handle.close()
    assert result.returncode != 0
    assert "换包失败" in result.stdout or "回滚" in result.stdout
    _assert_old_install_intact(home)


def test_zip_contains_no_config_or_data_dirs(tmp_path):
    """防回归:换包只覆盖白名单条目,即使包内带空 config/data 也不得覆盖现场。"""
    home = tmp_path / "home"
    home.mkdir()
    _make_install(home)
    zip_path = _make_portable_zip(tmp_path, "v0.6.0")
    # 模拟打包脚本附带的空 config/ 与 data/logs/
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.writestr("d2a-portable-platform-v0.6.0/config/.keep", "")
        zf.writestr("d2a-portable-platform-v0.6.0/data/logs/.keep", "")
    UpdateManager(home).stage(zip_path, expected_version="v0.6.0")
    write_update_scripts(home, home / "data" / "updates")
    # 白名单不含 config/data,脚本 MOVE_ITEMS 断言
    from data2agent.platform.updater.apply_script import APPLY_PS1
    assert "'config'" not in APPLY_PS1 and "'data'" not in APPLY_PS1
