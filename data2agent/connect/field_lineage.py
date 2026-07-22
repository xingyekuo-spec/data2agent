"""字段级血缘:规范对象键、key token 与值证据序列化。

M4-T01 先冻结 key token 与 ValueEvidence 口径;持久化与 published 查询在后续任务接入。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

_LINEAGE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_KEY_SCALARS = (type(None), str, int, float, bool)


class LineageKeyError(ValueError):
    """对象键或 key token 不合法。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def is_valid_lineage_key_token(token: str) -> bool:
    """URL `{key}` 必须是规范 64 位小写 hex SHA-256。"""
    return isinstance(token, str) and bool(_LINEAGE_KEY_RE.fullmatch(token))


def _require_key_scalar(value: Any, *, label: str) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124
            raise LineageKeyError(
                "lineage_key_invalid", f"{label} 不能是非有限浮点")
        return value
    raise LineageKeyError(
        "lineage_key_invalid",
        f"{label} 只允许 null/string/number/bool,得到 {type(value).__name__}",
    )


def canonical_object_key_pairs(
    keys: Sequence[str],
    values: Mapping[str, Any] | Sequence[Any],
) -> list[list[Any]]:
    """按 frozen template `keys` 顺序生成规范化 pair 数组。

    `values` 可以是业务键映射,或与 `keys` 等长的值序列。
    """
    if not keys:
        raise LineageKeyError("lineage_key_invalid", "对象没有业务键")
    if isinstance(values, Mapping):
        pairs = []
        for name in keys:
            if name not in values:
                raise LineageKeyError(
                    "lineage_key_invalid", f"缺少业务键 {name}")
            pairs.append([name, _require_key_scalar(values[name], label=name)])
        return pairs
    if len(values) != len(keys):
        raise LineageKeyError(
            "lineage_key_invalid",
            f"业务键值数量不匹配:期望 {len(keys)},得到 {len(values)}",
        )
    return [
        [name, _require_key_scalar(value, label=name)]
        for name, value in zip(keys, values, strict=True)
    ]


def canonical_object_key_json(
    keys: Sequence[str],
    values: Mapping[str, Any] | Sequence[Any],
) -> str:
    """紧凑 UTF-8 JSON pair 数组,例如 [["order_no","SO-001"],["line_no",10]]。"""
    pairs = canonical_object_key_pairs(keys, values)
    return json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))


def object_key_token(
    keys: Sequence[str],
    values: Mapping[str, Any] | Sequence[Any],
) -> str:
    """完整 SHA-256 hex(64 位小写),用作 URL 不透明定位符。"""
    raw = canonical_object_key_json(keys, values).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def object_key_token_from_pairs(pairs: Sequence[Sequence[Any]]) -> str:
    """已规范化 pair 数组 → key token(仍校验标量类型)。"""
    normalized: list[list[Any]] = []
    for item in pairs:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
            raise LineageKeyError("lineage_key_invalid", "pair 必须是 [name, value]")
        if len(item) != 2:
            raise LineageKeyError("lineage_key_invalid", "pair 必须是二元组")
        name, value = item[0], item[1]
        if not isinstance(name, str) or not name:
            raise LineageKeyError("lineage_key_invalid", "业务键名必须是非空字符串")
        normalized.append([name, _require_key_scalar(value, label=name)])
    raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def require_lineage_key_token(token: str) -> str:
    """校验 URL key token;非法时抛 LineageKeyError(lineage_key_invalid)。"""
    if not is_valid_lineage_key_token(token):
        raise LineageKeyError(
            "lineage_key_invalid",
            "key token 必须是规范 64 位小写十六进制 SHA-256",
        )
    return token


# ---- ValueEvidence / 持久化 DTO (M4-T04) ------------------------------------

LINEAGE_SCHEMA_VERSION = 1
VALUE_BUDGET_BYTES = 64 * 1024
VALUE_PREVIEW_CHARS = 512

TransformKind = Literal["direct", "derived", "unmapped"]


@dataclass(frozen=True)
class FieldLineageNode:
    dataset_version: str
    object_version: str
    object: str
    object_key_json: str
    object_key_hash: str
    property: str
    result_value_json: str
    trace_status: Literal["available", "unavailable"]
    unavailable_reason: str | None
    transform_kind: TransformKind
    transform_steps_json: str
    source: str
    map_batch_id: str
    binding_hash: str
    binding_status: str
    template_version: str


@dataclass(frozen=True)
class FieldLineageInputRow:
    dataset_version: str
    object: str
    object_key_json: str
    property: str
    input_ordinal: int
    role: Literal["value", "join_fk", "derived_condition"]
    source: str | None
    source_table: str | None
    source_pk_json: str | None
    source_column: str | None
    source_value_json: str | None
    extract_batch_id: str | None
    join_json: str | None


def encode_value_evidence(value: Any) -> dict[str, Any]:
    """稳定值证据 JSON(非 repr)。BLOB/超长文本只留摘要。"""
    if value is None:
        return {"kind": "null", "value": None, "preview": None, "sha256": None, "length": None}
    if isinstance(value, bool) or isinstance(value, (int, float)):
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))  # noqa: PLR0124
        ):
            text = str(value)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return {
                "kind": "truncated",
                "value": None,
                "preview": text[:VALUE_PREVIEW_CHARS],
                "sha256": digest,
                "length": len(text),
            }
        return {
            "kind": "scalar",
            "value": value,
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, datetime):
        text = value.isoformat()
        return {
            "kind": "scalar",
            "value": text,
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, date):
        return {
            "kind": "scalar",
            "value": value.isoformat(),
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, Decimal):
        return {
            "kind": "scalar",
            "value": float(value),
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "kind": "bytes",
            "value": None,
            "preview": None,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "length": len(raw),
        }
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= VALUE_BUDGET_BYTES:
            return {
                "kind": "scalar",
                "value": value,
                "preview": None,
                "sha256": None,
                "length": None,
            }
        return {
            "kind": "truncated",
            "value": None,
            "preview": value[:VALUE_PREVIEW_CHARS],
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "length": len(encoded),
        }
    # 其它类型:安全字符串化并按预算截断
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= VALUE_BUDGET_BYTES:
        return {
            "kind": "scalar",
            "value": text,
            "preview": None,
            "sha256": None,
            "length": None,
        }
    return {
        "kind": "truncated",
        "value": None,
        "preview": text[:VALUE_PREVIEW_CHARS],
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "length": len(encoded),
    }


def dumps_value_evidence(value: Any) -> str:
    return json.dumps(
        encode_value_evidence(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
