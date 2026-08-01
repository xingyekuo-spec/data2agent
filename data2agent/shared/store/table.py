"""表结构与运行键的共享数据模型:落地层与只读适配器共同依赖的纯数据定义。

本模块属于共享领域层(shared),不得依赖任何端目录(middle / platform)。
适配器安全强制(白名单 / 只读 / 限流 / 审计)见 middle.extract.adapters.base。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

Column = tuple[str, str]  # (列名, 可移植类型 int/real/text/blob)


class RuntimeKeyError(ValueError):
    """配置运行键无效:缺列、空键或存在 NULL。"""


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[Column]
    pk: list[str]          # 运行键(落地 upsert / 增量 keyset 依据)
    key_source: str = "database_pk"  # database_pk | configured
    schema: str | None = None


def encode_keyset_cursor(watermark, key_values: list | None) -> str:
    """统一序列化增量游标。

    key_values=None 表示该水位上整表已完成;list 表示已完成到该键边界(含)。
    """
    return json.dumps({"w": watermark, "k": key_values}, ensure_ascii=False, default=str)


def decode_keyset_cursor(raw: str) -> tuple[object, list | None]:
    """解码游标。兼容旧版纯水位字符串(视为已完成)。"""
    text = raw.strip()
    if text.startswith("{"):
        data = json.loads(text)
        return data["w"], data.get("k")
    return text, None


def apply_configured_keys(info: TableInfo, key_columns: list[str]) -> TableInfo:
    """用配置业务键覆盖数据库主键;校验列存在且不重复。"""
    if not key_columns:
        raise RuntimeKeyError(f"{info.name}: key_columns 不能为空")
    if len(key_columns) != len(set(key_columns)):
        raise RuntimeKeyError(f"{info.name}: key_columns 不得包含重复列")
    known = {c for c, _ in info.columns}
    missing = [c for c in key_columns if c not in known]
    if missing:
        raise RuntimeKeyError(
            f"{info.name}: 配置键列不存在: {', '.join(missing)}")
    return replace(info, pk=list(key_columns), key_source="configured")


def resolve_runtime_keys(
    info: TableInfo,
    key_columns: list[str] | None,
    *,
    require_keys: bool = True,
) -> TableInfo:
    """解析运行键:配置优先,否则使用数据库 PK。"""
    if key_columns:
        return apply_configured_keys(info, key_columns)
    if require_keys and not info.pk:
        raise RuntimeKeyError(
            f"{info.name}: 无数据库主键且未配置 key_columns,无法幂等落地/增量")
    return replace(info, key_source="database_pk")
