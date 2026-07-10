"""抽取 CLI:python -m data2agent.connect {sync|reconcile|apply}

sync      默认水位增量(binding.watermark 推导,无水位表 full_refresh),--full 强制全量
reconcile 分段对账 L1(--deep 全段 L2 修复);抓物理删除与不动水位的原地改动
apply     映射应用:raw_* → 物化对象表 obj_*(隔离区 + 熔断),纯落地库操作
完整调度(窗口 / 限流常驻)与 connect.yaml 配置属 E5 切片。
"""

from __future__ import annotations

import argparse
import os

from ..metamodel.loader import load_pack
from .increment import DEFAULT_LOOKBACK_DAYS, incremental_sync, watermarks_from_pack
from .landing import LandingStore
from .mapping_apply import DEFAULT_BREAKER_THRESHOLD, apply_objects
from .reconcile import reconcile
from .sync import whitelist_from_pack


def _add_common(sp: argparse.ArgumentParser) -> None:
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--sqlite", help="SQLite 源库路径(开发 / 展厅)")
    src.add_argument("--mssql-dsn-env", help="存放 MSSQL 连接串的环境变量名(凭据不落配置)")
    sp.add_argument("--source", default="digiwin_e10", help="binding 数据源名(推导白名单)")
    sp.add_argument("--landing", default="landing/factory.sqlite", help="落地库路径")
    sp.add_argument("--templates", default="templates", help="模板包目录")
    sp.add_argument("--batch-size", type=int, default=5000)
    sp.add_argument("--rows-per-second", type=int, default=0, help="0 为不限流(展厅);生产必配")


def _build(args, ap):
    pack = load_pack(args.templates)
    whitelist = whitelist_from_pack(pack, args.source)
    if not whitelist:
        ap.error(f"模板中没有 source={args.source} 的 binding,白名单为空")
    landing = LandingStore(args.landing)
    hook = lambda action, sql, rows, ms: landing.log_audit(args.source, action, sql, rows, ms)  # noqa: E731
    kwargs = dict(batch_size=args.batch_size, rows_per_second=args.rows_per_second,
                  audit_hook=hook)
    if args.sqlite:
        from .adapters.sqlite import SqliteReadOnlyAdapter
        adapter = SqliteReadOnlyAdapter(args.sqlite, whitelist, **kwargs)
    else:
        dsn = os.environ.get(args.mssql_dsn_env, "")
        if not dsn:
            ap.error(f"环境变量 {args.mssql_dsn_env} 为空")
        from .adapters.mssql import MssqlReadOnlyAdapter
        adapter = MssqlReadOnlyAdapter(dsn, whitelist, **kwargs)
    return pack, adapter, landing


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent 抽取(只读:增量 / 全量 / 对账)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sync", help="同步到落地库(默认水位增量)")
    _add_common(sp)
    sp.add_argument("--full", action="store_true", help="强制全量(忽略水位,不建立状态)")
    sp.add_argument("--lookback-days", type=float, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"回看窗口天数(默认 {DEFAULT_LOOKBACK_DAYS})")

    rp = sub.add_parser("reconcile", help="分段对账(L1;--deep 全段修复)")
    _add_common(rp)
    rp.add_argument("--deep", action="store_true",
                    help="全段 L2 修复(兜底不动水位的原地改动)")

    mp = sub.add_parser("apply", help="映射应用:raw_* → obj_*(隔离区 + 熔断)")
    mp.add_argument("--source", default="digiwin_e10", help="binding 数据源名")
    mp.add_argument("--landing", default="landing/factory.sqlite", help="落地库路径")
    mp.add_argument("--templates", default="templates", help="模板包目录")
    mp.add_argument("--threshold", type=float, default=DEFAULT_BREAKER_THRESHOLD,
                    help=f"熔断阈值(隔离率,默认 {DEFAULT_BREAKER_THRESHOLD:.0%})")

    args = ap.parse_args()

    if args.cmd == "apply":
        pack = load_pack(args.templates)
        report = apply_objects(LandingStore(args.landing), pack, args.source,
                               threshold=args.threshold)
        for r in report.results:
            mark = "⚠ 熔断" if r.status == "aborted" else "ok"
            print(f"  - {r.object:<16} 映射 {r.mapped:>5} 行, 隔离 {r.quarantined} 行  [{mark}]")
        if report.aborted:
            print(f"映射中止对象:{[r.object for r in report.aborted]}(旧对象表保留,"
                  "明细见 d2a_quarantine)")
            return 1
        print(f"映射应用完成:{len(report.results)} 个对象 → {args.landing}")
        return 0

    pack, adapter, landing = _build(args, ap)

    if args.cmd == "sync":
        watermarks = {} if args.full else watermarks_from_pack(pack, args.source)
        report = incremental_sync(adapter, landing, args.source, watermarks,
                                  lookback_days=args.lookback_days)
        print(f"同步完成:run #{report.run_id},{len(report.tables)} 表,"
              f"共 {report.total_rows} 行 → {args.landing}")
        for t in report.tables:
            wm = f"  水位 → {t.high_water}" if t.high_water else ""
            print(f"  - {t.table:<16} {t.rows:>6} 行 / {t.batches} 批  [{t.strategy}]{wm}")
        return 0

    report = reconcile(adapter, landing, args.source,
                       watermarks_from_pack(pack, args.source), deep=args.deep)
    mode = "deep" if args.deep else "L1"
    print(f"对账完成({mode}):run #{report.run_id},检查 {len(report.segments)} 段,"
          f"不一致 {len(report.mismatched)} 段,软删 {report.total_soft_deleted} 行")
    for s in report.segments:
        if not s.consistent or s.repaired_rows or s.soft_deleted:
            mark = "≠" if not s.consistent else "="
            print(f"  - {s.table:<16} {s.segment}  源 {s.src_count} {mark} 落地 {s.dst_count}"
                  f"  重抽 {s.repaired_rows} 行, 软删 {s.soft_deleted} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
