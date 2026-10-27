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
    return verify._quick_plan(files, workers="2")


def _task_names(phases) -> set[str]:
    return {task.name for phase in phases for task in phase}


def test_docs_only_change_runs_no_application_tests():
    assert _quick(["docs/runbook/source-dev.md"]) == []


def test_connect_change_selects_erp_tests_only():
    phases = _quick(["data2agent/middle/extract/increment.py"])
    assert _task_names(phases) == {"Python affected"}
    command = phases[0][0].commands[0].argv
    assert "tests/middle/test_connect.py" in command
    assert "tests/middle/test_table_config.py" in command
    assert "tests/middle/test_webhook_sink.py" not in command


def test_sink_change_selects_push_contracts():
    phases = _quick(["data2agent/middle/extract/webhook_sink.py"])
    command = phases[0][0].commands[0].argv
    assert "tests/middle/test_webhook_sink.py" in command
    assert "tests/contract/test_sink_ingest.py" in command
    assert "tests/middle/test_increment.py" in command


def test_shared_store_change_falls_back_to_full_backend():
    # shared/store 是抽取与推送的公共底座,无法限定模块时全量回归
    phases = _quick(["data2agent/shared/store/landing.py"])
    assert "Python" in _task_names(phases)


def test_admin_change_selects_admin_tests():
    phases = _quick(["data2agent/middle/admin/app.py"])
    assert _task_names(phases) == {"Python affected"}
    assert "tests/middle/test_middle_admin.py" in phases[0][0].commands[0].argv


def test_unknown_file_falls_back_to_full_backend():
    phases = _quick(["config/custom.runtime"])
    assert _task_names(phases) == {"Python"}


def test_test_infrastructure_change_falls_back_to_full_backend():
    phases = _quick(["tests/conftest.py"])
    assert _task_names(phases) == {"Python"}


def test_verify_script_change_runs_its_own_tests():
    phases = _quick(["scripts/verify.py"])
    assert _task_names(phases) == {"Python affected"}
    assert "tests/test_verify.py" in phases[0][0].commands[0].argv


def test_full_plan_runs_backend_with_smoke_checks():
    args = Namespace(
        mode="full",
        module=None,
        base="HEAD",
        workers="2",
    )
    phases = verify.build_plan(args)
    assert [[task.name for task in phase] for phase in phases] == [["Python"]]
    argv = [c.argv for c in phases[0][0].commands]
    assert any("scripts/check_architecture_layers.py" in a for a in argv)
