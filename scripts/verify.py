#!/usr/bin/env python3
"""Layered local verification entry point.

Examples:
    python scripts/verify.py quick
    python scripts/verify.py quick --base origin/main
    python scripts/verify.py module erp
    python scripts/verify.py full
    python scripts/verify.py release
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]

MODULE_TESTS: dict[str, tuple[str, ...]] = {
    "erp": (
        "tests/middle/test_config_scheduler.py",
        "tests/middle/test_connect.py",
        "tests/shared/test_home_setup.py",
        "tests/middle/test_increment.py",
        "tests/middle/test_middle_admin.py",
        "tests/middle/test_reconcile.py",
        "tests/middle/test_table_config.py",
    ),
    "push": (
        "tests/contract/test_sink_ingest.py",
        "tests/contract/test_ingest_protocol_compat.py",
        "tests/contract/test_ai_hub_object_push_sink.py",
        "tests/middle/test_webhook_sink.py",
    ),
    "admin": (
        "tests/shared/test_admin_common.py",
        "tests/middle/test_middle_admin.py",
        "tests/middle/test_middle_admin_production.py",
        "tests/middle/test_middle_extraction_tables.py",
        "tests/middle/test_middle_metadata_api.py",
    ),
}

# 推送出口模块:改动后同时跑推送契约与抽取回归。
PUSH_MODULES = {
    "sink.py", "webhook_sink.py", "ai_hub_object_push_sink.py",
    "ai_hub_object_map.py",
}


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path = ROOT
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Task:
    name: str
    commands: tuple[Command, ...]


def _python() -> str:
    candidates = (
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _cmd(
    *argv: str,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> Command:
    return Command(tuple(argv), cwd, tuple(sorted((env or {}).items())))


def _pytest(
    *targets: str,
    workers: str,
    marker: str | None = None,
    durations: int = 10,
) -> Command:
    argv = [
        _python(),
        "-m",
        "pytest",
        *(targets or ("tests",)),
        "-q",
        "-n",
        workers,
        "--dist",
        "loadscope",
        f"--durations={durations}",
    ]
    if marker:
        argv.extend(("-m", marker))
    return _cmd(*argv)


def _backend_checks(
    *,
    targets: Sequence[str] = (),
    workers: str,
    include_smoke: bool,
) -> Task:
    commands = [
        _pytest(
            *targets,
            workers=workers,
            marker=None if targets else "not integration",
            durations=20 if not targets else 10,
        )
    ]
    if include_smoke:
        commands.extend(
            (
                _cmd(_python(), "scripts/check_architecture_layers.py"),
                _cmd(_python(), "scripts/smoke_admin_ui.py"),
            )
        )
    return Task("Python", tuple(commands))


def _expand_tests(patterns: Iterable[str]) -> tuple[str, ...]:
    expanded: set[str] = set()
    for pattern in patterns:
        if "*" in pattern:
            expanded.update(
                path.relative_to(ROOT).as_posix() for path in ROOT.glob(pattern)
            )
        elif (ROOT / pattern).exists():
            expanded.add(pattern)
    return tuple(sorted(expanded))


def _git_lines(*args: str) -> set[str]:
    result = subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return {line for line in result.stdout.splitlines() if line}


def changed_files(base: str) -> tuple[str, ...]:
    """Return committed, staged, unstaged, and untracked changes."""

    changed: set[str] = set()
    if base != "HEAD":
        subprocess.run(
            ("git", "rev-parse", "--verify", f"{base}^{{commit}}"),
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        changed.update(
            _git_lines(
                "diff",
                "--name-only",
                "--diff-filter=ACMRTUXB",
                f"{base}...HEAD",
            )
        )
    changed.update(
        _git_lines("diff", "--name-only", "--diff-filter=ACMRTUXB")
    )
    changed.update(
        _git_lines("diff", "--cached", "--name-only", "--diff-filter=ACMRTUXB")
    )
    changed.update(_git_lines("ls-files", "--others", "--exclude-standard"))
    return tuple(sorted(changed))


def _quick_plan(
    files: Sequence[str],
    *,
    workers: str,
) -> list[list[Task]]:
    backend_scopes: set[str] = set()
    explicit_tests: set[str] = set()
    backend_full = False

    for filename in files:
        path = Path(filename)
        parts = path.parts

        if not parts:
            continue
        if parts[0] == "docs" or filename in {
            "README.md",
            "CONTRIBUTING.md",
            "NOTICE",
            "LICENSE",
            "SECURITY.md",
        }:
            continue
        if parts[0] == "tests":
            if path.name.startswith("test_") and filename.endswith(".py"):
                explicit_tests.add(filename)
            else:
                backend_full = True
            continue
        if parts[0] == "data2agent":
            if len(parts) < 2 or parts[1] == "__init__.py":
                backend_full = True
            elif parts[1] == "protocol":
                # 跨机契约:影响两端,全量回归
                backend_full = True
            elif parts[1] == "middle":
                if len(parts) > 2 and parts[2] == "admin":
                    backend_scopes.add("admin")
                elif path.name in PUSH_MODULES:
                    backend_scopes.update(("push", "erp"))
                else:
                    backend_scopes.add("erp")
            elif parts[1] == "shared":
                sub = parts[2] if len(parts) > 2 else ""
                if sub in {"admin", "admin_templates"}:
                    backend_scopes.add("admin")
                else:
                    # store / config 是抽取与推送的公共底座,全量回归
                    backend_full = True
            else:
                backend_full = True
            continue
        if filename == "pyproject.toml":
            backend_full = True
            continue
        if parts[0] == "scripts":
            if filename == "scripts/verify.py":
                explicit_tests.add("tests/test_verify.py")
            else:
                backend_full = True
            continue
        if parts[0] in {"deploy", ".github"}:
            continue

        # Unknown application/configuration files must fail safe.
        backend_full = True

    tasks: list[Task] = []
    if backend_full:
        tasks.append(
            _backend_checks(workers=workers, include_smoke=True)
        )
    else:
        patterns = set(explicit_tests)
        for scope in backend_scopes:
            patterns.update(MODULE_TESTS[scope])
        targets = _expand_tests(patterns)
        if targets:
            commands = [
                _pytest(*targets, workers=workers, durations=10)
            ]
            tasks.append(Task("Python affected", tuple(commands)))
    return [tasks] if tasks else []


def build_plan(args: argparse.Namespace) -> list[list[Task]]:
    if args.mode == "quick":
        files = changed_files(args.base)
        print(f"Changed files ({len(files)}):")
        for filename in files:
            print(f"  {filename}")
        return _quick_plan(files, workers=args.workers)

    if args.mode == "module":
        if args.module == "backend":
            return [[_backend_checks(workers=args.workers, include_smoke=True)]]
        targets = _expand_tests(MODULE_TESTS[args.module])
        return [[
            _backend_checks(
                targets=targets,
                workers=args.workers,
                include_smoke=args.module == "admin",
            )
        ]]

    phases: list[list[Task]] = [
        [_backend_checks(workers=args.workers, include_smoke=True)],
    ]
    if args.mode == "release":
        phases.append([
            Task(
                "MSSQL integration",
                (
                    _cmd(
                        "docker",
                        "compose",
                        "-f",
                        "tests/integration/mssql/docker-compose.yml",
                        "up",
                        "--build",
                        "--abort-on-container-exit",
                        "--exit-code-from",
                        "runner",
                    ),
                ),
            )
        ])
    return phases


def _format(command: Command) -> str:
    prefix = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in command.env
    )
    rendered = shlex.join(command.argv)
    return f"{prefix} {rendered}".strip()


def _run_task(task: Task, *, dry_run: bool) -> None:
    print(f"\n== {task.name} ==")
    for command in task.commands:
        shown_cwd = command.cwd.relative_to(ROOT) if command.cwd != ROOT else Path(".")
        print(f"[{shown_cwd}] {_format(command)}", flush=True)
        if not dry_run:
            env = os.environ.copy()
            env.update(command.env)
            subprocess.run(command.argv, cwd=command.cwd, env=env, check=True)


def run_plan(
    phases: Sequence[Sequence[Task]],
    *,
    dry_run: bool,
    serial: bool,
) -> None:
    if not phases:
        print("No application changes detected; no tests are required.")
        return

    for phase in phases:
        if serial or dry_run or len(phase) == 1:
            for task in phase:
                _run_task(task, dry_run=dry_run)
            continue
        with ThreadPoolExecutor(max_workers=len(phase)) as pool:
            futures = [
                pool.submit(_run_task, task, dry_run=False) for task in phase
            ]
            for future in futures:
                future.result()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("quick", "module", "full", "release"),
        help="verification depth",
    )
    parser.add_argument(
        "module",
        nargs="?",
        choices=("backend", "erp", "push", "admin"),
        help="module name when mode=module",
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("D2A_VERIFY_BASE", "HEAD"),
        help="Git base used by quick mode (default: HEAD)",
    )
    parser.add_argument(
        "--workers",
        default=os.environ.get("D2A_PYTEST_WORKERS", "auto"),
        help="pytest-xdist worker count (default: auto)",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="run independent tasks serially",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print selected commands without running them",
    )
    args = parser.parse_args(argv)
    if args.mode == "module" and not args.module:
        parser.error("module mode requires a module name")
    if args.mode != "module" and args.module:
        parser.error("a module name is only valid with module mode")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.dry_run:
        if shutil.which("git") is None:
            raise SystemExit("git is required")
    try:
        phases = build_plan(args)
        run_plan(phases, dry_run=args.dry_run, serial=args.serial)
    except subprocess.CalledProcessError as exc:
        print(
            f"\nVerification failed (exit {exc.returncode}): "
            f"{shlex.join(str(part) for part in exc.cmd)}",
            file=sys.stderr,
        )
        return exc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
