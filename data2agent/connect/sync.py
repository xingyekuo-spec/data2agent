"""同步编排(E1:全量):白名单推导 → 逐表分批落地 → 运行汇总。"""

from __future__ import annotations

import uuid
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


@dataclass
class SyncReport:
    source: str
    run_id: int
    tables: list[TableReport] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)


def full_sync(adapter: SourceAdapter, landing: LandingStore, source: str) -> SyncReport:
    report = SyncReport(source=source, run_id=landing.start_run(source))
    try:
        for info in adapter.tables():
            landing.ensure_raw_table(source, info)
            batch_id = uuid.uuid4().hex[:12]
            rows = batches = 0
            for batch in adapter.read_increment(info):
                rows += landing.upsert_rows(source, info, batch, batch_id)
                batches += 1
            report.tables.append(TableReport(info.name, rows, batches, batch_id))
    except Exception as e:
        landing.finish_run(report.run_id, tables=len(report.tables),
                           rows=report.total_rows, status="failed", detail=str(e))
        raise
    landing.finish_run(report.run_id, tables=len(report.tables), rows=report.total_rows)
    return report
