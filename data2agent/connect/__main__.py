"""抽取 CLI:python -m data2agent.connect sync --sqlite 源库 | --mssql-dsn-env 环境变量

默认水位增量(binding.watermark 推导,无水位的表 full_refresh),--full 强制全量。
完整 CLI(backfill / reconcile / status / quarantine)与 connect.yaml 配置属 E5 切片。
"""

from __future__ import annotations

import argparse
import os

from ..metamodel.loader import load_pack
from .increment import DEFAULT_LOOKBACK_DAYS, incremental_sync, watermarks_from_pack
from .landing import LandingStore
from .sync import whitelist_from_pack


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent 抽取(只读:增量 / 全量落地)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sync", help="同步到落地库(默认水位增量)")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--sqlite", help="SQLite 源库路径(开发 / 展厅)")
    src.add_argument("--mssql-dsn-env", help="存放 MSSQL 连接串的环境变量名(凭据不落配置)")
    sp.add_argument("--source", default="digiwin_e10", help="binding 数据源名(推导白名单)")
    sp.add_argument("--landing", default="landing/factory.sqlite", help="落地库路径")
    sp.add_argument("--templates", default="templates", help="模板包目录")
    sp.add_argument("--batch-size", type=int, default=5000)
    sp.add_argument("--rows-per-second", type=int, default=0, help="0 为不限流(展厅);生产必配")
    sp.add_argument("--full", action="store_true", help="强制全量(忽略水位,不建立状态)")
    sp.add_argument("--lookback-days", type=float, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"回看窗口天数(默认 {DEFAULT_LOOKBACK_DAYS})")
    args = ap.parse_args()

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

    watermarks = {} if args.full else watermarks_from_pack(pack, args.source)
    report = incremental_sync(adapter, landing, args.source, watermarks,
                              lookback_days=args.lookback_days)
    print(f"同步完成:run #{report.run_id},{len(report.tables)} 表,共 {report.total_rows} 行 → {args.landing}")
    for t in report.tables:
        wm = f"  水位 → {t.high_water}" if t.high_water else ""
        print(f"  - {t.table:<16} {t.rows:>6} 行 / {t.batches} 批  [{t.strategy}]{wm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
