from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_script", ROOT / "scripts" / "verify.py"
)
assert SPEC and SPEC.loader
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


def _quick(files: list[str]):
    return verify._quick_plan(files, base="HEAD", workers="2")


def _task_names(phases) -> set[str]:
    return {task.name for phase in phases for task in phase}


def test_docs_only_change_runs_no_application_tests():
    assert _quick(["docs/runbook/source-dev.md"]) == []


def test_connect_change_selects_erp_tests_only():
    phases = _quick(["data2agent/connect/config.py"])
    assert _task_names(phases) == {"Python affected"}
    command = phases[0][0].commands[0].argv
    assert "tests/test_connect.py" in command
    assert "tests/test_table_config.py" in command
    assert "tests/test_console.py" not in command


def test_console_change_selects_backend_and_frontend():
    phases = _quick(["data2agent/console/app.py"])
    assert _task_names(phases) == {"Python affected", "Vue Console"}


def test_unknown_file_falls_back_to_full_backend_and_frontend():
    phases = _quick(["config/custom.runtime"])
    assert _task_names(phases) == {"Python", "Vue Console"}


def test_test_infrastructure_change_falls_back_to_full_backend():
    phases = _quick(["tests/conftest.py"])
    assert _task_names(phases) == {"Python"}


def test_verify_script_change_runs_its_own_tests():
    phases = _quick(["scripts/verify.py"])
    assert _task_names(phases) == {"Python affected"}
    assert "tests/test_verify.py" in phases[0][0].commands[0].argv


def test_full_plan_parallelizes_backend_and_frontend_then_e2e():
    args = Namespace(
        mode="full",
        module=None,
        base="HEAD",
        workers="2",
    )
    phases = verify.build_plan(args)
    assert [task.name for task in phases[0]] == ["Python", "Vue Console"]
    assert [task.name for task in phases[1]] == ["E2E Mock", "E2E Real"]
    assert dict(phases[1][1].commands[0].env)["D2A_PYTHON"] == verify._python()
