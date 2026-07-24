"""调度常驻(E5):按 connect.yaml 排程 sync(+apply)与每日 L1 对账,窗口硬约束。

- 窗口外不发起;运行中越界 → incremental_sync 在批次边界优雅暂停(paused),
  水位机制保证下窗口自然续跑;
- 每轮汇总在 d2a_sync_run,结构化日志(key=value)输出;
- serve --once 立即各跑一轮(仍尊重窗口),用于验证配置与容器环境。
"""

from __future__ import annotations

import logging
from datetime import datetime

from .config import ConnectConfig, SourceConfig, in_window
from .dataset_publish import build_dataset
from .increment import incremental_sync
from .landing import LandingStore
from .reconcile import reconcile

log = logging.getLogger("data2agent.connect")


def build_adapter(name: str, scfg: SourceConfig,
                  landing: LandingStore):
    """构建适配器:白名单仅从 tables 配置生成。"""
    whitelist = scfg.table_whitelist()
    hook = lambda action, sql, rows, ms: landing.log_audit(name, action, sql, rows, ms)  # noqa: E731
    kwargs = dict(batch_size=scfg.rate.batch_size,
                  rows_per_second=scfg.rate.rows_per_second, audit_hook=hook)
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


def build_sink(scfg: SourceConfig, landing: LandingStore):
    """按 sink 配置构建落地出口:local=本地库;http=推给平台(§12.3)。"""
    if scfg.sink.type == "http":
        import os

        from .sink import HttpPushSink
        token = os.environ.get(scfg.sink.token_env or "", "") or None
        return HttpPushSink(scfg.sink.url, token)
    from .sink import LocalSink
    return LocalSink(landing)


def run_sync_cycle(name: str, scfg: SourceConfig,
                   landing_path: str, templates: str = "templates") -> bool:
    """一轮 sync(+apply)。返回是否实际执行(窗口外为 False)。
    若 tables 为空则跳过，不连接 ERP、不报错、不创建失败运行。"""
    if not in_window(datetime.now().time(), scfg.windows):
        log.info("skip source=%s reason=窗口外 windows=%s", name, scfg.windows)
        return False
    if not scfg.table_whitelist():
        log.info("source=%s reason=tables_unconfigured", name)
        return False
    landing = LandingStore(landing_path)
    adapter = build_adapter(name, scfg, landing)
    sink = build_sink(scfg, landing)
    watermarks = scfg.table_watermarks()
    report = incremental_sync(
        adapter, landing, name, watermarks,
        lookback_days=scfg.lookback_days(), sink=sink,
        should_continue=lambda: in_window(datetime.now().time(), scfg.windows))
    log.info("sync source=%s run=%s rows=%s tables=%s paused=%s sink=%s",
             name, report.run_id, report.total_rows, len(report.tables),
             report.paused, scfg.sink.type)
    # sink=http: raw 已推给平台,映射在平台侧跑,不在中间 apply
    if scfg.apply_after_sync and not report.paused and scfg.sink.type == "local":
        from ..metamodel.loader import load_pack as _load_pack
        pack = _load_pack(templates)  # local apply 仍需模板
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
    return True


def run_reconcile_cycle(name: str, scfg: SourceConfig,
                        landing_path: str, deep: bool = False) -> bool:
    if not in_window(datetime.now().time(), scfg.windows):
        log.info("skip reconcile source=%s reason=窗口外", name)
        return False
    landing = LandingStore(landing_path)
    adapter = build_adapter(name, scfg, landing)
    report = reconcile(adapter, landing, name, scfg.table_watermarks(), deep=deep)
    log.info("reconcile source=%s run=%s segments=%s mismatched=%s soft_deleted=%s",
             name, report.run_id, len(report.segments),
             len(report.mismatched), report.total_soft_deleted)
    return True


def serve(cfg: ConnectConfig, once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if once:
        for name, scfg in cfg.sources.items():
            run_sync_cycle(name, scfg, cfg.landing, cfg.templates)
            if scfg.reconcile_at is not None:
                run_reconcile_cycle(name, scfg, cfg.landing)
        return

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler()
    for name, scfg in cfg.sources.items():
        scheduler.add_job(
            run_sync_cycle, IntervalTrigger(seconds=scfg.sync_every_seconds()),
            args=(name, scfg, cfg.landing, cfg.templates), id=f"sync:{name}",
            max_instances=1, coalesce=True,
            next_run_time=datetime.now())
        if scfg.reconcile_at:
            hh, mm = scfg.reconcile_at.split(":")
            scheduler.add_job(
                run_reconcile_cycle, CronTrigger(hour=int(hh), minute=int(mm)),
                args=(name, scfg, cfg.landing), id=f"reconcile:{name}",
                max_instances=1, coalesce=True)
        log.info("scheduled source=%s sync_every=%s reconcile_at=%s windows=%s tables=%s",
                 name, scfg.sync_every, scfg.reconcile_at, scfg.windows or "不限",
                 len(scfg.table_whitelist()))
    scheduler.start()
