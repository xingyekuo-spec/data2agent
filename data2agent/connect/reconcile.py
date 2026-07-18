"""分段对账(E3):抓水位增量抓不住的两类漂移 —— 源侧物理删除、不更新水位的原地改动。

协议(docs/design/02-extraction.md §6):
- 按水位自然月分段;L1(廉价,每日可跑):源侧段内 COUNT+MAX vs 落地侧同口径;
- L2(修复,对不一致段或 --deep 全段):重抽该段(upsert 幂等,顺带修正原地改动、
  复活误删行),源侧消失的主键打 _d2a_deleted_at 软删标记;
- 无水位表(小维表):整表 count 对比,L2 = 全量重抽 + 主键 diff;
- 已知边界:原地改动若不改水位,L1 的 COUNT+MAX 察觉不到,须靠 --deep 兜底。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .adapters.base import SourceAdapter, TableInfo
from .landing import LandingStore, normalize_value


@dataclass
class SegmentResult:
    table: str
    segment: str                 # "YYYY-MM" 或 "全表"
    src_count: int
    dst_count: int
    consistent: bool             # L1 结论(修复前)
    repaired_rows: int = 0
    soft_deleted: int = 0


@dataclass
class ReconcileReport:
    source: str
    run_id: int
    deep: bool
    segments: list[SegmentResult] = field(default_factory=list)

    @property
    def mismatched(self) -> list[SegmentResult]:
        return [s for s in self.segments if not s.consistent]

    @property
    def total_soft_deleted(self) -> int:
        return sum(s.soft_deleted for s in self.segments)


def month_segments(min_wm: str, max_wm: str) -> list[tuple[str, str, str]]:
    """[(标签, 段起, 段止)],边界用完整 datetime 字符串(源侧 DATETIME 列可直接比较)。"""
    y, m = int(min_wm[:4]), int(min_wm[5:7])
    end_y, end_m = int(max_wm[:4]), int(max_wm[5:7])
    out = []
    while (y, m) <= (end_y, end_m):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y:04d}-{m:02d}",
                    f"{y:04d}-{m:02d}-01 00:00:00", f"{ny:04d}-{nm:02d}-01 00:00:00"))
        y, m = ny, nm
    return out


def _repair_segment(adapter: SourceAdapter, landing: LandingStore, source: str,
                    info: TableInfo, wm_col: str, start, end) -> tuple[int, int]:
    """L2:重抽段 + 主键 diff 软删。返回 (重抽行数, 软删行数)。"""
    batch_id = uuid.uuid4().hex[:12]
    pk = info.pk[0]
    src_pks: set = set()
    rows = 0
    for batch in adapter.read_segment(info, wm_col, start, end):
        rows += landing.upsert_rows(source, info, batch, batch_id)
        src_pks |= {r[pk] for r in batch}
    gone = landing.active_pks(source, info.name, pk, wm_col, start, end) - src_pks
    return rows, landing.mark_deleted(source, info.name, pk, gone)


def _repair_full(adapter: SourceAdapter, landing: LandingStore, source: str,
                 info: TableInfo) -> tuple[int, int]:
    """无水位表的 L2:全量重抽 + 主键 diff 软删。"""
    batch_id = uuid.uuid4().hex[:12]
    pk = info.pk[0]
    src_pks: set = set()
    rows = 0
    for batch in adapter.read_increment(info):
        rows += landing.upsert_rows(source, info, batch, batch_id)
        src_pks |= {r[pk] for r in batch}
    gone = landing.active_pks(source, info.name, pk) - src_pks
    return rows, landing.mark_deleted(source, info.name, pk, gone)


def reconcile(adapter: SourceAdapter, landing: LandingStore, source: str,
              watermarks: dict[str, str] | None = None, deep: bool = False) -> ReconcileReport:
    watermarks = watermarks or {}
    report = ReconcileReport(source=source, run_id=landing.start_run(source, "reconcile"), deep=deep)
    try:
        for info in adapter.tables():
            wm_col = watermarks.get(info.name)
            if wm_col is None:
                _reconcile_full_table(adapter, landing, source, info, deep, report)
            else:
                _reconcile_by_month(adapter, landing, source, info, wm_col, deep, report)
    except Exception as e:
        landing.finish_run(report.run_id, tables=0, rows=0, status="failed", detail=str(e))
        raise
    landing.finish_run(
        report.run_id, tables=len({s.table for s in report.segments}),
        rows=sum(s.repaired_rows for s in report.segments),
        status="ok",
        detail=f"reconcile{'-deep' if deep else ''}: "
               f"{len(report.mismatched)} 段不一致, 软删 {report.total_soft_deleted} 行")
    return report


def _reconcile_by_month(adapter, landing, source, info: TableInfo, wm_col: str,
                        deep: bool, report: ReconcileReport) -> None:
    min_wm = landing.min_watermark(source, info.name, wm_col)
    high = landing.get_high_water(source, info.name)
    if min_wm is None or high is None:
        return  # 尚未同步过,无从对账
    max_wm = max(high, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    for label, start, end in month_segments(min_wm, max_wm):
        src = adapter.segment_stats(info, wm_col, start, end)
        dst = landing.segment_stats(source, info.name, wm_col, start, end)
        src_max = normalize_value(src["max"])
        consistent = src["count"] == dst["count"] and src_max == dst["max"]
        if src["count"] == 0 and dst["count"] == 0:
            continue  # 双侧空段不进报告
        seg = SegmentResult(info.name, label, src["count"], dst["count"], consistent)
        if not consistent or deep:
            seg.repaired_rows, seg.soft_deleted = _repair_segment(
                adapter, landing, source, info, wm_col, start, end)
        report.segments.append(seg)


def _reconcile_full_table(adapter, landing, source, info: TableInfo,
                          deep: bool, report: ReconcileReport) -> None:
    src_count = adapter.table_count(info)
    dst_count = landing.count(source, info.name, active_only=True)
    consistent = src_count == dst_count
    seg = SegmentResult(info.name, "全表", src_count, dst_count, consistent)
    if not consistent or deep:
        seg.repaired_rows, seg.soft_deleted = _repair_full(adapter, landing, source, info)
    report.segments.append(seg)
