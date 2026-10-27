"""Test-only utilities for the E10-like reference source.

模板包与映射层已从中间机移除;测试改用固定的 E10 抽取表清单与水位声明
(与原 templates/ 中 digiwin_e10 绑定推导出的结果一致)。
生产代码使用 `SourceConfig.table_whitelist()` 与 `SourceConfig.table_watermarks()`。
"""

from __future__ import annotations

E10_TABLES: frozenset[str] = frozenset({
    "CURRENCY", "CUSTOMER", "ITEM", "ITEM_WAREHOUSE",
    "QUOTATION", "SALES_ORDER", "SALES_ORDER_D",
})

E10_WATERMARKS: dict[str, str] = {
    "CUSTOMER": "LAST_MODIFIED_DATE",
    "ITEM": "LAST_MODIFIED_DATE",
    "QUOTATION": "LAST_MODIFIED_DATE",
    "SALES_ORDER": "LAST_MODIFIED_DATE",
    "SALES_ORDER_D": "LAST_MODIFIED_DATE",
}


def e10_whitelist() -> set[str]:
    """E10-like 参考源的抽取白名单(每次返回新集合,调用方可修改)。"""
    return set(E10_TABLES)


def e10_watermarks() -> dict[str, str]:
    """E10-like 参考源的增量水位声明(每次返回新字典)。"""
    return dict(E10_WATERMARKS)
