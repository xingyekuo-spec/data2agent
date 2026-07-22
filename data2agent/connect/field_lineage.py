"""字段级血缘:规范对象键、key token 与值证据序列化。

M4-T01 先冻结 key token 与 ValueEvidence 口径;持久化与 published 查询在后续任务接入。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

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
