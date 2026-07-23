"""Moved from data2agent.connect.sync / increment — test-only utilities.

Production code no longer derives whitelist or watermarks from template packs.
Use `SourceConfig.table_whitelist()` and `SourceConfig.table_watermarks()` instead.
"""

from __future__ import annotations

from data2agent.mapping import parse_field_expr
from data2agent.metamodel.schema import TemplatePack


def whitelist_from_pack(pack: TemplatePack, source: str) -> set[str]:
    """Derive whitelist table names from template pack bindings (test utility)."""
    return {
        t
        for o in pack.objects
        for b in o.bindings
        if b.source == source and b.enabled
        for t in b.source_tables
    }


def watermarks_from_pack(pack: TemplatePack, source: str) -> dict[str, str]:
    """Derive watermark column mapping from template pack (test utility)."""
    out: dict[str, str] = {}
    for o in pack.objects:
        for b in o.bindings:
            if b.source != source or not b.watermark or not b.enabled:
                continue
            if b.materializer:
                continue
            expr = parse_field_expr(b.watermark)
            if expr.table in out and out[expr.table] != expr.column:
                raise ValueError(
                    f"表 {expr.table} 的水位列声明冲突:{out[expr.table]} vs {expr.column}")
            out[expr.table] = expr.column
    return out
