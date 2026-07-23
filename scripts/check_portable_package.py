#!/usr/bin/env python3
"""Fail a portable build when its shipped UI, templates, or ERP profile are stale."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fail(message: str) -> None:
    raise SystemExit(f"portable package check failed: {message}")


def _check_templates(portable: Path, expected: Path) -> None:
    shipped = portable / "app" / "templates"
    for source in expected.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(expected)
        target = shipped / relative
        if not target.is_file():
            _fail(f"template missing: {relative}")
        if _digest(source) != _digest(target):
            _fail(f"template differs from build source: {relative}")
    for required in (
        Path("metrics/dead_stock.yaml"),
        Path("objects/dead_stock_item.yaml"),
        Path("objects/dead_stock_attribution.yaml"),
    ):
        if not (shipped / required).is_file():
            _fail(f"dead-stock template missing: {required}")


def _check_platform_entry(portable: Path) -> None:
    if not (portable / "app" / "console-ui" / "dist" / "index.html").is_file():
        _fail("Vue Console dist/index.html missing")
    app_py = portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "console" / "app.py"
    if not app_py.is_file():
        _fail("installed platform console module missing")
    app_code = app_py.read_text(encoding="utf-8")
    if 'app.mount("/assets", StaticFiles(directory=assets_dir), name="vue-assets")' not in app_code:
        _fail("installed platform wheel does not mount root Vue assets")
    if 'def legacy_v1_index()' not in app_code:
        _fail("installed platform wheel does not retain /v1 compatibility redirect")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portable", type=Path, required=True)
    parser.add_argument("--role", choices=("middle", "platform"), required=True)
    parser.add_argument("--expected-templates", type=Path, required=True)
    args = parser.parse_args()

    portable = args.portable.resolve()
    _check_templates(portable, args.expected_templates.resolve())
    build_info = portable / "BUILD-INFO.json"
    if not build_info.is_file() or '"release_version"' not in build_info.read_text(encoding="utf-8"):
        _fail("portable build label missing")
    if args.role == "platform":
        _check_platform_entry(portable)
    print("portable package check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
