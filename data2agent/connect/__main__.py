"""抽取 CLI:python -m data2agent.connect {sync|reconcile|apply|backfill|serve|status|quarantine}

sync       默认水位增量(binding.watermark 推导,无水位表 full_refresh),--full 强制全量
reconcile  分段对账 L1(--deep 全段 L2 修复);抓物理删除与不动水位的原地改动
apply      映射应用:raw_* → 物化对象表 obj_*(隔离区 + 熔断),纯落地库操作
backfill   指定表的水位区间重抽(upsert 幂等,不动水位)
serve      按 connect.yaml 调度常驻(错峰窗口硬约束;--once 立即各跑一轮)
status     水位 / 最近运行 / 隔离区概览
quarantine list 查看隔离明细;retry 修复后重新映射对象
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

    bp = sub.add_parser("backfill", help="指定表的水位区间重抽(不动水位)")
    _add_common(bp)
    bp.add_argument("--table", required=True, help="源表名(须有水位声明)")
    bp.add_argument("--from", dest="wm_from", required=True, help="水位起(含)")
    bp.add_argument("--to", dest="wm_to", required=True, help="水位止(不含)")

    vp = sub.add_parser("serve", help="按 connect.yaml 调度常驻(窗口硬约束)")
    vp.add_argument("--config", default="connect.yaml", help="配置文件路径")
    vp.add_argument("--once", action="store_true", help="立即各跑一轮后退出(仍尊重窗口)")

    tp = sub.add_parser("status", help="水位 / 最近运行 / 隔离区概览")
    tp.add_argument("--landing", default="landing/factory.sqlite")
    tp.add_argument("--source", default="digiwin_e10")

    qp = sub.add_parser("quarantine", help="隔离区查看与重试")
    qp.add_argument("action", choices=["list", "retry"])
    qp.add_argument("--object", help="对象名(retry 必填;list 可选过滤)")
    qp.add_argument("--source", default="digiwin_e10")
    qp.add_argument("--landing", default="landing/factory.sqlite")
    qp.add_argument("--templates", default="templates")

    args = ap.parse_args()

    if args.cmd == "serve":
        from .config import load_config
        from .scheduler import serve
        serve(load_config(args.config), once=args.once)
        return 0

    if args.cmd == "status":
        return _status(args)

    if args.cmd == "quarantine":
        return _quarantine(args, ap)

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

    if args.cmd == "backfill":
        wm_col = watermarks_from_pack(pack, args.source).get(args.table)
        if wm_col is None:
            ap.error(f"表 {args.table} 没有水位声明,无法按区间回补(可用 sync --full)")
        info = adapter.table_info(args.table)
        landing.ensure_raw_table(args.source, info)
        import uuid
        batch_id = uuid.uuid4().hex[:12]
        rows = sum(
            landing.upsert_rows(args.source, info, batch, batch_id)
            for batch in adapter.read_segment(info, wm_col, args.wm_from, args.wm_to))
        print(f"回补完成:{args.table} [{args.wm_from}, {args.wm_to}) 共 {rows} 行"
              "(水位未变动;如需刷新对象层请再跑 apply)")
        return 0

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


def _status(args) -> int:
    landing = LandingStore(args.landing)
    print(f"== 水位状态(source={args.source})==")
    rows = landing.con.execute(
        "SELECT table_name, watermark_col, high_water, last_run_at FROM d2a_sync_state "
        "WHERE source = ? ORDER BY table_name", (args.source,)).fetchall()
    for r in rows or []:
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
    print("== 隔离区(未处理)==")
    q = landing.con.execute(
        "SELECT object, COUNT(*) n FROM d2a_quarantine "
        "WHERE source = ? AND resolved_at IS NULL GROUP BY object", (args.source,)).fetchall()
    for r in q:
        print(f"  {r['object']}: {r['n']} 行")
    if not q:
        print("  (空)")
    return 0


def _quarantine(args, ap) -> int:
    landing = LandingStore(args.landing)
    if args.action == "list":
        where, params = "source = ? AND resolved_at IS NULL", [args.source]
        if args.object:
            where += " AND object = ?"
            params.append(args.object)
        rows = landing.con.execute(
            f"SELECT id, object, keys_json, reason, created_at FROM d2a_quarantine "
            f"WHERE {where} ORDER BY id", params).fetchall()
        for r in rows:
            print(f"  #{r['id']} [{r['object']}] {r['keys_json']}  {r['reason']}"
                  f"  ({r['created_at']})")
        print(f"共 {len(rows)} 行未处理")
        return 0
    # retry:修好源数据 / binding 后,对该对象重新映射(成功则旧记录自动标记取代)
    if not args.object:
        ap.error("quarantine retry 需要 --object")
    from ..metamodel.loader import load_pack as _load
    from .mapping_apply import MappingCircuitBreaker, apply_object
    pack = _load(args.templates)
    tpl = next((o for o in pack.objects if o.object == args.object), None)
    if tpl is None:
        ap.error(f"未知对象 {args.object}")
    try:
        result = apply_object(landing, tpl, args.source)
    except MappingCircuitBreaker as e:
        print(f"重试失败(熔断):{e}")
        return 1
    print(f"重试完成:{result.object} 映射 {result.mapped} 行, 仍隔离 {result.quarantined} 行")
    return 0 if result.quarantined == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
