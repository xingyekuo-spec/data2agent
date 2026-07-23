"""Shared YAML whitelist merge, backup, and optional validation for admin UIs."""

from __future__ import annotations

import copy
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

MIDDLE_EDITABLE = {
    "templates",
    "landing",
    "sources.*.windows",
    "sources.*.rate.batch_size",
    "sources.*.rate.rows_per_second",
    "sources.*.lookback",
    "sources.*.sync_every",
    "sources.*.sink.url",
}

PLATFORM_EDITABLE = {"templates", "landing"}


def _path_matches(path: str, pattern: str) -> bool:
    path_parts = path.split(".")
    pattern_parts = pattern.split(".")
    if len(path_parts) != len(pattern_parts):
        return False
    for segment, pat in zip(path_parts, pattern_parts, strict=True):
        if pat == "*":
            continue
        if segment != pat:
            return False
    return True


def _is_editable(path: str, editable: set[str]) -> bool:
    return any(_path_matches(path, pattern) for pattern in editable)


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.extend(_flatten(value, dotted))
        else:
            items.append((dotted, value))
    return items


def _set_path(root: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = root
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def merge_whitelist_and_save(
    path: Path,
    editable: set[str],
    patch: dict[str, Any],
    validate: Callable[[Path], None] | None,
) -> tuple[bool, list[dict[str, str]]]:
    """Merge only editable fields from patch; backup; optionally validate(path).

    On validation failure, restore from backup and return (False, errors).
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{timestamp}")
    shutil.copy2(path, backup_path)

    merged = copy.deepcopy(data)

    for dotted, value in _flatten(patch):
        if _is_editable(dotted, editable):
            _set_path(merged, dotted, value)

    path.write_text(
        yaml.dump(merged, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    if validate is not None:
        try:
            validate(path)
        except Exception as e:
            shutil.copy2(backup_path, path)
            return False, [{"field": "", "message": str(e)}]

    return True, []
