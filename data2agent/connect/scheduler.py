"""调度常驻(E5):按 connect.yaml 排程 sync(+apply)与每日 L1 对账,窗口硬约束。

- 窗口外不发起;运行中越界 → incremental_sync 在批次边界优雅暂停(paused),
  水位机制保证下窗口自然续跑;
- 每轮汇总在 d2a_sync_run,结构化日志(key=value)输出;
- serve --once 立即各跑一轮(仍尊重窗口),用于验证配置与容器环境。
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..metamodel.loader import load_pack
from ..metamodel.schema import TemplatePack
from .config import ConnectConfig, SourceConfig, in_window
from .increment import incremental_sync, watermarks_from_pack
from .landing import LandingStore
from .mapping_apply import apply_objects
from .reconcile import reconcile
from .sync import whitelist_from_pack

log = logging.getLogger("data2agent.connect")


def build_adapter(name: str, scfg: SourceConfig, pack: TemplatePack,
                  landing: LandingStore):
    whitelist = whitelist_from_pack(pack, name) if scfg.whitelist_from_bindings else set()
    whitelist |= set(scfg.extra_whitelist)
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


def run_sync_cycle(name: str, scfg: SourceConfig, pack: TemplatePack,
                   landing_path: str) -> bool:
    """一轮 sync(+apply)。返回是否实际执行(窗口外为 False)。

    每轮自建 LandingStore:apscheduler 任务跑在工作线程,
    sqlite 连接不可跨线程复用。
    """
    if not in_window(datetime.now().time(), scfg.windows):
        log.info("skip source=%s reason=窗口外 windows=%s", name, scfg.windows)
        return False
    landing = LandingStore(landing_path)
    adapter = build_adapter(name, scfg, pack, landing)
    report = incremental_sync(
        adapter, landing, name, watermarks_from_pack(pack, name),
        lookback_days=scfg.lookback_days(),
        should_continue=lambda: in_window(datetime.now().time(), scfg.windows))
    log.info("sync source=%s run=%s rows=%s tables=%s paused=%s",
             name, report.run_id, report.total_rows, len(report.tables), report.paused)
    if scfg.apply_after_sync and not report.paused:
        apply_report = apply_objects(landing, pack, name)
        log.info("apply source=%s objects=%s quarantined=%s aborted=%s",
                 name, len(apply_report.results),
                 sum(r.quarantined for r in apply_report.results),
                 [r.object for r in apply_report.aborted])
    return True


def run_reconcile_cycle(name: str, scfg: SourceConfig, pack: TemplatePack,
                        landing_path: str, deep: bool = False) -> bool:
    if not in_window(datetime.now().time(), scfg.windows):
        log.info("skip reconcile source=%s reason=窗口外", name)
        return False
    landing = LandingStore(landing_path)
    adapter = build_adapter(name, scfg, pack, landing)
    report = reconcile(adapter, landing, name, watermarks_from_pack(pack, name), deep=deep)
    log.info("reconcile source=%s run=%s segments=%s mismatched=%s soft_deleted=%s",
             name, report.run_id, len(report.segments),
             len(report.mismatched), report.total_soft_deleted)
    return True


def serve(cfg: ConnectConfig, once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    pack = load_pack(cfg.templates)

    if once:
        for name, scfg in cfg.sources.items():
            run_sync_cycle(name, scfg, pack, cfg.landing)
            if scfg.reconcile_at is not None:
                run_reconcile_cycle(name, scfg, pack, cfg.landing)
        return

    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler()
    for name, scfg in cfg.sources.items():
        scheduler.add_job(
            run_sync_cycle, IntervalTrigger(seconds=scfg.sync_every_seconds()),
            args=(name, scfg, pack, cfg.landing), id=f"sync:{name}",
            max_instances=1, coalesce=True,
            next_run_time=datetime.now())  # 启动即跑首轮(仍受窗口约束)
        if scfg.reconcile_at:
            hh, mm = scfg.reconcile_at.split(":")
            scheduler.add_job(
                run_reconcile_cycle, CronTrigger(hour=int(hh), minute=int(mm)),
                args=(name, scfg, pack, cfg.landing), id=f"reconcile:{name}",
                max_instances=1, coalesce=True)
        log.info("scheduled source=%s sync_every=%s reconcile_at=%s windows=%s",
                 name, scfg.sync_every, scfg.reconcile_at, scfg.windows or "不限")
    scheduler.start()
