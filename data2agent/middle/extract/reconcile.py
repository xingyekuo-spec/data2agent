"""分段对账(E3):抓水位增量抓不住的两类漂移 —— 源侧物理删除、不更新水位的原地改动。

协议(docs/design/02-extraction.md §6):
- 按水位自然月分段;L1(廉价,每日可跑):源侧段内 COUNT+MAX vs 落地侧同口径;
- L2(修复,对不一致段或 --deep 全段):重抽该段(upsert 幂等,顺带修正原地改动、
  复活误删行),源侧消失的运行键打 _d2a_deleted_at 软删标记;
- 无水位表(小维表):整表 count 对比,L2 = 全量重抽 + 运行键 diff;
- 已知边界:原地改动若不改水位,L1 的 COUNT+MAX 察觉不到,须靠 --deep 兜底。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .adapters.base import SourceAdapter, TableInfo, resolve_runtime_keys
from .sink import HttpPushSink
from ...shared.store.landing import LandingStore, normalize_value


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
    """L2:流式重抽段 + staging 键表 SQL diff；内存与段大小无关。"""
    return _repair_local_stream(
        adapter, landing, source, info, wm_col, start, end)


def _repair_full(adapter: SourceAdapter, landing: LandingStore, source: str,
                 info: TableInfo) -> tuple[int, int]:
    """无水位表 L2：全量流式重抽 + staging 键表 SQL diff。"""
    return _repair_local_stream(adapter, landing, source, info)


def _repair_local_stream(
    adapter: SourceAdapter, landing: LandingStore, source: str,
    info: TableInfo, wm_col: str | None = None, start=None, end=None,
) -> tuple[int, int]:
    repair_id = uuid.uuid4().hex
    landing.begin_reconcile_repair(
        source, info, repair_id, wm_col, start, end)
    rows = batches = 0
    try:
        iterator = (
            adapter.read_segment(info, wm_col, start, end)
            if wm_col is not None else adapter.read_increment(info))
        for batch in iterator:
            batch_id = f"{repair_id}-{batches}"
            canonical = json.dumps(
                batch, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str).encode("utf-8")
            digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
            result = landing.write_reconcile_repair_batch(
                source, info.name, repair_id, batch_id, batch, digest)
            rows += int(result["ingested"])
            batches += 1
        completed = landing.complete_reconcile_repair(
            source, info.name, repair_id, rows, batches)
        return rows, int(completed["soft_deleted"])
    except Exception:
        landing.abort_reconcile_repair(source, info.name, repair_id)
        raise


def reconcile(adapter: SourceAdapter, landing: LandingStore, source: str,
              watermarks: dict[str, str] | None = None, deep: bool = False,
              key_columns: dict[str, list[str]] | None = None,
              start_dates: dict[str, str] | None = None,
              run_id: int | None = None) -> ReconcileReport:
    watermarks = watermarks or {}
    key_columns = key_columns or {}
    start_dates = start_dates or {}
    report = ReconcileReport(
        source=source,
        run_id=run_id or landing.start_run(source, "reconcile"),
        deep=deep)
    try:
        for raw_info in adapter.tables():
            info = resolve_runtime_keys(
                raw_info, key_columns.get(raw_info.name), require_keys=True)
            wm_col = watermarks.get(info.name)
            if wm_col is None:
                _reconcile_full_table(adapter, landing, source, info, deep, report)
            else:
                _reconcile_by_month(
                    adapter, landing, source, info, wm_col, deep, report,
                    start_date=start_dates.get(info.name))
    except Exception as e:
        landing.finish_run(
            report.run_id,
            tables=len({s.table for s in report.segments}),
            rows=sum(s.repaired_rows for s in report.segments),
            status="failed",
            detail=f"reconcile failed:{str(e)[:500]}")
        raise
    landing.finish_run(
        report.run_id, tables=len({s.table for s in report.segments}),
        rows=sum(s.repaired_rows for s in report.segments),
        status="ok",
        detail=f"reconcile{'-deep' if deep else ''}: "
               f"{len(report.mismatched)} 段不一致, 软删 {report.total_soft_deleted} 行")
    return report


def _reconcile_by_month(adapter, landing, source, info: TableInfo, wm_col: str,
                        deep: bool, report: ReconcileReport,
                        start_date: str | None = None) -> None:
    min_wm = landing.min_watermark(source, info.name, wm_col)
    high = landing.get_high_water(source, info.name)
    if min_wm is None or high is None:
        return  # 尚未同步过,跳过对账
    if start_date is not None:
        min_wm = max(str(min_wm), start_date)
    max_wm = max(high, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    for label, start, end in month_segments(min_wm, max_wm):
        # 首段不得回退到 start_date 所在月的月初，否则会超出用户
        # 明确配置的 ERP 抽取范围。
        if start_date is not None:
            start = max(start, start_date)
        step_id = landing.add_step(
            report.run_id, len(landing.steps_for_run(report.run_id)) + 1,
            "segment", f"{info.name}:{label}")
        src: dict | None = None
        dst: dict | None = None
        seg: SegmentResult | None = None
        try:
            src = adapter.segment_stats(info, wm_col, start, end)
            dst = landing.segment_stats(source, info.name, wm_col, start, end)
            src_max = normalize_value(src["max"])
            consistent = src["count"] == dst["count"] and src_max == dst["max"]
            if src["count"] == 0 and dst["count"] == 0:
                landing.update_step(step_id, status="ok", rows_in=0, rows_out=0)
                continue  # 双侧空段不进报告,但保留 step 证据
            seg = SegmentResult(info.name, label, src["count"], dst["count"], consistent)
            report.segments.append(seg)
            if not consistent or deep:
                seg.repaired_rows, seg.soft_deleted = _repair_segment(
                    adapter, landing, source, info, wm_col, start, end)
            landing.update_step(
                step_id, status="ok",
                rows_in=src["count"], rows_out=dst["count"],
                repaired=seg.repaired_rows, soft_deleted=seg.soft_deleted,
                error=None if consistent else f"不一致:源 {src['count']} vs 落地 {dst['count']}")
        except Exception as e:
            landing.update_step(
                step_id, status="failed",
                rows_in=src["count"] if src is not None else None,
                rows_out=dst["count"] if dst is not None else None,
                repaired=seg.repaired_rows if seg is not None else None,
                soft_deleted=seg.soft_deleted if seg is not None else None,
                error=str(e)[:500])
            raise


def _reconcile_full_table(adapter, landing, source, info: TableInfo,
                          deep: bool, report: ReconcileReport) -> None:
    step_id = landing.add_step(
        report.run_id, len(landing.steps_for_run(report.run_id)) + 1,
        "segment", f"{info.name}:全表")
    seg: SegmentResult | None = None
    try:
        src_count = adapter.table_count(info)
        dst_count = landing.count(source, info.name, active_only=True)
        consistent = src_count == dst_count
        seg = SegmentResult(info.name, "全表", src_count, dst_count, consistent)
        report.segments.append(seg)
        if not consistent or deep:
            seg.repaired_rows, seg.soft_deleted = _repair_full(adapter, landing, source, info)
        landing.update_step(
            step_id, status="ok",
            rows_in=src_count, rows_out=dst_count,
            repaired=seg.repaired_rows, soft_deleted=seg.soft_deleted,
            error=None if consistent else f"不一致:源 {src_count} vs 落地 {dst_count}")
    except Exception as e:
        landing.update_step(
            step_id, status="failed",
            rows_in=seg.src_count if seg is not None else None,
            rows_out=seg.dst_count if seg is not None else None,
            repaired=seg.repaired_rows if seg is not None else None,
            soft_deleted=seg.soft_deleted if seg is not None else None,
            error=str(e)[:500])
        raise


def _remote_repair(
    adapter: SourceAdapter, sink: HttpPushSink, source: str, info: TableInfo,
    wm_col: str | None = None, start=None, end=None,
) -> tuple[int, int]:
    """E6b L2：流式推行和当前键全集，平台完成 SQL 反连接软删。"""
    repair_id = uuid.uuid4().hex
    sink.begin_reconcile_repair(
        source, info, repair_id, wm_col, start, end)
    rows = batches = 0
    try:
        iterator = (
            adapter.read_segment(info, wm_col, start, end)
            if wm_col is not None
            else adapter.read_increment(info)
        )
        for batch in iterator:
            rows += sink.write_reconcile_repair(
                source, info, repair_id, f"{repair_id}-{batches}", batch)
            batches += 1
        completed = sink.complete_reconcile_repair(
            source, info, repair_id, rows, batches)
        return rows, int(completed.get("soft_deleted", 0))
    except Exception:
        try:
            sink.abort_reconcile_repair(source, info, repair_id)
        except Exception:
            pass
        raise


def reconcile_remote(
    adapter: SourceAdapter, landing: LandingStore, sink: HttpPushSink,
    source: str, watermarks: dict[str, str] | None = None,
    deep: bool = False,
    key_columns: dict[str, list[str]] | None = None,
    start_dates: dict[str, str] | None = None,
    run_id: int | None = None,
) -> ReconcileReport:
    """E6b：中间机读 ERP，平台算落地统计并执行软删，全程仅出站 HTTP。"""
    watermarks = watermarks or {}
    key_columns = key_columns or {}
    start_dates = start_dates or {}
    report = ReconcileReport(
        source=source,
        run_id=run_id or landing.start_run(source, "reconcile"),
        deep=deep)
    sink.bind_run(report.run_id)
    ordinal = 0
    # 不使用本地自增 run_id，避免 middle.sqlite 回滚/重装后与平台历史冲突。
    generation_id = f"reconcile-{uuid.uuid4().hex}"
    landing.set_run_generation(report.run_id, generation_id)
    generation_open = False
    repaired_any = False
    try:
        sink.ensure_reconcile_protocol()
        raw_tables = adapter.tables()
        sink.begin_reconcile_generation(
            source, generation_id, [table.name for table in raw_tables])
        generation_open = True
        for raw_info in raw_tables:
            info = resolve_runtime_keys(
                raw_info, key_columns.get(raw_info.name), require_keys=True)
            adapter.validate_runtime_keys(info)
            wm_col = watermarks.get(info.name)
            if wm_col is None:
                ordinal += 1
                step_id = landing.add_step(
                    report.run_id, ordinal, "segment", f"{info.name}:全表")
                src_count = adapter.table_count(info)
                dst = sink.reconcile_stats(source, info)
                dst_count = int(dst["count"])
                consistent = src_count == dst_count
                seg = SegmentResult(
                    info.name, "全表", src_count, dst_count, consistent)
                report.segments.append(seg)
                try:
                    if not consistent or deep:
                        repaired_any = True
                        seg.repaired_rows, seg.soft_deleted = _remote_repair(
                            adapter, sink, source, info)
                    landing.update_step(
                        step_id, status="ok", rows_in=src_count,
                        rows_out=dst_count, repaired=seg.repaired_rows,
                        soft_deleted=seg.soft_deleted,
                        error=None if consistent else
                        f"不一致:源 {src_count} vs 平台 {dst_count}")
                except Exception as e:
                    landing.update_step(
                        step_id, status="failed", rows_in=src_count,
                        rows_out=dst_count, error=str(e)[:500])
                    raise
                continue

            src_bounds = adapter.watermark_bounds(info, wm_col)
            dst_bounds = sink.reconcile_stats(source, info, wm_col)
            configured_start = start_dates.get(info.name)
            lows = ([configured_start] if configured_start is not None else [
                str(value) for value in (
                    normalize_value(src_bounds.get("min")),
                    normalize_value(dst_bounds.get("min")),
                ) if value is not None
            ])
            if not lows:
                continue
            highs = [
                str(value) for value in (
                    normalize_value(src_bounds.get("max")),
                    normalize_value(dst_bounds.get("max")),
                    landing.get_high_water(source, info.name),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ) if value is not None
            ]
            for label, start, end in month_segments(min(lows), max(highs)):
                if configured_start is not None:
                    start = max(start, configured_start)
                ordinal += 1
                step_id = landing.add_step(
                    report.run_id, ordinal, "segment",
                    f"{info.name}:{label}")
                src = adapter.segment_stats(info, wm_col, start, end)
                dst = sink.reconcile_stats(
                    source, info, wm_col, start, end)
                src_max = normalize_value(src["max"])
                consistent = (
                    int(src["count"]) == int(dst["count"])
                    and src_max == dst.get("max")
                )
                if int(src["count"]) == 0 and int(dst["count"]) == 0:
                    landing.update_step(
                        step_id, status="ok", rows_in=0, rows_out=0)
                    continue
                seg = SegmentResult(
                    info.name, label, int(src["count"]),
                    int(dst["count"]), consistent)
                report.segments.append(seg)
                try:
                    if not consistent or deep:
                        repaired_any = True
                        seg.repaired_rows, seg.soft_deleted = _remote_repair(
                            adapter, sink, source, info, wm_col, start, end)
                    landing.update_step(
                        step_id, status="ok", rows_in=seg.src_count,
                        rows_out=seg.dst_count, repaired=seg.repaired_rows,
                        soft_deleted=seg.soft_deleted,
                        error=None if consistent else
                        f"不一致:源 {seg.src_count} vs 平台 {seg.dst_count}")
                except Exception as e:
                    landing.update_step(
                        step_id, status="failed", rows_in=seg.src_count,
                        rows_out=seg.dst_count, error=str(e)[:500])
                    raise
        if repaired_any:
            sink.complete_reconcile_generation(
                source, generation_id, [table.name for table in raw_tables])
        else:
            sink.abort_reconcile_generation(source, generation_id)
        generation_open = False
    except Exception as e:
        if generation_open:
            try:
                sink.abort_reconcile_generation(source, generation_id)
            except Exception:
                pass
        landing.finish_run(
            report.run_id, tables=len({s.table for s in report.segments}),
            rows=sum(s.repaired_rows for s in report.segments),
            status="failed", detail=f"remote reconcile failed:{str(e)[:500]}")
        raise
    landing.finish_run(
        report.run_id, tables=len({s.table for s in report.segments}),
        rows=sum(s.repaired_rows for s in report.segments), status="ok",
        detail=f"remote reconcile{'-deep' if deep else ''}: "
        f"{len(report.mismatched)} 段不一致, 软删 "
        f"{report.total_soft_deleted} 行")
    return report
