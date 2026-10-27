"""抽取 CLI:python -m data2agent.middle.extract {sync|reconcile|backfill|reset-cursor|serve|status}

sync       --config connect.yaml [--source name];抽取范围/模式来自 tables 配置
reconcile  --config connect.yaml [--deep] 分段对账 L1;抓物理删除与不动水位的原地改动
backfill   --config connect.yaml --table T --from F --to T(upsert 幂等,不动水位)
reset-cursor --config connect.yaml --table T --yes 显式清理策略不兼容的单表游标
serve      --config connect.yaml [--once] 按 connect.yaml 调度常驻(错峰窗口硬约束)
status     水位 / 最近运行概览
"""

from __future__ import annotations

import argparse

from .increment import incremental_sync
from ...shared.store.landing import LandingStore
from .reconcile import reconcile, reconcile_remote


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--config", required=True, help="connect.yaml 路径(抽取配置唯一来源)")
    sp.add_argument("--source", default="digiwin_e10", help="数据源名")


def _build(args, ap):
    """构建适配器、落地库和水位映射。所有抽取操作必须通过 --config 获取 tables 配置。"""
    from ...shared.config import load_config
    cfg = load_config(args.config)
    if args.source not in cfg.sources:
        ap.error(f"配置中没有源 '{args.source}',可用: {sorted(cfg.sources)}")
    scfg = cfg.sources[args.source]
    whitelist = scfg.table_whitelist()
    watermarks = scfg.table_watermarks()

    landing = LandingStore(cfg.landing)
    hook = lambda action, sql, rows, ms: landing.log_audit(args.source, action, sql, rows, ms)  # noqa: E731
    kwargs = dict(batch_size=scfg.rate.batch_size,
                  rows_per_second=scfg.rate.rows_per_second,
                  audit_hook=hook,
                  table_schemas=scfg.table_schemas())

    import os as _os
    if scfg.adapter == "sqlite_readonly":
        from .adapters.sqlite import SqliteReadOnlyAdapter
        path = scfg.path or _os.environ.get(scfg.dsn_env or "", "")
        adapter = SqliteReadOnlyAdapter(path, whitelist, **kwargs)
    else:
        from .adapters.mssql import MssqlReadOnlyAdapter
        dsn = _os.environ.get(scfg.dsn_env or "", "")
        if not dsn:
            ap.error(f"环境变量 {scfg.dsn_env} 为空")
        adapter = MssqlReadOnlyAdapter(dsn, whitelist, **kwargs)
    return adapter, landing, watermarks, scfg, cfg


