"""观测聚合内核(M3):时间标准化、新鲜度阈值、状态折叠、安全错误摘要、
七节点状态计算与总览聚合。

纪律(路线图 §3.2 / M3 计划 §4):
- 状态只由结构化事实计算:运行表、水位、audit 元数据、raw/obj `_d2a_*` 列、
  模板配置、显式健康探测;不解析日志文本、SQL 或 detail 关键词;
- `unknown` 是一等状态:证据缺失、查询失败、跨机器不可见、版本未实现时
  返回 unknown / null 并说明原因,不得替换为 healthy、0、当前时间或假版本;
- 动态表名只能来自已加载配置/模板,禁止接受用户输入拼 SQL。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from ..connect.config import ConnectConfig, parse_duration_seconds
from ..connect.landing import LandingStore
from ..metamodel.schema import TemplatePack

# 固定节点顺序(契约;前端按此渲染)
NODE_IDS = ("erp", "extract", "push", "raw", "mapping", "objects", "mcp")

# 总体状态折叠优先级(只用于摘要;节点原始状态不被覆盖)
_FOLD_ORDER = ("failed", "stale", "warning", "running", "unknown", "idle", "healthy")

_ERROR_LIMIT = 500


# ---- 时间 ----


def aware(text: Any) -> datetime | None:
    """legacy SQLite 本地无时区文本 → 带时区 datetime(按服务器本地时区解释)。

    已是 aware 字符串时转换到本地时区;解析失败返回 None,
    不把当前时间当作原时间。
    """
    if not text or not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.strip()).astimezone()
    except (ValueError, TypeError):
        return None


def now_aware() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime | None) -> datetime | None:
    return dt


# ---- 新鲜度阈值 ----


def freshness_threshold(sync_every: str | None) -> timedelta | None:
    """默认新鲜度阈值:max(2 × sync_every_seconds, 300 秒)。

    sync_every 缺失或无法解析时不猜阈值,返回 None(调用方判 unknown)。
    """
    if not sync_every:
        return None
    try:
        seconds = parse_duration_seconds(sync_every)
    except (ValueError, TypeError):
        return None
    if seconds <= 0:
        return None
    return timedelta(seconds=max(2 * seconds, 300))


def is_stale(at: datetime | None, threshold: timedelta, now: datetime) -> bool:
    """at 早于 (now - threshold) 为陈旧;at 缺失不判陈旧(由调用方判 idle/unknown)。"""
    if at is None:
        return False
    return (now - at) > threshold


# ---- 安全错误摘要 ----


def safe_error_summary(text: Any, limit: int = _ERROR_LIMIT) -> str | None:
    """错误摘要:去空白、限长;输入必须已是结构化错误(非完整 SQL/敏感行)。"""
    if not text or not isinstance(text, str):
        return None
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    return cleaned[:limit]


# ---- 状态折叠 ----


def fold_status(statuses: list[str]) -> str:
    """按 failed>stale>warning>running>unknown>idle>healthy 折叠。

    存在任何 unknown 时结果不得为 healthy(由顺序保证);空列表为 unknown。
    """
    present = set(statuses)
    for status in _FOLD_ORDER:
        if status in present:
            return status
    return "unknown"


# ---- 事实查询(全部只读;表名来自配置/模板)----
#
# 纪律:查询失败必须抛出(由节点级 try/except 降级为 unknown 并说明原因),
# 不得把 SQLite 异常解释为"从未运行 / 没有数据"(那是 idle,语义截然不同)。


def latest_run(db: LandingStore, run_type: str, source: str,
               status: str | None = None) -> sqlite3.Row | None:
    """最近一次指定类型/来源/状态的 run;按源过滤,跨源记录不得混用。"""
    sql = "SELECT * FROM d2a_sync_run WHERE run_type = ? AND source = ?"
    params: list[Any] = [run_type, source]
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT 1"
    return db.con.execute(sql, params).fetchone()


def run_facts(db: LandingStore, run_type: str, source: str) -> dict[str, Any]:
    """run 事实:最近一次 ok / 失败 / 运行中 / 最新 run 分别查询(按源过滤)。

    最近成功与最近失败是两个独立事实:最新一条失败不能吞掉更早的成功;
    正在运行的 run 不是失败;错峰暂停(paused)不是失败,不计入失败历史。
    """
    return {
        "ok": latest_run(db, run_type, source, status="ok"),
        "failed": db.con.execute(
            "SELECT * FROM d2a_sync_run WHERE run_type = ? AND source = ? "
            "AND status NOT IN ('ok', 'running', 'paused') ORDER BY id DESC LIMIT 1",
            (run_type, source)).fetchone(),
        "running": latest_run(db, run_type, source, status="running"),
        "latest": latest_run(db, run_type, source),
    }


def has_any_run(db: LandingStore, source: str) -> bool:
    (n,) = db.con.execute(
        "SELECT COUNT(*) FROM d2a_sync_run WHERE source = ?", (source,)).fetchone()
    return n > 0


def latest_ingest_batch(db: LandingStore, source: str) -> sqlite3.Row | None:
    """最近一次该源 HTTP ingest 批次(结构化 audit;不解析 sql 文本)。"""
    return db.con.execute(
        "SELECT ts, source, rows, batch_id FROM d2a_audit_log "
        "WHERE action = 'ingest' AND batch_id IS NOT NULL AND source = ? "
        "ORDER BY id DESC LIMIT 1", (source,)).fetchone()


def raw_stats(db: LandingStore, source: str,
              tables: list[str]) -> tuple[int | None, datetime | None]:
    """raw 活跃(未软删)行数合计 + 最新抽取时间;查询失败返回 (None, None)。

    tables 必须来自配置白名单/模板 binding(可信标识符)。
    """
    total = 0
    latest: datetime | None = None
    try:
        for table in tables:
            name = f"raw_{source}__{table}"
            row = db.con.execute(
                f'SELECT COUNT(*) AS n, MAX("_d2a_extracted_at") AS m FROM "{name}"'
                " WHERE _d2a_deleted_at IS NULL"
            ).fetchone()
            total += row["n"]
            at = aware(row["m"])
            if at is not None and (latest is None or at > latest):
                latest = at
        return total, latest
    except sqlite3.Error:
        return None, None


def object_stats(db: LandingStore, pack: TemplatePack) -> dict[str, dict[str, Any]]:
    """每个对象的行数 / 最新物化时间 / 待处理隔离。

    "表不存在"(no such table)= 尚未物化;其他查询异常记入 entry["error"],
    由调用方降级为 unknown —— 查询失败不得表现为"未物化"。
    """
    out: dict[str, dict[str, Any]] = {}
    for tpl in pack.objects:
        entry: dict[str, Any] = {"rows": None, "mapped_at": None, "quarantined": None}
        try:
            row = db.con.execute(
                f'SELECT COUNT(*) AS n, MAX("_d2a_mapped_at") AS m FROM "obj_{tpl.object}"'
            ).fetchone()
            entry["rows"] = row["n"]
            entry["mapped_at"] = aware(row["m"])
        except sqlite3.Error as e:
            if "no such table" not in str(e).lower():
                entry["error"] = f"obj_{tpl.object} 查询失败:{e}"
        try:
            (q,) = db.con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine "
                "WHERE object = ? AND resolved_at IS NULL",
                (tpl.object,)).fetchone()
            entry["quarantined"] = q
        except sqlite3.Error as e:
            entry["error"] = f"隔离计数查询失败:{e}"
        out[tpl.object] = entry
    return out


def quarantine_pending(db: LandingStore) -> int | None:
    """未处理隔离数;查询失败返回 None(调用方判 unknown,不得当 0)。"""
    try:
        (n,) = db.con.execute(
            "SELECT COUNT(*) FROM d2a_quarantine WHERE resolved_at IS NULL").fetchone()
        return n
    except sqlite3.Error:
        return None


def binding_summary(pack: TemplatePack) -> dict[str, int]:
    summary = {"verified": 0, "draft": 0, "disabled": 0}
    for tpl in pack.objects:
        for b in tpl.bindings:
            summary[b.status] = summary.get(b.status, 0) + 1
    return summary


def raw_table_names(db: LandingStore, source: str) -> list[str]:
    """该源在落地库中已存在的 raw 业务表名(去掉 raw_<source>__ 前缀)。

    查询失败抛出异常(调用方判 unknown),不返回空列表冒充"没有数据"。
    """
    prefix = f"raw_{source}__%"
    return [r[0][len(f"raw_{source}__"):] for r in db.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
        (prefix,))]


# ---- 节点状态计算(纯函数,输入事实)----

Probe = Callable[[], tuple[bool, str]]


def _run_status(facts: dict[str, Any], threshold: timedelta | None, now: datetime,
                *, never: str = "从未运行",
                require_threshold: bool = True) -> tuple[str, str]:
    """run 类节点的通用状态规则(erp/extract/mapping 共享):

    有效运行中(最新一条)> 最近失败 > 窗口暂停(warning)>
    阈值内成功(healthy)/超阈值(stale) > 从未运行(idle) > 不可判定(unknown)。

    - 崩溃遗留的旧 running(id 小于更新的 run)是孤儿,不得覆盖更新的结果;
    - require_threshold=True 时(erp/extract/raw 的数据新鲜度判断)缺少阈值
      不猜:成功只能判 unknown;mapping 的 apply run 不受此约束(其新旧由
      objects 节点的时间差表达),成功可判 healthy;
    - paused 是合法窗口暂停(已落批次幂等,下窗口续跑),映射 warning。
    """
    running, latest = facts["running"], facts["latest"]
    if running is not None and latest is not None and running["id"] == latest["id"]:
        return "running", f"运行中(run #{running['id']},开始于 {running['started_at']})"
    if latest is None:
        return "idle", never
    status = latest["status"]
    finished = aware(latest["finished_at"]) or aware(latest["started_at"])
    if status == "paused":
        return "warning", "错峰窗口暂停(已落批次幂等,下窗口续跑)"
    if status != "ok":
        return "failed", safe_error_summary(latest["detail"]) or f"最近运行失败({status})"
    if finished is None:
        return "unknown", "最近运行时间不可判定(legacy 时间解析失败)"
    if require_threshold and threshold is None:
        return "unknown", "缺少同步节奏配置(sync_every),无法判定数据新鲜度"
    if threshold is not None and is_stale(finished, threshold, now):
        return "stale", f"最近一次成功在 {finished.isoformat()},已超出新鲜度阈值"
    return "healthy", "最近一次运行成功"


def _run_fields(facts: dict[str, Any], source: str, now: datetime) -> dict[str, Any]:
    """run 节点的展示字段:最近成功/失败时间(独立事实)、耗时、行数、run_id。"""
    out: dict[str, Any] = {"source": source}
    ok, failed = facts["ok"], facts["failed"]
    if ok is not None:
        out["last_success_at"] = aware(ok["finished_at"]) or aware(ok["started_at"])
    if failed is not None:
        out["last_failure_at"] = aware(failed["finished_at"]) or aware(failed["started_at"])
    # 展示用 run:有效 running(最新一条)优先;孤儿 running(崩溃遗留)不展示,
    # 避免把数年前的假运行中和夸大耗时显示出来
    running, latest = facts["running"], facts["latest"]
    shown = (running if running is not None and latest is not None
             and running["id"] == latest["id"] else None) or latest
    if shown is None:
        return out
    out["run_id"] = str(shown["id"])
    out["rows_in"] = shown["rows"]
    out["rows_out"] = shown["rows"]
    started = aware(shown["started_at"])
    finished = aware(shown["finished_at"])
    if started is not None and finished is not None:
        out["duration_ms"] = max(0.0, (finished - started).total_seconds() * 1000)
    elif started is not None and shown["status"] == "running":
        out["duration_ms"] = max(0.0, (now - started).total_seconds() * 1000)
    out["observed_at"] = finished or started
    return out


def _node(node: str, status: str, reason: str = "", **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "node": node,
        "status": status,
        "status_reason": reason,
        "observed_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "rows_in": None,
        "rows_out": None,
        "duration_ms": None,
        "error": None,
        "version": None,
        "run_id": None,
        "source": None,
        "detail_path": None,
    }
    out.update(kw)
    return out


def compute_nodes(
    db: LandingStore,
    pack: TemplatePack,
    cfg: ConnectConfig | None,
    source: str,
    *,
    probes: dict[str, Probe] | None = None,
    now: datetime | None = None,
    component_version: str | None = None,
) -> list[dict[str, Any]]:
    """按固定顺序计算 7 个节点状态(详见 M3 计划 §6 口径矩阵)。

    每个节点的事实查询独立 try/except:SQLite 异常降级为该节点 unknown
    ("查询失败"),绝不解释为 idle("从未运行/没有数据")。
    """
    now = now or now_aware()
    probes = probes or {}
    scfg = cfg.sources.get(source) if cfg else None
    threshold = freshness_threshold(scfg.sync_every if scfg else None)
    is_http_sink = bool(scfg and scfg.sink.type == "http")

    nodes: list[dict[str, Any]] = []

    # -- erp / extract:证据同为 sync run;推送模式下平台看不到中间机运行 --
    sync_facts: dict[str, Any] | None = None
    any_run: bool | None = None
    sync_error: sqlite3.Error | None = None
    try:
        sync_facts = run_facts(db, "sync", source)
        any_run = has_any_run(db, source)
    except sqlite3.Error as e:
        sync_error = e

    for node_id, never_text in (("erp", "从未连接抽取"), ("extract", "从未运行同步")):
        if sync_error is not None:
            nodes.append(_node(node_id, "unknown",
                               f"运行记录查询失败:{sync_error}", source=source))
            continue
        assert sync_facts is not None and any_run is not None
        if is_http_sink and sync_facts["latest"] is None:
            nodes.append(_node(node_id, "unknown",
                               "推送模式:ERP/抽取运行在中间机,平台侧无跨机器证据",
                               source=source))
            continue
        if sync_facts["latest"] is None and any_run:
            nodes.append(_node(node_id, "unknown",
                               "存在历史运行但未记录类型(老库),无法判定同步状态",
                               source=source))
            continue
        status, reason = _run_status(sync_facts, threshold, now, never=never_text)
        err_run = sync_facts["failed"]
        nodes.append(_node(node_id, status, reason,
                           error=safe_error_summary(err_run["detail"] if err_run else None),
                           **_run_fields(sync_facts, source, now)))

    # -- push:local 直写为 idle;http 看 ingest 批次与探测 --
    if not is_http_sink:
        nodes.append(_node("push", "idle", "本地直写(local sink),不经过 HTTP 推送",
                           source=source))
    else:
        ingest_ok = None
        if "ingest" in probes:
            try:
                ingest_ok, _method = probes["ingest"]()
            except Exception:
                ingest_ok = None
        batch = None
        batch_error: sqlite3.Error | None = None
        try:
            batch = latest_ingest_batch(db, source)
        except sqlite3.Error as e:
            batch_error = e
        if batch_error is not None:
            nodes.append(_node("push", "unknown", f"ingest 批次查询失败:{batch_error}",
                               source=source))
        elif batch is None and ingest_ok is None:
            nodes.append(_node("push", "unknown",
                               "无 ingest 批次记录且接收端不可探测", source=source))
        elif batch is None:
            nodes.append(_node(
                "push",
                "failed" if ingest_ok is False else "idle",
                "ingest 接收端不可达" if ingest_ok is False else "尚无推送批次",
                source=source))
        else:
            at = aware(batch["ts"])
            if ingest_ok is False:
                nodes.append(_node("push", "failed", "ingest 接收端当前不可达",
                                   observed_at=at, rows_in=batch["rows"],
                                   source=source))
            elif at is None:
                nodes.append(_node("push", "unknown", "最近批次时间不可判定",
                                   rows_in=batch["rows"], source=source))
            elif threshold is None:
                nodes.append(_node("push", "unknown",
                                   "缺少同步节奏配置(sync_every),无法判定推送新鲜度",
                                   observed_at=at, rows_in=batch["rows"], source=source))
            elif is_stale(at, threshold, now):
                nodes.append(_node("push", "stale",
                                   f"最近批次在 {at.isoformat()},已超出新鲜度阈值",
                                   observed_at=at, rows_in=batch["rows"], source=source))
            else:
                nodes.append(_node("push", "healthy", "最近推送批次已落地",
                                   observed_at=at, rows_in=batch["rows"], source=source))

    # -- raw:活跃行 + 最新抽取时间 --
    raw_error: sqlite3.Error | None = None
    tables: list[str] = []
    raw_rows: int | None = None
    raw_latest: datetime | None = None
    try:
        tables = raw_table_names(db, source)
    except sqlite3.Error as e:
        raw_error = e
    if raw_error is None:
        raw_rows, raw_latest = raw_stats(db, source, tables)
    if raw_error is not None:
        nodes.append(_node("raw", "unknown", f"raw 元数据查询失败:{raw_error}",
                           source=source))
    elif raw_rows is None:
        nodes.append(_node("raw", "unknown", "raw 行数查询失败", source=source))
    elif not tables and not any_run:
        nodes.append(_node("raw", "idle", "无数据且从未同步", source=source))
    elif not tables:
        nodes.append(_node("raw", "idle", "尚无 raw 数据", source=source))
    elif raw_latest is None:
        nodes.append(_node("raw", "unknown", "raw 最新抽取时间缺失",
                           rows_in=raw_rows, rows_out=raw_rows, source=source))
    elif threshold is None:
        nodes.append(_node("raw", "unknown",
                           "缺少同步节奏配置(sync_every),无法判定数据新鲜度",
                           observed_at=raw_latest, rows_in=raw_rows, rows_out=raw_rows,
                           source=source))
    elif is_stale(raw_latest, threshold, now):
        nodes.append(_node("raw", "stale",
                           f"最新抽取在 {raw_latest.isoformat()},已超出新鲜度阈值",
                           observed_at=raw_latest, rows_in=raw_rows, rows_out=raw_rows,
                           source=source))
    else:
        nodes.append(_node("raw", "healthy", "raw 数据在新鲜度阈值内",
                           observed_at=raw_latest, rows_in=raw_rows, rows_out=raw_rows,
                           source=source))

    # -- mapping:apply run + 隔离 + binding 状态 --
    apply_facts: dict[str, Any] | None = None
    map_error: sqlite3.Error | None = None
    try:
        apply_facts = run_facts(db, "apply", source)
    except sqlite3.Error as e:
        map_error = e
    qp = quarantine_pending(db)
    bs = binding_summary(pack)
    if map_error is not None:
        nodes.append(_node("mapping", "unknown",
                           f"apply 运行记录查询失败:{map_error}", source=source))
    elif apply_facts is not None and apply_facts["latest"] is None and any_run:
        nodes.append(_node("mapping", "unknown",
                           "存在历史运行但未记录类型(老库),无法判定 apply 状态",
                           source=source))
    elif qp is None:
        nodes.append(_node("mapping", "unknown", "隔离计数查询失败", source=source))
    else:
        map_status, map_reason = _run_status(apply_facts, None, now,
                                             never="从未执行 apply",
                                             require_threshold=False)
        if map_status == "healthy" and (bs["draft"] > 0 or qp > 0):
            reasons = []
            if bs["draft"] > 0:
                reasons.append(f"{bs['draft']} 个 binding 仍为 draft(未经现场校准)")
            if qp > 0:
                reasons.append(f"{qp} 行待处理隔离")
            map_status, map_reason = "warning", ";".join(reasons)
        map_err_run = apply_facts["failed"] if apply_facts else None
        nodes.append(_node("mapping", map_status, map_reason,
                           version=pack.version,
                           error=safe_error_summary(
                               map_err_run["detail"] if map_err_run else None),
                           **_run_fields(apply_facts or {"ok": None, "failed": None,
                                                         "running": None, "latest": None},
                                         source, now)))

    # -- objects:物化时间 vs raw 最新时间;apply 失败但旧表保留 = stale --
    # 传播规则:上游节点(raw / mapping)领域状态为 unknown 时,对象层无法证明
    # 自己与 raw 同步,必须同为 unknown —— 不只是查询异常这一种情形。
    raw_node_status = nodes[3]["status"]      # raw 是第 4 个节点
    map_node_status = nodes[4]["status"]      # mapping 是第 5 个节点
    objs = object_stats(db, pack)
    obj_errors = [v["error"] for v in objs.values() if v.get("error")]
    materialized = {k: v for k, v in objs.items() if v["rows"] is not None}

    # apply 对对象层的影响按真实状态区分:running/paused 是进行中,不是失败
    apply_status = (apply_facts["latest"]["status"]
                    if apply_facts and apply_facts["latest"] else None)
    apply_impact: str | None = None
    if apply_status == "running":
        apply_impact = "apply 运行中,对象层为进行中/部分更新结果"
    elif apply_status == "paused":
        apply_impact = "apply 窗口暂停,对象层为部分更新结果"
    elif apply_status not in (None, "ok"):
        apply_impact = "apply 失败,对象层继续使用上一稳定结果"

    if obj_errors:
        nodes.append(_node("objects", "unknown", obj_errors[0], source=source))
    elif map_node_status == "unknown":
        nodes.append(_node("objects", "unknown",
                           "apply 状态不可检测,无法验证对象层状态", source=source))
    elif materialized and raw_node_status == "unknown":
        nodes.append(_node("objects", "unknown",
                           "raw 状态不可检测,无法验证对象层与 raw 同步", source=source))
    elif not materialized:
        if apply_impact is not None and apply_impact.startswith("apply 失败"):
            nodes.append(_node("objects", "failed",
                               "apply 失败且对象表未物化", source=source))
        else:
            nodes.append(_node("objects", "idle", "尚未物化", source=source))
    else:
        mapped_latest = max((v["mapped_at"] for v in materialized.values()
                             if v["mapped_at"] is not None), default=None)
        raw_newer = (raw_latest is not None and mapped_latest is not None
                     and raw_latest > mapped_latest)
        if mapped_latest is None:
            nodes.append(_node("objects", "unknown", "对象物化时间不可判定",
                               source=source))
        elif apply_impact is not None or raw_newer:
            reason = apply_impact or "raw 已更新,对象层尚未重新物化(旧结果)"
            nodes.append(_node("objects", "stale", reason,
                               observed_at=mapped_latest, source=source))
        else:
            rows_total = sum(v["rows"] for v in materialized.values())
            nodes.append(_node("objects", "healthy", "对象层与 raw 同步",
                               observed_at=mapped_latest, rows_out=rows_total,
                               source=source))

    # -- mcp:服务探测 + 对象层状态(服务健康 ≠ 数据健康)--
    mcp_ok: bool | None = None
    mcp_method = ""
    if "mcp" in probes:
        try:
            mcp_ok, mcp_method = probes["mcp"]()
        except Exception:
            mcp_ok = None
    objects_status = nodes[-1]["status"]
    if mcp_ok is None:
        nodes.append(_node("mcp", "unknown", "MCP 未配置或探测超时", source=source))
    elif mcp_ok is False:
        nodes.append(_node("mcp", "failed", "MCP 服务不可达", source=source))
    elif objects_status == "failed":
        nodes.append(_node("mcp", "failed",
                           "对象层构建失败,MCP 无法服务正常数据", source=source))
    elif objects_status == "stale":
        nodes.append(_node("mcp", "stale",
                           "MCP 服务正常,但对象层为旧结果", source=source))
    elif objects_status == "unknown":
        nodes.append(_node("mcp", "unknown",
                           "对象层状态不可检测,无法判定 MCP 数据健康", source=source))
    elif objects_status == "idle":
        nodes.append(_node("mcp", "idle", "MCP 服务正常,对象层未就绪", source=source))
    else:
        nodes.append(_node("mcp", "healthy",
                           "MCP 服务正常(探测:" + (mcp_method or "http") + ")",
                           observed_at=now, source=source,
                           version=component_version))

    return nodes


def build_pipeline(db: LandingStore, pack: TemplatePack, cfg: ConnectConfig | None,
                   source: str, *, probes: dict[str, Probe] | None = None,
                   now: datetime | None = None,
                   component_version: str | None = None) -> dict[str, Any]:
    """PipelineResponse 载荷:固定 7 节点 + 折叠总体状态。"""
    now = now or now_aware()
    nodes = compute_nodes(db, pack, cfg, source, probes=probes, now=now,
                          component_version=component_version)
    assert [n["node"] for n in nodes] == list(NODE_IDS)
    return {
        "generated_at": now,
        "overall_status": fold_status([n["status"] for n in nodes]),
        "nodes": nodes,
    }


# ---- 总览聚合(Overview;T04)----


def recent_runs(db: LandingStore, limit: int = 5) -> list[dict[str, Any]] | None:
    """最近运行(带 run_type;历史 NULL 不回填);时间转带时区。

    查询失败返回 None(不可检测),不返回空列表冒充"从未运行"。
    """
    try:
        rows = db.con.execute(
            "SELECT * FROM d2a_sync_run ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 50)),)).fetchall()
    except sqlite3.Error:
        return None
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "run_type": r["run_type"] if "run_type" in r.keys() else None,
            "source": r["source"],
            "status": r["status"],
            "rows": r["rows"],
            "tables": r["tables"],
            "started_at": aware(r["started_at"]),
            "finished_at": aware(r["finished_at"]),
        })
    return out


def sync_trend(db: LandingStore, *, now: datetime | None = None,
               hours: int = 24, max_points: int = 24) -> list[dict[str, Any]] | None:
    """最近 N 小时 sync 抽取趋势(按小时桶聚合;上限 max_points 点)。

    查询失败返回 None(不可检测),不返回空列表冒充"没有趋势"。
    """
    now = now or now_aware()
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        rows = db.con.execute(
            "SELECT substr(started_at, 1, 13) AS h, COUNT(*) AS runs,"
            " COALESCE(SUM(rows), 0) AS rows "
            "FROM d2a_sync_run WHERE run_type = 'sync' AND started_at >= ? "
            "GROUP BY h ORDER BY h DESC LIMIT ?",
            (cutoff, max_points)).fetchall()
    except sqlite3.Error:
        return None
    points = []
    for r in rows:
        bucket = aware(f"{r['h']}:00:00")
        if bucket is not None:
            points.append({"bucket": bucket, "rows": r["rows"], "runs": r["runs"]})
    points.sort(key=lambda p: p["bucket"])
    return points


_NODE_SEVERITY = {"failed": "critical", "stale": "warning"}


def build_alerts(nodes: list[dict[str, Any]], *, quarantine: int | None,
                 drafts: int, query_failures: list[str] | None = None,
                 now: datetime | None = None) -> list[dict[str, Any]]:
    """当前告警:由节点/隔离/治理/查询失败确定性聚合(非持久化实体)。

    ID 由 kind + 目标组成,刷新后稳定;后端查询异常形成不可检测告警,
    不能让告警区空白。
    """
    now = now or now_aware()
    alerts: list[dict[str, Any]] = []
    for n in nodes:
        severity = _NODE_SEVERITY.get(n["status"])
        if severity is None:
            continue
        alerts.append({
            "id": f"node-{n['node']}",
            "severity": severity,
            "title": f"管道节点 {n['node']} {n['status']}",
            "reason": n.get("status_reason") or "无详细原因",
            "source": n.get("source"),
            "observed_at": n.get("observed_at") or now,
            "detail_path": None,
        })
    if (quarantine or 0) > 0:
        alerts.append({
            "id": "quarantine-pending",
            "severity": "warning",
            "title": "存在未处理隔离",
            "reason": f"{quarantine} 行数据因映射失败等待处理",
            "source": None,
            "observed_at": now,
            "detail_path": None,
        })
    if drafts > 0:
        alerts.append({
            "id": "binding-draft",
            "severity": "info",
            "title": "binding 未经现场校准",
            "reason": f"{drafts} 个 binding 仍为 draft,口径以现场数据字典核对为准",
            "source": None,
            "observed_at": now,
            "detail_path": None,
        })
    for i, failure in enumerate(query_failures or []):
        alerts.append({
            "id": f"query-failure-{i}",
            "severity": "critical",
            "title": "观测数据查询失败",
            "reason": failure,
            "source": None,
            "observed_at": now,
            "detail_path": None,
        })
    rank = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: rank[a["severity"]])
    return alerts
