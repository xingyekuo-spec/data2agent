"""调度常驻(E5):按 connect.yaml 排程 sync(+apply)与每日 L1 对账,窗口硬约束。

- 窗口外不发起;运行中越界 → incremental_sync 在批次边界优雅暂停(paused),
  水位机制保证下窗口自然续跑;
- 每轮汇总在 d2a_sync_run,结构化日志(key=value)输出;
- serve --once 立即各跑一轮(仍尊重窗口),用于验证配置与容器环境。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from ...shared.config import (
    ConnectConfig,
    SourceConfig,
    assert_production_ready,
    in_window,
)
from ...shared.store.dataset_publish import build_dataset
from .increment import cleanup_orphan_spools, incremental_sync
from ...shared.store.landing import LandingStore
from .reconcile import reconcile, reconcile_remote

log = logging.getLogger("data2agent.connect")


@dataclass
class SyncCycleResult:
    """run_sync_cycle 返回值:明确表达跳过/执行/锁冲突等状态。"""
    executed: bool
    reason: str
    run_id: int | None = None
    status: str | None = None       # ok/paused/failed/started
    note: str = ""
    suggestion: str | None = None


def _brief_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def build_adapter(name: str, scfg: SourceConfig,
                  landing: LandingStore):
    """构建适配器:白名单仅从 tables 配置生成。"""
    whitelist = scfg.table_whitelist()
    hook = lambda action, sql, rows, ms: landing.log_audit(name, action, sql, rows, ms)  # noqa: E731
    kwargs = dict(batch_size=scfg.rate.batch_size,
                  rows_per_second=scfg.rate.rows_per_second, audit_hook=hook,
                  table_schemas=scfg.table_schemas())
    if scfg.adapter == "sqlite_readonly":
        import os

        from .adapters.sqlite import SqliteReadOnlyAdapter
        path = scfg.path or os.environ[scfg.dsn_env]
        return SqliteReadOnlyAdapter(path, whitelist, **kwargs)
    import os

    from .adapters.mssql import MssqlReadOnlyAdapter
    dsn = os.environ.get(scfg.dsn_env or "", "")
    if not dsn:
        raise RuntimeError(f"源 {name}: 环境变量 {scfg.dsn_env} 为空")
    return MssqlReadOnlyAdapter(dsn, whitelist, **kwargs)


def build_sink(scfg: SourceConfig, landing: LandingStore, *,
               source: str = "", run_id: int | None = None):
    """按 sink 配置构建落地出口:local=本地库;http=推给平台(§12.3)。"""
    if scfg.sink.type == "http":
        import os

        from .sink import HttpPushSink
        token = os.environ.get(scfg.sink.token_env or "", "") or None
        return HttpPushSink(
            scfg.sink.url, token,
            timeout=scfg.sink.timeout_seconds,
            retries=scfg.sink.retries,
            ca_bundle=scfg.sink.ca_bundle,
            landing=landing, source=source, run_id=run_id)
    from .sink import LocalSink
    return LocalSink(landing)


def validate_configured_schemas(
    source: str, scfg: SourceConfig, landing: LandingStore, *,
    only_tables: set[str] | None = None,
) -> None:
    """把元数据页保存的 schema_fingerprint 变成运行时屏障。"""
    pending = [
        (table, spec)
        for table, spec in (scfg.tables or {}).items()
        if spec.schema_fingerprint
        and (only_tables is None or table in only_tables)
        and not landing.schema_recently_validated(
            source, table, spec.schema_fingerprint)
    ]
    if not pending:
        return
    from .metadata import build_discoverer

    discoverer = build_discoverer(scfg)
    try:
        default_schema = discoverer.default_schema()
        for table, spec in pending:
            actual = discoverer.get_table(
                spec.schema or default_schema, table).schema_fingerprint
            if actual != spec.schema_fingerprint:
                raise RuntimeError(
                    f"{table}: ERP 表结构已变更"
                    f"(configured={spec.schema_fingerprint}, actual={actual})；"
                    "已在抽取前停止，请在元数据页重新扫描、确认并保存")
            landing.record_schema_validation(
                source, table, spec.schema_fingerprint)
    finally:
        discoverer.close()


def check_sync_preflight(name: str, scfg: SourceConfig) -> SyncCycleResult:
    """同步前预检(不创建 run,不获取锁):窗口外/tables 为空立即返回。"""
    if not in_window(datetime.now().time(), scfg.windows):
        return SyncCycleResult(
            executed=False, reason="outside_window",
            note="错峰窗口外，未发起（窗口约束同样生效）",
            suggestion="等待错峰窗口开始，或临时调整 sources.*.windows 后重启抽取进程",
        )
    if not scfg.table_whitelist():
        return SyncCycleResult(
            executed=False, reason="tables_unconfigured",
            note="尚未配置抽取表，未发起同步",
            suggestion="到「元数据」选表并在「抽取表」页保存计划后再触发同步",
        )
    return SyncCycleResult(executed=True, reason="preflight_ok")


def run_sync_cycle(name: str, scfg: SourceConfig,
                   landing_path: str, templates: str = "templates",
                   run_id: int | None = None,
                   acquired_lock=None,
                   tables: list[str] | None = None) -> SyncCycleResult:
    """一轮 sync(+apply)。返回 SyncCycleResult。

    - 自动调度路径(run_id=None):内部创建 run,自行获取锁
    - 手动触发路径(run_id != None):外部已获取锁 + 创建 run,传入复用
    - tables:限定只同步这些表(失败表定向重试);None=全部
    """
    # 1. 预检
    preflight = check_sync_preflight(name, scfg)
    if not preflight.executed:
        return preflight

    landing = LandingStore(landing_path)

    # 2. 锁:自动调度路径自行获取
    own_lock = None
    if acquired_lock is None:
        from .sync_lock import SourceSyncLock  # noqa: E402
        own_lock = SourceSyncLock.try_acquire(landing_path, name)
        if own_lock is None:
            existing = SourceSyncLock.find_running_run(landing_path, name)
            landing.con.close()
            return SyncCycleResult(
                executed=False, reason="already_running", run_id=existing,
                note="已有同步正在运行" if existing else "已有同步正在启动中",
                suggestion="等待当前运行完成后再触发",
            )

    try:
        # 3. 没有外部 run_id 时自己建(run_sync_cycle 在此处建 run
        #    属于新协议:所有 pre-increment 错误都会 finish_running_run)
        own_run = run_id is None
        if own_run:
            run_id = landing.start_run(name, "sync")

        if (
            scfg.spool.policy == "encrypted_temp_volume"
            and scfg.spool.directory
        ):
            removed = cleanup_orphan_spools(scfg.spool.directory, name)
            if removed:
                log.warning(
                    "removed orphan encrypted spool source=%s count=%s",
                    name, removed)

        try:
            adapter = build_adapter(name, scfg, landing)
            sink = build_sink(scfg, landing, source=name, run_id=run_id)
            validate_configured_schemas(
                name, scfg, landing,
                only_tables=set(tables) if tables else None)
        except Exception as exc:
            landing.finish_running_run(run_id, status="failed",
                                       detail=_brief_error(exc))
            raise

        watermarks = scfg.table_watermarks()
        key_columns = scfg.table_key_columns()
        report = incremental_sync(
            adapter, landing, name, watermarks,
            lookback_days=scfg.lookback_days(), sink=sink,
            key_columns=key_columns,
            should_continue=lambda: in_window(datetime.now().time(), scfg.windows),
            run_id=run_id,
            only_tables=set(tables) if tables else None,
            start_dates=scfg.table_start_dates(),
            estimate_rows=scfg.estimate_rows,
            full_refresh_spool_policy=scfg.spool.policy,
            spool_directory=scfg.spool.directory)
        log.info("sync source=%s run=%s rows=%s tables=%s paused=%s sink=%s",
                 name, report.run_id, report.total_rows, len(report.tables),
                 report.paused, scfg.sink.type)
        # sink=http: raw 已推给平台,映射在平台侧跑,不在中间 apply
        if scfg.apply_after_sync and not report.paused and scfg.sink.type == "local":
            from ...shared.metamodel.loader import load_pack as _load_pack
            pack = _load_pack(templates)
            apply_report = build_dataset(landing, pack, name, auto_publish=True)
            log.info(
                "apply source=%s objects=%s quarantined=%s aborted=%s "
                "dataset_version=%s published=%s",
                name, len(apply_report.results),
                sum(r.quarantined for r in apply_report.results),
                [r.object for r in apply_report.results if r.status == "aborted"],
                apply_report.dataset_version,
                apply_report.published,
            )
        status = "paused" if report.paused else "ok"
        return SyncCycleResult(
            executed=True, reason="executed", run_id=run_id, status=status,
            note="窗口越界,批次边界暂停" if report.paused else "",
        )
    except Exception as exc:
        log.exception("sync failed source=%s run=%s", name, run_id)
        if run_id is not None:
            try:
                landing.finish_running_run(run_id, status="failed",
                                           detail=_brief_error(exc))
            except Exception:
                pass
        return SyncCycleResult(
            executed=False, reason="failed", run_id=run_id, status="failed",
            note=str(exc)[:200],
            suggestion="查看日志排查错误后重试",
        )
    finally:
        if own_lock is not None:
            own_lock.release()
        landing.con.close()


def run_reconcile_cycle(name: str, scfg: SourceConfig,
                        landing_path: str, deep: bool = False,
                        wait_for_lock_seconds: float = 0.0,
                        run_id: int | None = None,
                        acquired_lock=None) -> bool:
    if not in_window(datetime.now().time(), scfg.windows):
        log.info("skip reconcile source=%s reason=窗口外", name)
        return False
    from .sync_lock import SourceSyncLock

    deadline = time.monotonic() + max(0.0, wait_for_lock_seconds)
    own_lock = None
    lock = acquired_lock
    if lock is None:
        own_lock = SourceSyncLock.try_acquire(landing_path, name)
        lock = own_lock
    while lock is None and time.monotonic() < deadline:
        time.sleep(min(30.0, max(0.1, deadline - time.monotonic())))
        if not in_window(datetime.now().time(), scfg.windows):
            log.info("skip reconcile source=%s reason=window_closed_while_waiting", name)
            return False
        own_lock = SourceSyncLock.try_acquire(landing_path, name)
        lock = own_lock
    if lock is None:
        log.info("skip reconcile source=%s reason=sync_or_reconcile_running", name)
        return False
    landing = LandingStore(landing_path)
    try:
        adapter = build_adapter(name, scfg, landing)
        if scfg.sink.type == "http":
            sink = build_sink(scfg, landing, source=name)
            report = reconcile_remote(
                adapter, landing, sink, name, scfg.table_watermarks(),
                deep=deep, key_columns=scfg.table_key_columns(),
                start_dates=scfg.table_start_dates(), run_id=run_id)
        else:
            report = reconcile(
                adapter, landing, name, scfg.table_watermarks(), deep=deep,
                key_columns=scfg.table_key_columns(),
                start_dates=scfg.table_start_dates(), run_id=run_id)
        log.info(
            "reconcile source=%s run=%s deep=%s segments=%s "
            "mismatched=%s soft_deleted=%s",
            name, report.run_id, deep, len(report.segments),
            len(report.mismatched), report.total_soft_deleted)
        return True
    finally:
        landing.con.close()
        if own_lock is not None:
            own_lock.release()


def _reload_source(
    config_path: str, source: str,
) -> tuple[ConnectConfig, SourceConfig] | None:
    from ...shared.config import load_config

    cfg = load_config(config_path)
    scfg = cfg.sources.get(source)
    if scfg is None:
        log.warning(
            "skip source=%s reason=removed_from_config;重启 connector 以刷新任务清单",
            source,
        )
        return None
    return cfg, scfg


def run_sync_cycle_from_config(config_path: str, source: str) -> SyncCycleResult:
    """每次触发重新读取配置，使表计划/窗口/凭据引用下一轮生效。"""
    loaded = _reload_source(config_path, source)
    if loaded is None:
        return SyncCycleResult(False, "source_removed")
    cfg, scfg = loaded
    return run_sync_cycle(source, scfg, cfg.landing, cfg.templates)


def run_reconcile_cycle_from_config(
    config_path: str, source: str, deep: bool = False,
    wait_for_lock_seconds: float = 0.0,
) -> bool:
    loaded = _reload_source(config_path, source)
    if loaded is None:
        return False
    cfg, scfg = loaded
    return run_reconcile_cycle(
        source, scfg, cfg.landing, deep=deep,
        wait_for_lock_seconds=wait_for_lock_seconds)


def serve(
    cfg: ConnectConfig, once: bool = False, *,
    config_path: str | None = None,
) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # 管理页必须能加载并解释不合规配置，但实际 connector 在生产模式
    # 必须 fail closed，禁止 local sink 或未受控磁盘 spool。
    assert_production_ready(cfg)

    # connector 被断电/强制结束时 SQLite 会留下 running。只有成功获得
    # source 锁的新实例才能回收，避免误伤另一个正常 connector。
    from .sync_lock import SourceSyncLock
    recovery_store = LandingStore(cfg.landing)
    try:
        for source in cfg.sources:
            recovery_lock = SourceSyncLock.try_acquire(cfg.landing, source)
            if recovery_lock is None:
                log.warning(
                    "skip abandoned-run recovery source=%s reason=lock_held",
                    source,
                )
                continue
            try:
                scfg = cfg.sources[source]
                if (
                    scfg.spool.policy == "encrypted_temp_volume"
                    and scfg.spool.directory
                ):
                    removed = cleanup_orphan_spools(
                        scfg.spool.directory, source)
                    if removed:
                        log.warning(
                            "removed startup orphan spools source=%s count=%s",
                            source, removed)
                recovered = recovery_store.recover_abandoned_runs(source)
                if recovered:
                    log.warning(
                        "recovered abandoned runs source=%s count=%s",
                        source, recovered,
                    )
            finally:
                recovery_lock.release()
    finally:
        recovery_store.con.close()

    if once:
        for name, scfg in cfg.sources.items():
            result = run_sync_cycle(name, scfg, cfg.landing, cfg.templates)
            log.info("once sync source=%s %s", name, result.reason)
            if scfg.reconcile_at is not None:
                run_reconcile_cycle(name, scfg, cfg.landing)
        return

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    # Windows 主机休眠/短暂高负载后仍允许一小时内补跑；
    # APScheduler 默认 1 秒 misfire 会让每日对账无声丢失。
    scheduler = BlockingScheduler(job_defaults={
        "misfire_grace_time": 3600,
        "coalesce": True,
        "max_instances": 1,
    })
    for name, scfg in cfg.sources.items():
        next_run_time = scfg.sync_start_datetime_after(datetime.now())
        sync_func = run_sync_cycle
        sync_args = (name, scfg, cfg.landing, cfg.templates)
        if config_path is not None:
            sync_func = run_sync_cycle_from_config
            sync_args = (config_path, name)
        scheduler.add_job(
            sync_func, IntervalTrigger(seconds=scfg.sync_every_seconds()),
            args=sync_args, id=f"sync:{name}",
            max_instances=1, coalesce=True,
            next_run_time=next_run_time)
        for deep, at in (
            (False, scfg.reconcile_at),
            (True, scfg.reconcile_deep_at),
        ):
            if not at:
                continue
            hh, mm = at.split(":")
            reconcile_func = run_reconcile_cycle
            reconcile_args = (name, scfg, cfg.landing, deep, 3600.0)
            if config_path is not None:
                reconcile_func = run_reconcile_cycle_from_config
                reconcile_args = (config_path, name, deep, 3600.0)
            cron_kwargs = {"hour": int(hh), "minute": int(mm)}
            if deep and scfg.reconcile_deep_day_of_week:
                cron_kwargs["day_of_week"] = scfg.reconcile_deep_day_of_week
            scheduler.add_job(
                reconcile_func, CronTrigger(**cron_kwargs),
                args=reconcile_args,
                id=f"reconcile{'-deep' if deep else ''}:{name}",
                max_instances=1, coalesce=True)
        log.info(
            "scheduled source=%s sync_every=%s sync_start_at=%s "
            "next_run_time=%s reconcile_at=%s windows=%s tables=%s",
            name, scfg.sync_every, scfg.sync_start_at, next_run_time.isoformat(),
            scfg.reconcile_at, scfg.windows or "不限", len(scfg.table_whitelist()))
    scheduler.start()
