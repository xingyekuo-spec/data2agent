"""Read log file tail with optional level keyword filter."""

from __future__ import annotations

from pathlib import Path

_MAX_LINES = 1000


def tail_lines(path: Path, lines: int = 200, level: str | None = None) -> tuple[bool, str]:
    """Return (ok, text). Missing/unreadable files return ok=False with a message."""
    if lines < 1:
        lines = 1
    if lines > _MAX_LINES:
        lines = _MAX_LINES

    if not path.exists():
        return False, f"Log file not found: {path}"

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"Cannot read log file {path}: {e}"

    all_lines = raw.splitlines()
    tail = all_lines[-lines:] if lines < len(all_lines) else all_lines

    if level:
        needle = level.upper()
        tail = [line for line in tail if needle in line.upper()]

    return True, "\n".join(tail)
