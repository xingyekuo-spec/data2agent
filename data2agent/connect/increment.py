"""水位增量引擎:回看窗口、水位状态机、按表策略编排。

协议(docs/design/02-extraction.md §5):
- since = high_water - lookback,含边界;upsert 幂等使重叠安全;
- 水位只在该表全部批次落地提交后前进,且只前进不后退;
- 无水位声明的表(小维表如 CURRENCY)走 full_refresh;
- 首轮(无状态)= 全表按水位序扫描,顺便建立水位。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..mapping import parse_field_expr
from ..metamodel.schema import TemplatePack
from .adapters.base import SourceAdapter
from .landing import LandingStore
from .sink import LocalSink, Sink
from .sync import SyncReport, TableReport

DEFAULT_LOOKBACK_DAYS = 3

_WM_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def watermarks_from_pack(pack: TemplatePack, source: str) -> dict[str, str]:
    """binding.watermark(表.字段)→ {表: 水位列}。同表冲突声明视为契约错误。"""
    out: dict[str, str] = {}
    for o in pack.objects:
        for b in o.bindings:
            if b.source != source or not b.watermark or not b.enabled:
                continue
            expr = parse_field_expr(b.watermark)
            if expr.table in out and out[expr.table] != expr.column:
                raise ValueError(
                    f"表 {expr.table} 的水位列声明冲突:{out[expr.table]} vs {expr.column}")
            out[expr.table] = expr.column
    return out


def subtract_lookback(high_water: str, days: float) -> str:
    """按水位值原格式做回看减法(支持 datetime / date 字符串)。"""
    for fmt in _WM_FORMATS:
        try:
            dt = datetime.strptime(high_water, fmt)
        except ValueError:
            continue
        return (dt - timedelta(days=days)).strftime(fmt)
    raise ValueError(f"无法解析水位值 '{high_water}'(支持格式:{_WM_FORMATS})")


def incremental_sync(adapter: SourceAdapter, landing: LandingStore, source: str,
                     watermarks: dict[str, str] | None = None,
                     lookback_days: float = DEFAULT_LOOKBACK_DAYS,
                     should_continue: Optional[Callable[[], bool]] = None,
                     sink: Optional[Sink] = None) -> SyncReport:
    """sink:raw 落地出口(§12.3)。默认 LocalSink(landing)=写本地库(同机 / 开发);
    Pattern A 传 HttpPushSink,raw 推给平台、landing 只留水位 / 审计 / 运行状态。
    should_continue:批次边界的优雅暂停钩子(错峰窗口越界时返回 False)。
    暂停时进行中的表不推进水位(已落批次幂等),下窗口自然续跑。"""
    watermarks = watermarks or {}
    sink = sink or LocalSink(landing)
    report = SyncReport(source=source, run_id=landing.start_run(source, "sync"))
    try:
        for info in adapter.tables():
            if should_continue and not should_continue():
                report.paused = True
                break
            sink.ensure_table(source, info)
            batch_id = uuid.uuid4().hex[:12]
            wm_col = watermarks.get(info.name)

            if wm_col is None:
                strategy, since, high_water = "full_refresh", None, None
            else:
                high_water = landing.get_high_water(source, info.name)
                if high_water is None:
                    strategy, since = "initial", None
                else:
                    strategy, since = "increment", subtract_lookback(high_water, lookback_days)

            rows = batches = 0
            max_wm = high_water
            interrupted = False
            for batch in adapter.read_increment(info, since=since, watermark_col=wm_col):
                if should_continue and not should_continue():
                    interrupted = True
                    break
                rows += sink.write(source, info, batch, batch_id)
                batches += 1
                if wm_col:
                    seen = max((str(r[wm_col]) for r in batch if r[wm_col] is not None),
                               default=None)
                    if seen is not None and (max_wm is None or seen > max_wm):
                        max_wm = seen
            if interrupted:
                report.paused = True  # 本表水位不前进,下轮从旧水位重来(upsert 幂等)
                break
            if wm_col:  # 全部批次提交后才前进水位
                landing.set_high_water(source, info.name, wm_col, max_wm, batch_id)
            report.tables.append(TableReport(info.name, rows, batches, batch_id,
                                             strategy=strategy, high_water=max_wm))
    except Exception as e:
        landing.finish_run(report.run_id, tables=len(report.tables),
                           rows=report.total_rows, status="failed", detail=str(e))
        raise
    landing.finish_run(report.run_id, tables=len(report.tables), rows=report.total_rows,
                       status="paused" if report.paused else "ok",
                       detail="窗口越界,批次边界暂停" if report.paused else "")
    return report
