"""Check release version consistency across Python, frontend, and git tag.

The Python package version in pyproject.toml is the source of truth.  Official
release tags are expected to be the same version with a leading "v".
"""

from __future__ import annotations

import argparse
import json
import os
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"]).strip()


def _imported_version() -> str:
    import data2agent

    return str(data2agent.__version__).strip()


def _metadata_version() -> str | None:
    try:
        return package_version("data2agent").strip()
    except PackageNotFoundError:
        return None


def _console_version() -> str | None:
    package_json = ROOT / "console-ui" / "package.json"
    if not package_json.is_file():
        return None
    data = json.loads(package_json.read_text(encoding="utf-8"))
    value = data.get("version")
    return str(value).strip() if value is not None else None


def _tag_from_env() -> str | None:
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        return os.environ.get("GITHUB_REF_NAME")
    return None


def _normalize_tag(tag: str) -> str:
    return tag.strip().removeprefix("refs/tags/").removeprefix("v")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify release version values are consistent.")
    parser.add_argument(
        "--tag",
        default=None,
        help="Release tag to check, e.g. v0.5.3. Defaults to GitHub tag env.")
    parser.add_argument(
        "--require-console",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require console-ui/package.json version to match pyproject.")
    parser.add_argument(
        "--check-installed-metadata",
        action="store_true",
        help="Also require installed package metadata to match pyproject.")
    args = parser.parse_args(argv)

    expected = _project_version()
    checks: list[tuple[str, str | None, bool]] = [
        ("data2agent.__version__", _imported_version(), True),
        ("console-ui/package.json", _console_version(), args.require_console),
    ]
    if args.check_installed_metadata:
        checks.append(("installed package metadata", _metadata_version(), True))
    tag = args.tag or _tag_from_env()
    if tag:
        checks.append((f"tag {tag}", _normalize_tag(tag), True))

    failures: list[str] = []
    for label, actual, required in checks:
        if actual is None:
            if required:
                failures.append(f"{label}: missing, expected {expected}")
            continue
        if actual != expected:
            failures.append(f"{label}: {actual} != {expected}")

    if failures:
        print("Release version mismatch:", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print(f"Release version ok: {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
