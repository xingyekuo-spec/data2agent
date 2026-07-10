"""同步报告结构与全量入口;增量编排见 increment.py。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..metamodel.schema import TemplatePack
from .adapters.base import SourceAdapter
from .landing import LandingStore


def whitelist_from_pack(pack: TemplatePack, source: str) -> set[str]:
    """白名单由模板 binding 自动推导 —— 元模型作为唯一事实来源。"""
    return {
        t
        for o in pack.objects
        for b in o.bindings
        if b.source == source
        for t in b.tables
    }


@dataclass
class TableReport:
    table: str
    rows: int
    batches: int
    batch_id: str
    strategy: str = "full_refresh"
    high_water: str | None = None


@dataclass
class SyncReport:
    source: str
    run_id: int
    tables: list[TableReport] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)


def full_sync(adapter: SourceAdapter, landing: LandingStore, source: str) -> SyncReport:
    """全量同步 = 所有表按 full_refresh 策略的增量编排(不建立水位)。"""
    from .increment import incremental_sync

    return incremental_sync(adapter, landing, source, watermarks={})
