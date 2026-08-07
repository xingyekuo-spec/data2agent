"""Shared YAML whitelist merge, backup, and optional validation for admin UIs."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from .suggestions import field_error

MIDDLE_EDITABLE = {
    "templates",
    "landing",
    "sources.*.windows",
    "sources.*.rate.batch_size",
    "sources.*.rate.rows_per_second",
    "sources.*.lookback",
    "sources.*.sync_every",
    "sources.*.sync_start_at",
    "sources.*.start_date",
    "sources.*.reconcile_at",
    "sources.*.reconcile_deep_at",
    "sources.*.reconcile_deep_day_of_week",
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
    # 微秒避免同一进程内连续保存时覆盖备份；调用方负责 revision 并发控制。
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = path.with_suffix(path.suffix + f".bak-{timestamp}")
    shutil.copy2(path, backup_path)

    merged = copy.deepcopy(data)

    for dotted, value in _flatten(patch):
        if _is_editable(dotted, editable):
            _set_path(merged, dotted, value)

    # 写入临时文件,验证后原子替换(源文件已备份,可用于灾难恢复)
    yaml_text = yaml.dump(merged, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        suffix=".yaml", dir=str(path.parent), prefix=".tmp-")
    tmp_path = Path(tmp_path_str)
    try:
        os.write(tmp_fd, yaml_text.encode("utf-8"))
        os.close(tmp_fd)
        if validate is not None:
            validate(tmp_path)
        os.replace(tmp_path_str, str(path))
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        try:
            os.close(tmp_fd)
        except Exception:
            pass
        return False, [field_error(
            "",
            str(e),
            "根据报错修正 connect.yaml 中对应字段后重新保存",
        )]

    return True, []
