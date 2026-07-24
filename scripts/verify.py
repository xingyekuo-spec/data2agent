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
FRONTEND = ROOT / "console-ui"

MODULE_TESTS: dict[str, tuple[str, ...]] = {
    "erp": (
        "tests/test_config_scheduler.py",
        "tests/test_connect.py",
        "tests/test_excel_import.py",
        "tests/test_home_setup.py",
        "tests/test_increment.py",
        "tests/test_middle_admin.py",
        "tests/test_reconcile.py",
        "tests/test_sink_ingest.py",
        "tests/test_table_config.py",
    ),
    "console": (
        "tests/test_admin_common.py",
        "tests/test_console*.py",
        "tests/test_datasets_api.py",
        "tests/test_middle_admin.py",
        "tests/test_ui_launcher.py",
    ),
    "mcp": (
        "tests/test_mcp*.py",
    ),
    "metamodel": (
        "tests/test_dataset_publish_contract.py",
        "tests/test_lineage*.py",
        "tests/test_mapping*.py",
        "tests/test_metamodel.py",
        "tests/test_version*.py",
    ),
    "scenario": (
        "tests/test_dead_stock*.py",
        "tests/test_validation_m6.py",
    ),
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


def _npm() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


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
                _cmd(
                    _python(),
                    "scripts/export_console_openapi.py",
                    "--check",
                    "console-ui/openapi.json",
                ),
                _cmd(_python(), "scripts/smoke_admin_ui.py"),
                _cmd(
                    _python(),
                    "-m",
                    "data2agent.metamodel.validate",
                    "templates",
                ),
            )
        )
    return Task("Python", tuple(commands))


def _frontend_quality(*, changed_base: str | None = None, build: bool) -> Task:
    commands = [
        _cmd(_npm(), "run", "api:check", cwd=FRONTEND),
        _cmd(_npm(), "run", "lint", cwd=FRONTEND),
        _cmd(_npm(), "run", "typecheck", cwd=FRONTEND),
    ]
    if changed_base:
        commands.append(
            _cmd(
                _npm(),
                "run",
                "test",
                "--",
                "--changed",
                changed_base,
                "--passWithNoTests",
                cwd=FRONTEND,
            )
        )
    else:
        commands.append(_cmd(_npm(), "run", "test", cwd=FRONTEND))
    if build:
        commands.extend(
            (
                _cmd(_npm(), "run", "build", cwd=FRONTEND),
                _cmd("node", "scripts/check-dist.mjs", cwd=FRONTEND),
            )
        )
    return Task("Vue Console", tuple(commands))


def _e2e(kind: str) -> Task:
    return Task(
        f"E2E {kind}",
        (
            _cmd(
                "node",
                "scripts/e2e-acceptance.mjs",
                f"--{kind.lower()}",
                cwd=FRONTEND,
                env={"D2A_PYTHON": _python()},
            ),
        ),
    )


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
    base: str,
    workers: str,
) -> list[list[Task]]:
    backend_scopes: set[str] = set()
    explicit_tests: set[str] = set()
    frontend = False
    frontend_build = False
    template_validation = False
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
        if parts[0] == "console-ui":
            frontend = True
            if len(parts) < 2 or parts[1] in {
                "package.json",
                "package-lock.json",
                "vite.config.ts",
                "vitest.config.ts",
            }:
                frontend_build = True
            continue
        if parts[0] == "templates":
            backend_scopes.add("metamodel")
            template_validation = True
            continue
        if parts[0] == "data2agent":
            if len(parts) < 2 or parts[1] == "__init__.py":
                backend_full = True
            elif parts[1] == "connect":
                backend_scopes.add("erp")
            elif parts[1] in {"console", "admin_common", "middle_admin"}:
                backend_scopes.add("console")
                frontend = True
            elif parts[1] == "mcp_server":
                backend_scopes.add("mcp")
            elif parts[1] == "metamodel":
                backend_scopes.add("metamodel")
                template_validation = True
            elif parts[1] == "scenarios":
                backend_scopes.add("scenario")
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
        if parts[0] == "deploy" or filename in {
            "tests/integration/mssql/docker-compose.yml",
        }:
            frontend = True
            frontend_build = True
            continue
        if parts[0] == ".github":
            continue

        # Unknown application/configuration files must fail safe.
        backend_full = True
        frontend = True
        frontend_build = True

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
            if template_validation:
                commands.append(
                    _cmd(
                        _python(),
                        "-m",
                        "data2agent.metamodel.validate",
                        "templates",
                    )
                )
            tasks.append(Task("Python affected", tuple(commands)))

    if frontend:
        tasks.append(
            _frontend_quality(changed_base=base, build=frontend_build)
        )
    return [tasks] if tasks else []


def build_plan(args: argparse.Namespace) -> list[list[Task]]:
    if args.mode == "quick":
        files = changed_files(args.base)
        print(f"Changed files ({len(files)}):")
        for filename in files:
            print(f"  {filename}")
        return _quick_plan(files, base=args.base, workers=args.workers)

    if args.mode == "module":
        if args.module == "backend":
            return [[_backend_checks(workers=args.workers, include_smoke=True)]]
        if args.module == "frontend":
            return [
                [_frontend_quality(build=True)],
                [_e2e("Mock")],
            ]
        targets = _expand_tests(MODULE_TESTS[args.module])
        tasks = [
            _backend_checks(
                targets=targets,
                workers=args.workers,
                include_smoke=args.module in {"console", "metamodel"},
            )
        ]
        if args.module == "console":
            tasks.append(_frontend_quality(build=False))
            return [tasks, [_e2e("Mock")]]
        return [tasks]

    phases: list[list[Task]] = [
        [
            _backend_checks(workers=args.workers, include_smoke=True),
            _frontend_quality(build=True),
        ],
        [_e2e("Mock"), _e2e("Real")],
    ]
    if args.mode == "release":
        phases.extend(
            (
                [
                    Task(
                        "Docker distribution",
                        (
                            _cmd(
                                _python(),
                                "scripts/check_docker_v1_distribution.py",
                                "--image",
                                "data2agent:release-check",
                            ),
                        ),
                    )
                ],
                [
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
                ],
            )
        )
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
        choices=("backend", "erp", "console", "mcp", "metamodel", "scenario", "frontend"),
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
        help="run independent Python/frontend tasks serially",
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
        if shutil.which(_npm()) is None and args.mode in {"full", "release"}:
            raise SystemExit("npm is required for full/release verification")
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
