"""源适配器:安全强制(只读 / 白名单 / 限流 / 审计)全部在适配器层实现,不可绕过。"""

from .base import ReadOnlyViolation, SourceAdapter, TableInfo, WhitelistViolation
from .sqlite import SqliteReadOnlyAdapter

__all__ = [
    "SourceAdapter", "TableInfo",
    "ReadOnlyViolation", "WhitelistViolation",
    "SqliteReadOnlyAdapter",
]
