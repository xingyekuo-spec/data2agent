"""M6 Validation Run：只读事实收敛与不可变报告构造。

本模块不调用 sync/apply/publish/rollback/retry，也不读取环境变量、Token、
原始行或 SQL。它只读取 LandingStore、当前模板与已发布快照，最终由调用方在
同一短事务中持久化 validation run/report/check。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..connect.config import ConnectConfig
from ..connect.dataset_publish import PublishedSnapshotError, resolve_published_snapshot
from ..connect.landing import LandingStore
from ..metamodel.schema import TemplatePack
from ..mcp_server.evidence import is_valid_digest
from . import data_browser as br

REPORT_SCHEMA_VERSION = 1
CHECK_ORDER = (
    "service_reachable", "source_connectivity", "readonly_whitelist",
    "sync_execution", "landing_and_push", "raw_presence", "published_dataset",
    "quarantine_breaker", "mapping_preview", "mcp_query", "masking",
    "evidence_integrity", "cross_surface_consistency",
)
_CHECK_TITLES = {
    "service_reachable": "服务与落地库可读",
    "source_connectivity": "数据源连接配置",
    "readonly_whitelist": "只读适配器与显式表清单",
    "sync_execution": "同步执行记录",
    "landing_and_push": "落地与推送摘要",
    "raw_presence": "Raw 表存在性",
    "published_dataset": "已发布数据集",
    "quarantine_breaker": "隔离与熔断阈值",
    "mapping_preview": "映射治理状态",
    "mcp_query": "MCP 查询证据",
    "masking": "敏感字段脱敏",
    "evidence_integrity": "证据完整性",
    "cross_surface_consistency": "跨界面版本一致性",
}


def _next_expected_run(last_finished: datetime, source_cfg) -> datetime | None:
    """计算上次成功运行后下一次预期运行时间,考虑错峰窗口。

    无窗口: last_finished + sync_every
    有窗口: 找到 >= (last_finished + sync_every) 且落在窗口内的最早时刻
    无法计算时返回 None
    """
    from ..connect.config import parse_window
    sync_s = source_cfg.sync_every_seconds() if source_cfg else 1800
    windows = source_cfg.windows if source_cfg else []

    if not windows:
        return last_finished + timedelta(seconds=sync_s)

    candidate = last_finished + timedelta(seconds=sync_s)

    def _in_window(dt: datetime) -> bool:
        t = dt.time()
        for w in windows:
            start, end = parse_window(w)
            if start <= end:
                if start <= t < end:
                    return True
            elif t >= start or t < end:
                return True
        return False

    # 收集所有窗口边界(当天分钟数),用于快速跳跃
    boundaries: set[int] = set()
    for w in windows:
        start, end = parse_window(w)
        boundaries.add(start.hour * 60 + start.minute)
        boundaries.add(end.hour * 60 + end.minute)
    boundaries = sorted(boundaries)

    # 搜索最多 366 天
    day = candidate.date()
    for _ in range(366):
        day_start = datetime(day.year, day.month, day.day)
        # 当天:从 candidate 开始,跳到下一个窗口边界
        check = max(candidate, day_start)
        end_of_day = day_start + timedelta(days=1)
        while check < end_of_day:
            if _in_window(check):
                return check
            # 跳到下一个窗口边界(跳过窗口外的空白区间)
            minute_of_day = check.hour * 60 + check.minute
            nxt = end_of_day  # 默认跳到下一天
            for b in boundaries:
                if b > minute_of_day:
                    nxt = day_start + timedelta(minutes=b)
                    break
            check = nxt
        day += timedelta(days=1)
        candidate = datetime(day.year, day.month, day.day)

    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _evidence(kind: str, label: str, href: str) -> dict[str, str]:
    return {"kind": kind, "label": label[:160], "href": href}


def _check(
    check_id: str, status: str, summary: str, *, blocking: bool = True,
    detail: dict[str, Any] | None = None, evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    stamp = _now()
    return {
        "check_id": check_id,
        "title": _CHECK_TITLES[check_id],
        "status": status,
        "blocking": blocking,
        "summary": summary[:500],
        "started_at": stamp,
        "finished_at": _now(),
        "detail": detail or {},
        "evidence": evidence or [],
    }


def _overall(checks: list[dict[str, Any]]) -> str:
    if any(c["status"] == "fail" and c["blocking"] for c in checks):
        return "fail"
    if any(c["status"] == "warning" for c in checks):
        return "warning"
    if any(c["status"] == "skipped" for c in checks):
        return "warning"
    return "pass"


def _safe_json(value: str) -> dict | list | None:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _latest_run(db: LandingStore, source: str, run_types: tuple[str, ...]):
    placeholders = ",".join("?" for _ in run_types)
    return db.con.execute(
        "SELECT * FROM d2a_sync_run WHERE source = ? AND run_type IN (" + placeholders
        + ") ORDER BY started_at DESC, id DESC LIMIT 1",
        (source, *run_types),
    ).fetchone()


def _validation_context(
    db: LandingStore, pack: TemplatePack, source: str,
) -> tuple[dict[str, Any], Any | None, str | None]:
    """在 run 开始处冻结已发布快照；后续检查不得重新解析 current pointer。"""
    try:
        snap = resolve_published_snapshot(db, source)
    except PublishedSnapshotError as exc:
        return {"template_version": pack.version, "snapshot_error": exc.reason_code}, None, exc.reason_code
    return {
        "template_version": pack.version,
        "dataset_version": snap.dataset_version,
        "published_template_version": snap.template_version,
        "object_count": len(snap.objects),
    }, snap, None


def build_validation_report(
    db: LandingStore, *, run_id: int, pack: TemplatePack, source: str,
    config: ConnectConfig | None, include_mcp_probe: bool,
    mcp_probe: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """读取一次冻结快照，构造固定 13 项报告（不做任何业务写入）。"""
    started_at = _now()
    context, snap, snapshot_error = _validation_context(db, pack, source)
    checks: list[dict[str, Any]] = []

    # 1. service
    try:
        db.con.execute("SELECT 1").fetchone()
        checks.append(_check("service_reachable", "pass", "Console 已能读取落地库。",
                             evidence=[_evidence("run", "本次验收运行", f"/api/runs/{run_id}")]))
    except sqlite3.Error:
        checks.append(_check("service_reachable", "fail", "落地库不可读。"))

    source_cfg = config.sources.get(source) if config is not None else None
    if config is None:
        checks.append(_check("source_connectivity", "skipped", "未加载连接配置，不能执行源连接检查。",
                             blocking=False))
    elif source_cfg is None:
        checks.append(_check("source_connectivity", "fail", "当前数据源不在连接配置中。"))
    else:
        checks.append(_check("source_connectivity", "pass", "数据源配置已加载；本次未发起写操作。",
                             detail={"adapter": source_cfg.adapter}))

    if source_cfg is None:
        checks.append(_check("readonly_whitelist", "skipped", "未加载对应数据源配置。", blocking=False))
    else:
        readonly = source_cfg.adapter in ("sqlite_readonly", "mssql_readonly")
        tables_configured = source_cfg.tables is not None and len(source_cfg.tables) > 0
        if readonly and tables_configured:
            checks.append(_check("readonly_whitelist", "pass",
                                 f"只读适配器与显式表清单已启用({len(source_cfg.tables)} 张表)。",
                                 detail={"adapter": source_cfg.adapter,
                                         "table_count": len(source_cfg.tables)}))
        else:
            checks.append(_check("readonly_whitelist", "fail",
                                 "只读适配器或显式表清单未满足。",
                                 detail={"readonly_adapter": readonly,
                                         "tables_configured": tables_configured}))

    # 4. sync / landing facts
    sync = _latest_run(db, source, ("sync", "ingest"))
    if sync is None:
        checks.append(_check("sync_execution", "fail", "未找到成功的同步或接收运行。"))
    elif sync["status"] == "ok":
        checks.append(_check("sync_execution", "pass", "最近同步或接收运行成功。",
                             evidence=[_evidence("run", f"运行 #{sync['id']}", f"/api/runs/{sync['id']}")]))
    else:
        checks.append(_check("sync_execution", "fail", "最近同步或接收运行未成功。",
                             evidence=[_evidence("run", f"运行 #{sync['id']}", f"/api/runs/{sync['id']}")]))

    if source_cfg is None or source_cfg.sink.type == "local":
        checks.append(_check("landing_and_push", "skipped", "当前为本地落地模式，无独立推送环节。", blocking=False))
    elif sync is not None and sync["status"] == "ok":
        checks.append(_check("landing_and_push", "pass", "最近接收运行成功。",
                             evidence=[_evidence("run", f"运行 #{sync['id']}", f"/api/runs/{sync['id']}")]))
    else:
        checks.append(_check("landing_and_push", "fail", "推送模式缺少成功的接收运行。"))

    # Raw presence: 逐表存在性 + 可验证的新鲜度
    actual_raw = set(br.raw_tables(db, source))

    binding_tables = {
        table for obj in pack.objects for binding in obj.bindings
        if binding.enabled and binding.source == source for table in binding.tables
    } if pack is not None else set()

    missing_from_raw = [t for t in binding_tables if t not in actual_raw]

    if not binding_tables:
        checks.append(_check("raw_presence", "skipped",
                             "模板未声明该数据源的 Raw 表。", blocking=False))
    elif missing_from_raw:
        checks.append(_check("raw_presence", "fail",
                             f"平台 binding 依赖 {len(missing_from_raw)} 张表在落地库中缺失 raw 数据: "
                             + ", ".join(sorted(missing_from_raw)),
                             detail={"binding_tables": sorted(binding_tables),
                                     "actual_raw_tables": sorted(actual_raw),
                                     "missing": sorted(missing_from_raw)}))
    else:
        # 逐表新鲜度:本地水位状态或明确的 table-complete 事件。
        # HTTP batch 只表示一批数据落地，绝不能视为整表同步完成。
        per_table_last: dict[str, datetime] = {}
        # 本地增量表:水位状态。
        for r in db.con.execute(
            "SELECT table_name, last_run_at FROM d2a_sync_state WHERE source = ?",
            (source,)
        ).fetchall():
            if r["last_run_at"]:
                try:
                    per_table_last[r["table_name"]] = datetime.fromisoformat(r["last_run_at"])
                except ValueError:
                    pass
        # 本地 full_refresh / HTTP table-complete:表级 run step(确保源隔离)。
        for r in db.con.execute(
            "SELECT s.target, MAX(s.finished_at) as last_ok "
            "FROM d2a_run_step s JOIN d2a_sync_run r ON s.run_id = r.id "
            "WHERE r.source = ? AND s.kind = 'table' AND s.status = 'ok' AND s.target != '' "
            "GROUP BY s.target",
            (source,)
        ).fetchall():
            if r["last_ok"]:
                tbl = r["target"]
                try:
                    dt = datetime.fromisoformat(r["last_ok"])
                    if tbl not in per_table_last or dt > per_table_last[tbl]:
                        per_table_last[tbl] = dt
                except ValueError:
                    pass

        # 逐表判断
        stale_tables: list[str] = []
        unverified_tables: list[str] = []
        sync_every_s = (source_cfg.sync_every_seconds() if source_cfg else 1800)
        grace_s = max(sync_every_s * 2, 600)
        now = datetime.now()

        for tbl in sorted(binding_tables):
            last_dt = per_table_last.get(tbl)
            if last_dt is None:
                unverified_tables.append(tbl)
            else:
                next_expected = _next_expected_run(last_dt, source_cfg)
                if next_expected is None:
                    next_expected = last_dt + timedelta(seconds=sync_every_s)
                if now > next_expected + timedelta(seconds=grace_s):
                    stale_tables.append(tbl)

        if unverified_tables or stale_tables:
            parts: list[str] = []
            if unverified_tables:
                parts.append(f"{len(unverified_tables)} 张表缺少同步记录: "
                             + ", ".join(unverified_tables))
            if stale_tables:
                parts.append(f"{len(stale_tables)} 张表同步已过期: "
                             + ", ".join(stale_tables))
            checks.append(_check("raw_presence", "warning",
                                 "；".join(parts),
                                 blocking=False,
                                 detail={"unverified": unverified_tables,
                                         "stale": stale_tables,
                                         "per_table_last": {
                                             t: per_table_last[t].isoformat(timespec="seconds")
                                             for t in per_table_last},
                                         "windows": source_cfg.windows if source_cfg else [],
                                         "sync_every": source_cfg.sync_every if source_cfg else "?",
                                 }))
        else:
            checks.append(_check("raw_presence", "pass",
                                 f"平台 binding 依赖的 {len(binding_tables)} 张表 raw 数据均存在且同步新鲜。",
                                 detail={"table_count": len(binding_tables)}))

    # 7. published snapshot
    if snap is None:
        checks.append(_check("published_dataset", "fail", "未找到可用的已发布数据集快照。",
                             detail={"reason_code": snapshot_error or "not_published"}))
    else:
        checks.append(_check("published_dataset", "pass", "已发布数据集与对象快照完整。",
                             detail={"object_count": len(snap.objects)},
                             evidence=[_evidence("dataset", "已发布数据集", f"/api/datasets/{snap.dataset_version}")]))

    # 8. quarantine breaker
    if snap is None:
        checks.append(_check("quarantine_breaker", "skipped", "没有已发布快照，无法计算对象隔离率。", blocking=False))
    else:
        rates = []
        for name, obj in snap.objects.items():
            q = db.quarantine_count(source, name)
            rates.append(q / obj.row_count if obj.row_count else (1.0 if q else 0.0))
        maximum = max(rates, default=0.0)
        if maximum >= 0.05:
            checks.append(_check("quarantine_breaker", "fail", "对象隔离率达到熔断阈值。",
                                 detail={"max_rate": round(maximum, 6), "threshold": 0.05}))
        elif maximum > 0:
            checks.append(_check("quarantine_breaker", "warning", "存在未解决隔离记录，但未达到熔断阈值。",
                                 detail={"max_rate": round(maximum, 6), "threshold": 0.05}))
        else:
            checks.append(_check("quarantine_breaker", "pass", "没有触发隔离熔断。",
                                 detail={"threshold": 0.05}))

    bindings = [b for o in pack.objects for b in o.bindings if b.enabled and b.source == source]
    drafts = sum(b.status == "draft" for b in bindings)
    if not bindings:
        checks.append(_check("mapping_preview", "skipped", "模板没有启用的映射绑定。", blocking=False))
    elif drafts:
        checks.append(_check("mapping_preview", "warning", "存在草稿映射，结果不应被视作已核验。",
                             detail={"binding_count": len(bindings), "draft_count": drafts}))
    else:
        checks.append(_check("mapping_preview", "pass", "启用的映射绑定均为已核验状态。",
                             detail={"binding_count": len(bindings)}))

    # 10–12: M6 probe 复用 QueryService 的 published 查询/脱敏路径，但不创建
    # citable M5 evidence。历史 evidence 只用于 integrity check，不能代替 live probe。
    probe_result: dict[str, Any] | None = None
    probe_target = None
    if not include_mcp_probe:
        checks.append(_check("mcp_query", "skipped", "调用方已关闭 MCP 只读探测。", blocking=False))
    elif snap is None or mcp_probe is None:
        checks.append(_check("mcp_query", "fail", "MCP 只读探测不可用。"))
    else:
        candidates = sorted(
            snap.objects,
            key=lambda name: not any(
                prop.sensitive
                for obj in snap.template_pack.objects if obj.object == name
                for prop in obj.properties
            ),
        )
        probe_target = candidates[0] if candidates else None
        try:
            if probe_target is None:
                raise ValueError("published snapshot contains no object")
            probe_result = mcp_probe(probe_target)
            meta = probe_result.get("meta", {})
            if (
                not isinstance(probe_result.get("rows"), list)
                or meta.get("dataset_version") != snap.dataset_version
                or meta.get("template_version") != snap.template_version
                or meta.get("source") != source
                or meta.get("query_id") is not None
            ):
                raise ValueError("probe result cannot be verified")
            checks.append(_check("mcp_query", "pass", "MCP 已对当前已发布数据集完成最小只读查询。",
                                 detail={"object": probe_target, "row_count": len(probe_result["rows"])}))
        except Exception:
            probe_result = None
            checks.append(_check("mcp_query", "fail", "MCP 最小只读查询失败。"))

    sensitive_names: set[str] = set()
    if snap is not None and probe_target is not None:
        probe_template = next(
            (obj for obj in snap.template_pack.objects if obj.object == probe_target), None,
        )
        sensitive_names = {
            prop.name for prop in (probe_template.properties if probe_template else [])
            if prop.sensitive
        }
    if probe_result is None:
        checks.append(_check("masking", "skipped", "没有成功的 MCP 只读探测，无法复核脱敏。", blocking=False))
    elif not sensitive_names:
        checks.append(_check("masking", "skipped", "探测对象没有敏感字段。", blocking=False))
    elif not probe_result["rows"]:
        checks.append(_check("masking", "warning", "探测对象没有样本行，无法验证敏感值脱敏。",
                             detail={"sensitive_field_count": len(sensitive_names)}))
    else:
        meta_masked = set(probe_result.get("meta", {}).get("masked_fields") or [])
        leaked = [
            name for name in sensitive_names
            if name not in meta_masked or any(
                row.get(name) not in (None, "***") for row in probe_result["rows"]
            )
        ]
        if leaked:
            checks.append(_check("masking", "fail", "MCP 只读探测发现敏感字段未按规则脱敏。",
                                 detail={"sensitive_field_count": len(leaked)}))
        else:
            checks.append(_check("masking", "pass", "MCP 探测返回的敏感字段均已脱敏。",
                                 detail={"sensitive_field_count": len(sensitive_names)}))

    evidence_rows = []
    if snap is not None:
        evidence_rows = db.con.execute(
            "SELECT * FROM d2a_gateway_query_evidence WHERE source = ? AND dataset_version = ? "
            "ORDER BY created_at DESC LIMIT 50", (source, snap.dataset_version),
        ).fetchall()

    invalid_evidence = 0
    for row in evidence_rows:
        if (not is_valid_digest(row["result_digest"])
                or _safe_json(row["normalized_query_json"]) is None
                or _safe_json(row["binding_hashes_json"]) is None
                or _safe_json(row["result_summary_json"]) is None):
            invalid_evidence += 1
    if not evidence_rows:
        checks.append(_check("evidence_integrity", "warning", "没有 MCP 查询 evidence；无法验证证据链。", blocking=False))
    elif invalid_evidence:
        checks.append(_check("evidence_integrity", "fail", "存在格式或摘要校验失败的查询证据。",
                             detail={"invalid_count": invalid_evidence}))
    else:
        checks.append(_check("evidence_integrity", "pass", "当前数据集的查询证据格式与摘要均可读。",
                             detail={"query_evidence_count": len(evidence_rows)}))

    if snap is None:
        checks.append(_check("cross_surface_consistency", "fail", "缺少已发布快照，无法建立跨界面版本一致性。"))
    elif pack.version != snap.template_version:
        checks.append(_check("cross_surface_consistency", "fail", "当前模板版本与已发布快照模板版本不一致。",
                             detail={"current_template_version": pack.version, "published_template_version": snap.template_version}))
    elif set(snap.objects) != {o.object for o in snap.template_pack.objects}:
        checks.append(_check("cross_surface_consistency", "fail", "已发布对象清单与冻结模板不一致。"))
    else:
        checks.append(_check("cross_surface_consistency", "pass", "Console 模板、已发布数据集和对象快照版本一致。",
                             detail={"dataset_version": snap.dataset_version, "template_version": snap.template_version}))

    seen_ids = []
    for c in checks:
        if c["check_id"] not in seen_ids:
            seen_ids.append(c["check_id"])
    assert tuple(seen_ids) == CHECK_ORDER
    overall = _overall(checks)
    finished_at = _now()
    summary = {
        "check_count": len(checks),
        "pass_count": sum(c["status"] == "pass" for c in checks),
        "warning_count": sum(c["status"] == "warning" for c in checks),
        "fail_count": sum(c["status"] == "fail" for c in checks),
        "skipped_count": sum(c["status"] == "skipped" for c in checks),
    }
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "source": source,
        "overall_status": overall,
        "started_at": started_at,
        "finished_at": finished_at,
        "deployment": {
            "config_loaded": config is not None,
            "source_configured": source_cfg is not None,
            "template_version": pack.version,
        },
        "dataset_version": snap.dataset_version if snap is not None else None,
        "template_version": snap.template_version if snap is not None else pack.version,
        "summary": summary,
        "checks": checks,
    }
