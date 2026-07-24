"""水位增量引擎:回看窗口、水位状态机、按表策略编排。

协议(docs/design/02-extraction.md §5):
- since = high_water - lookback,含边界;upsert 幂等使重叠安全;
- 水位/复合键游标只在批次成功后前进,且只前进不后退;
- 无水位声明的表(小维表如 CURRENCY)走 full_refresh;
- 首轮(无状态)= 全表按水位序扫描,顺便建立水位;
- 运行键优先使用配置 key_columns,否则使用数据库主键(支持复合键);
- 中断后续传使用持久化复合键游标边界。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from .adapters.base import (
    SourceAdapter,
    resolve_runtime_keys,
)
from .landing import LandingStore
from .sink import LocalSink, Sink
from .sync import SyncReport, TableReport

DEFAULT_LOOKBACK_DAYS = 3

_WM_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


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
                     sink: Optional[Sink] = None,
                     key_columns: dict[str, list[str]] | None = None) -> SyncReport:
    """sink:raw 落地出口(§12.3)。默认 LocalSink(landing)=写本地库(同机 / 开发);
    Pattern A 传 HttpPushSink,raw 推给平台、landing 只留水位 / 审计 / 运行状态。
    key_columns:表名 → 配置运行键,覆盖数据库主键。
    should_continue:批次边界的优雅暂停钩子(错峰窗口越界时返回 False)。
    """
    watermarks = watermarks or {}
    key_columns = key_columns or {}
    sink = sink or LocalSink(landing)
    report = SyncReport(source=source, run_id=landing.start_run(source, "sync"))
    current_step: int | None = None
    try:
        ordinal = 0
        for raw_info in adapter.tables():
            if should_continue and not should_continue():
                report.paused = True
                break
            ordinal += 1
            current_step = landing.add_step(report.run_id, ordinal, "table", raw_info.name)
            wm_col = watermarks.get(raw_info.name)
            info = resolve_runtime_keys(
                raw_info, key_columns.get(raw_info.name), require_keys=True)
            adapter.validate_runtime_keys(info)
            landing.log_audit(
                source, "runtime_keys",
                f"table={info.name} source={info.key_source} columns={','.join(info.pk)}",
                0, 0.0)

            sink.ensure_table(source, info)
            batch_id = uuid.uuid4().hex[:12]

            since = None
            resume_after = None
            high_water = None
            strategy = "full_refresh"
            if wm_col is not None:
                cursor = landing.get_sync_cursor(source, info.name)
                if cursor is None:
                    strategy, since, high_water = "initial", None, None
                else:
                    cur_w, cur_k = cursor
                    high_water = str(cur_w) if cur_w is not None else None
                    if cur_k is not None:
                        # 中断后续传:从持久化复合键边界继续
                        strategy = "resume"
                        resume_after = (cur_w, *cur_k)
                    else:
                        strategy = "increment"
                        since = subtract_lookback(str(cur_w), lookback_days)

            rows = batches = 0
            max_wm = high_water
            last_cursor_keys: list | None = None
            interrupted = False
            for batch in adapter.read_increment(
                info, since=since, watermark_col=wm_col, resume_after=resume_after,
            ):
                if should_continue and not should_continue():
                    interrupted = True
                    break
                rows += sink.write(source, info, batch, batch_id)
                batches += 1
                if wm_col:
                    last = batch[-1]
                    last_wm = last[wm_col]
                    last_cursor_keys = [last[k] for k in info.pk]
                    # 每批成功后原子推进持久化游标(稳定边界)
                    landing.set_sync_cursor(
                        source, info.name, wm_col, last_wm, last_cursor_keys, batch_id)
                    seen = str(last_wm)
                    if max_wm is None or seen > max_wm:
                        max_wm = seen
            if interrupted:
                landing.update_step(current_step, status="paused",
                                    rows_in=rows, rows_out=rows, batch_id=batch_id)
                report.paused = True
                break
            sink.complete_table(source, info, batch_id, rows, batches)
            if wm_col:
                # 整表完成:游标推进为(水位, None)
                landing.set_sync_cursor(
                    source, info.name, wm_col, max_wm, None, batch_id, force=True)
            landing.update_step(
                current_step, status="ok", rows_in=rows, rows_out=rows,
                batch_id=batch_id,
                watermark_before=(json.dumps(high_water)
                                  if high_water is not None else None),
                watermark_after=(json.dumps(max_wm)
                                 if max_wm is not None else None))
            current_step = None
            report.tables.append(TableReport(info.name, rows, batches, batch_id,
                                             strategy=strategy, high_water=max_wm))
    except Exception as e:
        if current_step is not None:
            try:
                landing.update_step(current_step, status="failed", error=str(e)[:500])
            except Exception:
                pass
        landing.finish_run(report.run_id, tables=len(report.tables),
                           rows=report.total_rows, status="failed", detail=str(e))
        raise
    landing.finish_run(report.run_id, tables=len(report.tables), rows=report.total_rows,
                       status="paused" if report.paused else "ok",
                       detail="窗口越界,批次边界暂停" if report.paused else "")
    return report
