"""同步报告结构;编排见 increment.py。"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    paused: bool = False        # 错峰窗口越界,批次边界优雅暂停

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)
