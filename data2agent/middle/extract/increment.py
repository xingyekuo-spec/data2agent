"""水位增量引擎:回看窗口、水位状态机、按表策略编排。

协议(docs/design/02-extraction.md §5 + M4 快照):
- since = high_water - lookback,含边界;upsert 幂等使重叠安全;
- 水位/复合键游标只在批次成功后前进,且只前进不后退;
- 无水位声明的表(mode=full_refresh)走快照 staging → 原子发布;
- 首轮增量(无状态)= 全表按水位序扫描,顺便建立水位;
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
from ...shared.store.landing import LandingStore
from .sink import LocalSink, Sink
from .sync import SyncReport, TableReport

DEFAULT_LOOKBACK_DAYS = 3

_WM_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")

_CREDENTIAL_PATTERN = __import__("re").compile(
    r"(?i)("
    r"password\s*[=:]|pwd\s*[=:]|"
    r"token\s*[=:]|bearer\s+|authorization\s*[:=]|"
    r"Driver\s*=|Server\s*=|Database\s*=|"
    r"uid\s*=|user\s+id\s*=|integrated\s+security|"
    r"connection\s+string|connect\s+string|DSN\s*=|"
    r"ODBC|pyodbc\.connect|"
    # SQL DML / DDL / 过程动词:词边界匹配,不依赖后续关键字
    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|"
    r"TRUNCATE|EXEC|EXECUTE|MERGE|GRANT|REVOKE|"
    r"DECLARE|CALL|EXPLAIN|BEGIN|COMMIT|ROLLBACK|SAVEPOINT)\b"
    r")"
)


def _safe_error(exc: BaseException) -> str:
    """脱敏错误摘要,禁止 DSN / 连接串 / Token / SQL / 凭据泄露。

    命中脱敏关键字 → 通用文本;超过 400 字符 → 截断。
    """
    raw = f"{type(exc).__name__}: {str(exc)}"
    if _CREDENTIAL_PATTERN.search(raw):
        return "执行失败(详情已脱敏,含凭据/连接串/Token/SQL 关键字)"
    if len(raw) > 400:
        return raw[:400] + "…[已截断]"
    return raw


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
                     key_columns: dict[str, list[str]] | None = None,
                     run_id: int | None = None) -> SyncReport:
    """sink:raw 落地出口。默认 LocalSink(landing)。
    key_columns:表名 → 配置运行键,覆盖数据库主键。
    无水位的表按 full_refresh 快照协议落地。

    当外部已创建 run(如手动触发已预检窗口/锁/create run 后
    传入 run_id)时,不重复建 run,避免 double-run。
    """
    watermarks = watermarks or {}
    key_columns = key_columns or {}
    sink = sink or LocalSink(landing)
    ensure_proto = getattr(sink, "ensure_protocol", None)

    report = SyncReport(
        source=source,
        run_id=run_id or landing.start_run(source, "sync"))
    current_step: int | None = None
    try:
        if callable(ensure_proto):
            ensure_proto()

        ordinal = 0
        for raw_info in adapter.tables():
            if should_continue and not should_continue():
                report.paused = True
                break
            ordinal += 1
            current_step = landing.add_step(report.run_id, ordinal, "table", raw_info.name)
            wm_col = watermarks.get(raw_info.name)
            is_full = wm_col is None
            mode = "full_refresh" if is_full else "incremental"
            info = resolve_runtime_keys(
                raw_info, key_columns.get(raw_info.name),
                require_keys=not is_full)
            if info.pk:
                adapter.validate_runtime_keys(info)
            landing.log_audit(
                source, "runtime_keys",
                f"table={info.name} source={info.key_source} mode={mode} "
                f"columns={','.join(info.pk) if info.pk else '(none)'}",
                0, 0.0)

            snapshot_id = uuid.uuid4().hex[:16] if is_full else None
            table_batch_id = uuid.uuid4().hex[:12]
            sink.begin_table(source, info, mode=mode, snapshot_id=snapshot_id)

            since = None
            resume_after = None
            high_water = None
            strategy = "full_refresh"
            if not is_full:
                cursor = landing.get_sync_cursor(source, info.name)
                if cursor is None:
                    strategy, since, high_water = "initial", None, None
                else:
                    cur_w, cur_k = cursor
                    high_water = str(cur_w) if cur_w is not None else None
                    if cur_k is not None:
                        strategy = "resume"
                        resume_after = (cur_w, *cur_k)
                    else:
                        strategy = "increment"
                        since = subtract_lookback(str(cur_w), lookback_days)

            rows = batches = 0
            max_wm = high_water
            interrupted = False
            # 同步前预估行数(进度分母);预估失败不阻断同步,页面退化为仅行数
            try:
                expect_since = since if strategy == "increment" else (
                    high_water if strategy == "resume" else None)
                expected = adapter.count_for_sync(info, wm_col, expect_since)
                landing.update_step(current_step, expected_rows=expected)
            except Exception:
                pass
            try:
                for batch in adapter.read_increment(
                    info, since=since, watermark_col=wm_col, resume_after=resume_after,
                ):
                    if should_continue and not should_continue():
                        interrupted = True
                        break
                    batch_id = (f"{table_batch_id}-{batches}" if is_full
                                else table_batch_id)
                    rows += sink.write(
                        source, info, batch, batch_id,
                        mode=mode, snapshot_id=snapshot_id)
                    batches += 1
                    if wm_col:
                        last = batch[-1]
                        last_wm = last[wm_col]
                        last_cursor_keys = [last[k] for k in info.pk]
                        landing.record_sync_batch_progress(
                            step_id=current_step,
                            source=source, table=info.name,
                            watermark_col=wm_col, watermark=last_wm,
                            key_values=last_cursor_keys,
                            rows_in=rows, rows_out=rows, batches=batches,
                            batch_id=table_batch_id)
                        seen = str(last_wm)
                        if max_wm is None or seen > max_wm:
                            max_wm = seen
                    else:
                        landing.record_sync_batch_progress(
                            step_id=current_step,
                            source=source, table=info.name,
                            watermark_col=None,
                            rows_in=rows, rows_out=rows, batches=batches,
                            batch_id=table_batch_id)
                if interrupted:
                    if is_full:
                        sink.abort_table(
                            source, info, mode=mode, snapshot_id=snapshot_id)
                    landing.update_step(current_step, status="paused",
                                        rows_in=rows, rows_out=rows,
                                        batch_id=table_batch_id)
                    report.paused = True
                    break
                sink.complete_table(
                    source, info, table_batch_id, rows, batches,
                    mode=mode, snapshot_id=snapshot_id)
            except Exception:
                if is_full:
                    try:
                        sink.abort_table(
                            source, info, mode=mode, snapshot_id=snapshot_id)
                    except Exception:
                        pass
                raise

            if wm_col:
                landing.set_sync_cursor(
                    source, info.name, wm_col, max_wm, None, table_batch_id,
                    force=True)
            landing.update_step(
                current_step, status="ok", rows_in=rows, rows_out=rows,
                batch_id=table_batch_id,
                watermark_before=(json.dumps(high_water)
                                  if high_water is not None else None),
                watermark_after=(json.dumps(max_wm)
                                 if max_wm is not None else None))
            current_step = None
            report.tables.append(TableReport(info.name, rows, batches, table_batch_id,
                                             strategy=strategy, high_water=max_wm))
    except Exception as e:
        safe = _safe_error(e)
        if current_step is not None:
            try:
                landing.update_step(current_step, status="failed", error=safe)
            except Exception:
                pass
        landing.finish_run(report.run_id, tables=len(report.tables),
                           rows=report.total_rows, status="failed", detail=safe)
        raise
    landing.finish_run(report.run_id, tables=len(report.tables), rows=report.total_rows,
                       status="paused" if report.paused else "ok",
                       detail="窗口越界,批次边界暂停" if report.paused else "")
    return report
