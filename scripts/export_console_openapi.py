#!/usr/bin/env python3
"""Deterministically export or check the console OpenAPI snapshot.

Usage:
  python scripts/export_console_openapi.py console-ui/openapi.json
  python scripts/export_console_openapi.py --check console-ui/openapi.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def dump_openapi() -> str:
    from data2agent.platform.console.app import create_app

    # Landing path is never opened during OpenAPI generation; keep it ephemeral.
    with tempfile.TemporaryDirectory(prefix="d2a-openapi-") as td:
        landing = Path(td) / "landing.sqlite"
        app = create_app(landing=str(landing), templates=str(ROOT / "templates"))
        spec = app.openapi()
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=str(ROOT / "console-ui" / "openapi.json"),
        help="Snapshot path (default: console-ui/openapi.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if snapshot differs from live OpenAPI",
    )
    args = parser.parse_args(argv)
    target = Path(args.path)
    if not target.is_absolute():
        target = ROOT / target

    live = dump_openapi()
    if args.check:
        if not target.is_file():
            print(
                f"OpenAPI snapshot missing: {target}\n"
                f"Regenerate with:\n"
                f"  python scripts/export_console_openapi.py {target}",
                file=sys.stderr,
            )
            return 1
        existing = target.read_text(encoding="utf-8")
        if existing != live:
            print(
                f"OpenAPI snapshot drift detected: {target}\n"
                f"Regenerate with:\n"
                f"  python scripts/export_console_openapi.py {target}",
                file=sys.stderr,
            )
            return 1
        print(f"OpenAPI snapshot OK: {target}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(live, encoding="utf-8")
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