def main() -> int:
    from ...shared.admin.secrets_file import load_home_secrets_if_present
    load_home_secrets_if_present()

    ap = argparse.ArgumentParser(description="data2agent 抽取(只读:增量 / 全量 / 对账)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sync", help="同步到落地库(按 tables.*.mode:增量或全量快照)")
    _add_common(sp)

    rp = sub.add_parser("reconcile", help="分段对账(L1;--deep 全段修复)")
    _add_common(rp)
    rp.add_argument("--deep", action="store_true",
                    help="全段 L2 修复(兜底不动水位的原地改动)")

    bp = sub.add_parser("backfill", help="指定表的水位区间重抽(不动水位)")
    _add_common(bp)
    bp.add_argument("--table", required=True, help="源表名(须有水位声明)")
    bp.add_argument("--from", dest="wm_from", required=True, help="水位起(含)")
    bp.add_argument("--to", dest="wm_to", required=True, help="水位止(不含)")

    cp = sub.add_parser(
        "reset-cursor",
        help="显式清理单表增量游标；下一轮将从配置 start_date 重新同步")
    _add_common(cp)
    cp.add_argument("--table", required=True, help="配置中的增量表名")
    cp.add_argument(
        "--yes", action="store_true",
        help="确认清理游标（raw 数据保留，下一轮按配置下界重抽）")

    vp = sub.add_parser("serve", help="按 connect.yaml 调度常驻(窗口硬约束)")
    vp.add_argument("--config", default="connect.yaml", help="配置文件路径")
    vp.add_argument("--once", action="store_true", help="立即各跑一轮后退出(仍尊重窗口)")

    tp = sub.add_parser("status", help="水位 / 最近运行概览")
    tp.add_argument("--landing", default="landing/factory.sqlite")
    tp.add_argument("--source", default="digiwin_e10")

    args = ap.parse_args()

    if args.cmd == "serve":
        from ...shared.config import load_config
        from .scheduler import serve
        serve(
            load_config(args.config), once=args.once,
            config_path=None if args.once else args.config)
        return 0

    if args.cmd == "status":
        return _status(args)

    adapter, landing, watermarks, scfg, cfg = _build(args, ap)

    # 一次性 CLI 与 serve 调度必须使用同一 schema 屏障；否则运维人员手工
    # 执行 sync/reconcile 会绕过管理页已确认的结构指纹。
    if args.cmd in ("sync", "reconcile"):
        from ...shared.config import assert_production_ready
        from .scheduler import validate_configured_schemas
        assert_production_ready(cfg)
        validate_configured_schemas(args.source, scfg, landing)

    if args.cmd == "reset-cursor":
        if args.table not in watermarks:
            ap.error(
                f"表 {args.table} 不在配置的增量表中。增量表: {sorted(watermarks)}")
        if not args.yes:
            ap.error(
                "reset-cursor 会让下一轮从配置 start_date（未配置则源头）重抽；"
                "确认后请加 --yes")
        removed = landing.reset_sync_cursor(args.source, args.table)
        state = "已清理" if removed else "原本不存在"
        print(
            f"游标{state}:{args.source}.{args.table}；平台业务数据未删除，"
            "下一轮 sync 将从配置下界重新同步")
        return 0

    if args.cmd == "backfill":
        if scfg.sink.type != "local":
            landing.con.close()
            ap.error(
                "backfill 的旧实现只支持开发环境 local sink；HTTP 推送模式请通过"
                " reset-cursor 后执行受 generation 保护的同步，禁止在中间机创建 Raw 表")
        wm_col = watermarks.get(args.table)
        if wm_col is None:
            ap.error(f"表 {args.table} 不在配置的增量表中,无法按区间回补。"
                     f"增量表: {sorted(watermarks)}")
        from .adapters.base import resolve_runtime_keys
        raw_info = adapter.table_info(args.table)
        info = resolve_runtime_keys(
            raw_info, scfg.table_key_columns().get(args.table), require_keys=True)
        adapter.validate_runtime_keys(info)
        landing.ensure_raw_table(args.source, info)
        import uuid
        batch_id = uuid.uuid4().hex[:12]
        rows = sum(
            landing.upsert_rows(args.source, info, batch, batch_id)
            for batch in adapter.read_segment(info, wm_col, args.wm_from, args.wm_to))
        print(f"回补完成:{args.table} [{args.wm_from}, {args.wm_to}) 共 {rows} 行"
              "(水位未变动)")
        return 0

    if args.cmd == "sync":
        from .scheduler import build_sink
        sink = build_sink(scfg, landing, source=args.source)
        report = incremental_sync(
            adapter, landing, args.source, watermarks,
            lookback_days=scfg.lookback_days(),
            key_columns=scfg.table_key_columns(),
            # 与 serve 调度路径一致:start_date 下界在一次性命令同样生效
            start_dates=scfg.table_start_dates(),
            estimate_rows=scfg.estimate_rows,
            full_refresh_spool_policy=scfg.spool.policy,
            spool_directory=scfg.spool.directory,
            sink=sink,
        )
        print(f"同步完成:run #{report.run_id},{len(report.tables)} 表,"
              f"共 {report.total_rows} 行 → {cfg.landing}")
        for t in report.tables:
            wm = f"  水位 → {t.high_water}" if t.high_water else ""
            print(f"  - {t.table:<16} {t.rows:>6} 行 / {t.batches} 批  [{t.strategy}]{wm}")
        return 0

    from .scheduler import unsupported_reconcile
    blocked = unsupported_reconcile(scfg)
    if blocked:
        landing.con.close()
        ap.error(blocked)

    if scfg.sink.type == "http":
        from .scheduler import build_sink
        sink = build_sink(scfg, landing, source=args.source)
        report = reconcile_remote(
            adapter, landing, sink, args.source, watermarks, deep=args.deep,
            key_columns=scfg.table_key_columns(),
            start_dates=scfg.table_start_dates())
    else:
        report = reconcile(
            adapter, landing, args.source, watermarks, deep=args.deep,
            key_columns=scfg.table_key_columns(),
            start_dates=scfg.table_start_dates())
    mode = "deep" if args.deep else "L1"
    print(f"对账完成({mode}):run #{report.run_id},检查 {len(report.segments)} 段,"
          f"不一致 {len(report.mismatched)} 段,软删 {report.total_soft_deleted} 行")
    for s in report.segments:
        if not s.consistent or s.repaired_rows or s.soft_deleted:
            mark = "≠" if not s.consistent else "="
            print(f"  - {s.table:<16} {s.segment}  源 {s.src_count} {mark} 落地 {s.dst_count}"
                  f"  重抽 {s.repaired_rows} 行, 软删 {s.soft_deleted} 行")
    return 0


def _status(args) -> int:
    landing = LandingStore(args.landing)
    print(f"== 水位状态(source={args.source})==")
    rows = landing.list_sync_watermarks(args.source)
    for r in rows:
        print(f"  {r['table_name']:<16} {r['watermark_col']} → {r['high_water']}"
              f"  (最近 {r['last_run_at']})")
    if not rows:
        print("  (尚无水位状态,先跑 sync)")
    print("== 最近运行 ==")
    for r in landing.con.execute(
            "SELECT id, started_at, finished_at, status, tables, rows, detail "
            "FROM d2a_sync_run WHERE source = ? ORDER BY id DESC LIMIT 5", (args.source,)):
        print(f"  #{r['id']} {r['started_at']} → {r['finished_at']} [{r['status']}]"
              f" tables={r['tables']} rows={r['rows']} {r['detail'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
