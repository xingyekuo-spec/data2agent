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
import hashlib
import os
import pickle
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
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


def _spool_prefix(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    return f"d2a-full-{digest}-"


def cleanup_orphan_spools(directory: str | Path, source: str) -> int:
    """在持有 source 同步锁时清理该源崩溃遗留的加密卷 spool。"""
    root = Path(directory)
    if not root.exists():
        return 0
    removed = 0
    for path in root.glob(_spool_prefix(source) + "*.spool"):
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
    return removed


def _close_spool(spool, spool_path: Path | None) -> None:
    try:
        if spool is not None:
            spool.close()
    finally:
        if spool_path is not None:
            spool_path.unlink(missing_ok=True)


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
    # datetimeoffset 由 pyodbc 返回带时区 ISO 字符串。
    try:
        dt = datetime.fromisoformat(high_water.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is not None and dt.tzinfo is not None:
        return (dt - timedelta(days=days)).isoformat(sep=" ")
    raise ValueError(f"无法解析水位值 '{high_water}'(支持格式:{_WM_FORMATS})")


def incremental_sync(adapter: SourceAdapter, landing: LandingStore, source: str,
                     watermarks: dict[str, str] | None = None,
                     lookback_days: float = DEFAULT_LOOKBACK_DAYS,
                     should_continue: Optional[Callable[[], bool]] = None,
                     sink: Optional[Sink] = None,
                     key_columns: dict[str, list[str]] | None = None,
                     run_id: int | None = None,
                     only_tables: set[str] | None = None,
                     start_dates: dict[str, str] | None = None,
                     estimate_rows: bool = True,
                     full_refresh_spool_policy: str = "temporary_file",
                     spool_directory: str | None = None) -> SyncReport:
    """sink:raw 落地出口。默认 LocalSink(landing)。
    key_columns:表名 → 配置运行键,覆盖数据库主键。
    无水位的表按 full_refresh 快照协议落地。
    only_tables:限定只同步这些表(失败表定向重试);None=全部白名单表。
    start_dates:表名 → 抽取起始日期;首轮从此日期起扫(不抽历史全量),
    后续增量取下界 max(水位-回看, start_date)。

    当外部已创建 run(如手动触发已预检窗口/锁/create run 后
    传入 run_id)时,不重复建 run,避免 double-run。
    """
    watermarks = watermarks or {}
    key_columns = key_columns or {}
    start_dates = start_dates or {}
    sink = sink or LocalSink(landing)
    ensure_proto = getattr(sink, "ensure_protocol", None)

    report = SyncReport(
        source=source,
        run_id=run_id or landing.start_run(source, "sync"))
    current_step: int | None = None
    sync_started = False
    try:
        if callable(ensure_proto):
            ensure_proto()

        table_infos = [
            info for info in adapter.tables()
            if only_tables is None or info.name in only_tables
        ]
        begin_sync = getattr(sink, "begin_sync", None)
        if callable(begin_sync):
            sync_started = True
            begin_sync(source, [info.name for info in table_infos], report.run_id)

        ordinal = 0
        for raw_info in table_infos:
            ordinal += 1
            current_step = landing.add_step(
                report.run_id, ordinal, "table", raw_info.name)
            if should_continue and not should_continue():
                landing.update_step(
                    current_step, status="paused",
                    error="错峰窗口已结束，本表尚未开始")
                current_step = None
                report.paused = True
                break
            wm_col = watermarks.get(raw_info.name)
            is_full = wm_col is None
            mode = "full_refresh" if is_full else "incremental"
            info = resolve_runtime_keys(
                raw_info, key_columns.get(raw_info.name),
                require_keys=not is_full)
            if wm_col is not None:
                column_types = dict(info.columns)
                if wm_col not in column_types:
                    raise ValueError(
                        f"{info.name}: watermark 列 '{wm_col}' 不存在")
                if column_types[wm_col] != "text":
                    raise ValueError(
                        f"{info.name}: watermark '{wm_col}' 当前类型为 "
                        f"{column_types[wm_col]}；当前增量协议仅支持日期/时间水位")
            if info.pk:
                strategy_raw = json.dumps({
                    "schema": info.schema,
                    "table": info.name,
                    "columns": info.columns,
                    "keys": info.pk,
                    "watermark": wm_col,
                }, sort_keys=True, separators=(",", ":"))
                strategy_fingerprint = hashlib.sha256(
                    strategy_raw.encode("utf-8")).hexdigest()
                if (
                    info.key_source == "configured"
                    or not landing.runtime_keys_recently_validated(
                        source, info.name, strategy_fingerprint)
                ):
                    adapter.validate_runtime_keys(info)
                    if wm_col is not None:
                        adapter.validate_watermark(info, wm_col)
                    landing.record_runtime_key_validation(
                        source, info.name, strategy_fingerprint)
            landing.log_audit(
                source, "runtime_keys",
                f"table={info.name} source={info.key_source} mode={mode} "
                f"columns={','.join(info.pk) if info.pk else '(none)'}",
                0, 0.0)

            snapshot_id = uuid.uuid4().hex[:16] if is_full else None
            table_batch_id = uuid.uuid4().hex[:12]

            since = None
            resume_after = None
            high_water = None
            strategy = "full_refresh"
            if not is_full:
                start_date = start_dates.get(raw_info.name)
                cursor = landing.get_sync_cursor(
                    source, info.name, watermark_col=wm_col,
                    key_columns=list(info.pk), schema=info.schema)
                if cursor is None:
                    # 首轮:配置 start_date 则从其起扫,不抽历史全量
                    strategy, since, high_water = "initial", start_date, None
                else:
                    cur_w, cur_k = cursor
                    high_water = str(cur_w) if cur_w is not None else None
                    if cur_k is not None:
                        strategy = "resume"
                        resume_after = (cur_w, *cur_k)
                    else:
                        strategy = "increment"
                        since = subtract_lookback(str(cur_w), lookback_days)
                        # 起始日期作为下界(ISO 格式字典序可比较)
                        if start_date and (since is None or since < start_date):
                            since = start_date

            # 游标策略兼容性检查完成后才在 sink 侧开启表事务，避免配置变更
            # 被拒绝时留下远端 open snapshot / 虚假 begin 证据。
            sink.begin_table(source, info, mode=mode, snapshot_id=snapshot_id)

            rows = batches = 0
            max_wm = high_water
            interrupted = False
            # 同步前预估行数(进度分母);预估失败不阻断同步,页面退化为仅行数
            if estimate_rows:
                try:
                    expect_since = since if strategy in ("initial", "increment") else (
                        high_water if strategy == "resume" else None)
                    expected = adapter.count_for_sync(info, wm_col, expect_since)
                    landing.update_step(current_step, expected_rows=expected)
                except Exception:
                    pass
            try:
                last_heartbeat = time.monotonic()
                batch_iterator = adapter.read_increment(
                    info, since=since, watermark_col=wm_col,
                    resume_after=resume_after)
                spool = None
                spool_path: Path | None = None
                if is_full and full_refresh_spool_policy != "strict_stream":
                    # 先快速读完源库单语句快照并落到本机临时文件，
                    # 再做 HTTP 推送；避免网络重试/限流期间长时持有 ERP 游标和锁。
                    if full_refresh_spool_policy not in (
                        "temporary_file", "encrypted_temp_volume",
                    ):
                        raise ValueError(
                            f"未知 full_refresh spool 策略:{full_refresh_spool_policy}")
                    spool_dir = None
                    if full_refresh_spool_policy == "encrypted_temp_volume":
                        if not spool_directory:
                            raise ValueError(
                                "encrypted_temp_volume 缺少 spool_directory")
                        spool_dir = Path(spool_directory)
                        spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                        try:
                            spool_dir.chmod(0o700)
                        except OSError:
                            pass
                    spool = tempfile.NamedTemporaryFile(
                        prefix=_spool_prefix(source), suffix=".spool",
                        dir=spool_dir, delete=False)
                    spool_path = Path(spool.name)
                    try:
                        os.chmod(spool_path, 0o600)
                    except OSError:
                        pass
                    for source_batch in batch_iterator:
                        if should_continue and not should_continue():
                            interrupted = True
                            break
                        pickle.dump(source_batch, spool, protocol=pickle.HIGHEST_PROTOCOL)
                        if time.monotonic() - last_heartbeat >= 60:
                            heartbeat = getattr(sink, "heartbeat_sync", None)
                            if callable(heartbeat):
                                heartbeat(source)
                            last_heartbeat = time.monotonic()
                    if not interrupted:
                        spool.seek(0)

                        def _spooled_batches():
                            while True:
                                try:
                                    yield pickle.load(spool)
                                except EOFError:
                                    return

                        batch_iterator = _spooled_batches()
                for batch in batch_iterator:
                    if should_continue and not should_continue():
                        interrupted = True
                        break
                    if time.monotonic() - last_heartbeat >= 60:
                        heartbeat = getattr(sink, "heartbeat_sync", None)
                        if callable(heartbeat):
                            heartbeat(source)
                        last_heartbeat = time.monotonic()
                    batch_id = f"{table_batch_id}-{batches}"
                    rows += sink.write(
                        source, info, batch, batch_id,
                        mode=mode, snapshot_id=snapshot_id,
                        table_run_id=table_batch_id)
                    # 成功 batch 本身也会在平台刷新 generation 活动时间。
                    last_heartbeat = time.monotonic()
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
                            batch_id=table_batch_id,
                            key_columns=list(info.pk), schema=info.schema)
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
                _close_spool(spool, spool_path)
                spool = None
                spool_path = None
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
                if "spool" in locals():
                    _close_spool(spool, spool_path)
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
                    force=True, key_columns=list(info.pk), schema=info.schema)
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
        if report.paused:
            abort_sync = getattr(sink, "abort_sync", None)
            if sync_started and callable(abort_sync):
                abort_sync(source)
        else:
            complete_sync = getattr(sink, "complete_sync", None)
            if sync_started and callable(complete_sync):
                complete_sync(source)
    except Exception as e:
        abort_sync = getattr(sink, "abort_sync", None)
        if sync_started and callable(abort_sync):
            try:
                abort_sync(source)
            except Exception:
                pass
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
