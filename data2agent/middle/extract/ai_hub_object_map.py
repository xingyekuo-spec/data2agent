"""Map a physical source table row to an AI Hub object envelope.

Physical schema/table/column names stay in middle-machine config. The
platform only receives object_type / object_id / version / payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ...shared.store.landing import normalize_value
from ...shared.store.table import TableInfo


@dataclass(frozen=True)
class ObjectBinding:
    object_type: str
    payload_contract_version: str
    schema_fingerprint: str
    payload_columns: tuple[str, ...]
    delete_flag_column: str | None = None


class ObjectMappingError(ValueError):
    """Configured mapping cannot produce a stable object envelope."""


_DELETE_TRUE = frozenset({"1", "true", "t", "yes", "y"})
_DELETE_FALSE = frozenset({"0", "false", "f", "no", "n"})


def object_id_from_parts(parts: list[str]) -> str:
    """Encode PK parts injectively. Concatenating with '|' is not injective."""
    return json.dumps(list(parts), ensure_ascii=True, separators=(",", ":"))


def object_id_from_row(info: TableInfo, row: Mapping[str, Any]) -> str:
    if not info.pk:
        raise ObjectMappingError(f"{info.name}: AI Hub object_id 需要稳定主键")
    parts: list[str] = []
    for key in info.pk:
        if key not in row or row[key] is None:
            raise ObjectMappingError(f"{info.name}: 主键 {key} 为空,无法生成 object_id")
        text = str(normalize_value(row[key]))
        if not text.strip():
            raise ObjectMappingError(f"{info.name}: 主键 {key} 为空,无法生成 object_id")
        parts.append(text)
    object_id = object_id_from_parts(parts)
    if len(object_id) > 200:
        raise ObjectMappingError(f"{info.name}: object_id 超过 200 字符")
    return object_id


def _is_deleted(info: TableInfo, row: Mapping[str, Any], binding: ObjectBinding) -> bool:
    column = binding.delete_flag_column
    if not column:
        return False
    if column not in dict(info.columns):
        raise ObjectMappingError(
            f"{info.name}: delete_flag_column '{column}' 不在表结构中"
        )
    if column not in row:
        raise ObjectMappingError(
            f"{info.name}: 行缺少删除标志列 {column}"
        )
    value = row[column]
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        raise ObjectMappingError(
            f"{info.name}: 删除标志 {column}={value!r} 不是 0/1"
        )
    if isinstance(value, float):
        if value == 1.0:
            return True
        if value == 0.0:
            return False
        raise ObjectMappingError(
            f"{info.name}: 删除标志 {column}={value!r} 不是 0/1"
        )
    if value is None:
        raise ObjectMappingError(
            f"{info.name}: 删除标志 {column} 为空,无法判定"
        )
    token = str(value).strip().casefold()
    if token in _DELETE_TRUE:
        return True
    if token in _DELETE_FALSE:
        return False
    raise ObjectMappingError(
        f"{info.name}: 删除标志 {column}={value!r} 不在允许集合 "
        f"(0/1/true/false/yes/no/y/n)"
    )


def map_row(
    info: TableInfo,
    row: Mapping[str, Any],
    binding: ObjectBinding,
    version: int,
) -> dict[str, Any]:
    unknown = [
        column for column in binding.payload_columns if column not in dict(info.columns)
    ]
    if unknown:
        raise ObjectMappingError(
            f"{info.name}: payload_columns 含未登记列 {unknown}"
        )
    object_id = object_id_from_row(info, row)
    if _is_deleted(info, row, binding):
        return {
            "object_id": object_id,
            "operation": "delete",
            "version": version,
            "payload": None,
        }
    payload: dict[str, Any] = {}
    for column in binding.payload_columns:
        if column in row:
            payload[column] = normalize_value(row.get(column))
    return {
        "object_id": object_id,
        "operation": "upsert",
        "version": version,
        "payload": payload,
    }
