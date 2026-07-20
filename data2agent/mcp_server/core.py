"""只读查询服务 + "说"档建议卡,传输无关。

E4 起网关消费完整管道的产物:源系统 → sync(raw_*)→ apply(obj_*)→ 本服务。
枚举值 / 编码翻译在映射应用阶段已完成,网关只做属性校验、脱敏与口径警示。

治理档位(看/说/做,docs/design/03 §3):
- 看:query_objects / query_metrics,只读;
- 说:propose_action 生成结构化建议卡 —— 不落任何写操作,卡内每条依据
  必须引用本会话某次查询的 meta.query_id(数字可溯源);
- 做:审批后的写回,不在开源网关范围;max_tier 为部署级档位上限。

安全边界(lite):
- 落地库以只读模式(mode=ro)打开;
- SQL 只引用模板声明的属性列与 obj_ 表,天然白名单;
- sensitive 属性一律脱敏为 "***",当前不提供解敏开关(解敏属"做"档治理,后续按权限模型提供)。
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from ..metamodel.loader import load_pack
from ..metamodel.schema import ObjectTemplate
from .metrics_impl import registry

MASK = "***"
TIER_ORDER = {"看": 0, "说": 1, "做": 2}
_QUERY_LOG_CAP = 500


class QueryService:
    def __init__(self, db_path: str | Path, templates_root: str | Path = "templates",
                 source: str = "digiwin_e10", max_tier: str = "说",
                 audit_sink=None):
        """audit_sink:可选的持久审计回调(dict → None),每次工具调用一条;
        与抽取侧 d2a_audit_log 对称,HTTP 部署默认写 JSONL(见 __main__)。"""
        self.db_path = str(db_path)
        self.source = source
        self.pack = load_pack(templates_root)
        self.metrics = registry(source)
        if max_tier not in TIER_ORDER:
            raise ValueError(f"max_tier 须为 {sorted(TIER_ORDER)},got '{max_tier}'")
        self.max_tier = max_tier
        self.audit_sink = audit_sink
        self._query_log: OrderedDict[str, dict] = OrderedDict()
        self._query_seq = 0
        self._proposal_seq = 0
        self._lock = threading.Lock()

    def _audit(self, record: dict) -> None:
        if self.audit_sink:
            self.audit_sink(record)

    def _log_query(self, tool: str, target: str, detail: str, warnings: list[str]) -> str:
        with self._lock:
            self._query_seq += 1
            qid = f"q{self._query_seq}"
            entry = {
                "query_id": qid, "tool": tool, "target": target, "detail": detail,
                "at": datetime.now().isoformat(timespec="seconds"),
                "warnings": [w for w in warnings if w],
            }
            self._query_log[qid] = entry
            while len(self._query_log) > _QUERY_LOG_CAP:
                self._query_log.popitem(last=False)
            self._audit({k: entry[k] for k in ("query_id", "tool", "target", "detail", "at")})
            return qid

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")  # WAL 检查点期间读锁竞争时等待而非报错
        return con

    # ---- query_objects ----

    def query_objects(self, object: str | None = None, filters: dict | None = None,
                      order_by: str | None = None, desc: bool = False, limit: int = 20) -> dict:
        if object is None:
            return self._object_catalog()

        tpl = next((o for o in self.pack.objects if o.object == object), None)
        if tpl is None:
            raise ValueError(f"未知对象 '{object}',可用:{sorted(self.pack.object_names())}")
        binding = next((b for b in tpl.bindings
                        if b.source == self.source and b.enabled), None)
        if binding is None:
            raise ValueError(f"{object} 没有 source={self.source} 的可用 binding")

        sql, params = self._object_sql(tpl, filters, order_by, desc, limit)
        con = self._connect()
        try:
            try:
                rows = [dict(r) for r in con.execute(sql, params)]
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    raise ValueError(
                        f"对象层尚未物化({object}):请先运行 "
                        "python -m data2agent.connect sync 与 apply") from e
                raise
            quarantined = self._quarantine_count(con, object)
        finally:
            con.close()

        sensitive = {p.name for p in tpl.properties if p.sensitive}
        for row in rows:
            for prop in sensitive:
                if row.get(prop) is not None:
                    row[prop] = MASK
        note = ("binding 为 draft:字段映射按参考表形构造,口径未经现场校准"
                if binding.status == "draft" else "")
        qid = self._log_query("query_objects", object,
                              f"filters={filters or {}} rows={len(rows)}", [note])
        return {
            "object": object,
            "display_name": tpl.display_name,
            "rows": rows,
            "meta": {
                "query_id": qid,
                "source": binding.source,
                "binding_status": binding.status,
                "masked_fields": sorted(sensitive),
                "row_count": len(rows),
                "quarantined": quarantined,
                "note": note,
            },
        }

    def _object_sql(self, tpl: ObjectTemplate, filters: dict | None,
                    order_by: str | None, desc: bool, limit: int) -> tuple[str, list]:
        props = {p.name: p for p in tpl.properties}
        cols = ", ".join('"{}"'.format(n) for n in props)
        where, params = [], []
        for name, val in (filters or {}).items():
            prop = props.get(name)
            if prop is None:
                raise ValueError(f"未知筛选字段 '{name}',可用:{sorted(props)}")
            if prop.type == "enum" and val not in prop.enum_values:
                raise ValueError(f"'{name}' 取值须为 {sorted(prop.enum_values)},got '{val}'")
            where.append(f'"{name}" = ?')
            params.append(val)
        order = ""
        if order_by:
            if order_by not in props:
                raise ValueError(f"未知排序字段 '{order_by}',可用:{sorted(props)}")
            order = f' ORDER BY "{order_by}" {"DESC" if desc else "ASC"}'
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit), 200))
        return (f'SELECT {cols} FROM "obj_{tpl.object}"{where_sql}{order} LIMIT {limit}',
                params)

    def _quarantine_count(self, con: sqlite3.Connection, object_name: str) -> int:
        try:
            (n,) = con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine "
                "WHERE source = ? AND object = ? AND resolved_at IS NULL",
                (self.source, object_name)).fetchone()
            return n
        except sqlite3.OperationalError:
            return 0

    def _object_catalog(self) -> dict:
        return {
            "objects": [
                {
                    "object": o.object,
                    "display_name": o.display_name,
                    "domain": o.domain,
                    "description": o.description,
                    "keys": o.keys,
                    "properties": [
                        {"name": p.name, "type": p.type, "desc": p.desc,
                         **({"sensitive": True} if p.sensitive else {})}
                        for p in o.properties
                    ],
                    "states": o.states,
                    "sources": [b.source for b in o.bindings],
                }
                for o in self.pack.objects
            ],
            "meta": {"active_source": self.source,
                     "usage": "带 object 参数查询数据;filters 为 属性→值 等值筛选"},
        }

    # ---- propose_action("说"档)----

    def propose_action(self, object: str, action: str, conclusion: str,
                       evidence: list[dict]) -> dict:
        """生成结构化建议卡。不落任何写操作;每条依据必须引用 meta.query_id。"""
        tpl = next((o for o in self.pack.objects if o.object == object), None)
        if tpl is None:
            raise ValueError(f"未知对象 '{object}',可用:{sorted(self.pack.object_names())}")
        act = next((a for a in tpl.actions if a.name == action), None)
        if act is None:
            available = [f"{a.name}(档位:{a.tier})" for a in tpl.actions]
            raise ValueError(f"{object} 未声明动作 '{action}',可用:{available or '(无)'}")
        if TIER_ORDER[act.tier] > TIER_ORDER[self.max_tier]:
            raise ValueError(
                f"动作 '{action}' 档位为「{act.tier}」,超出本网关档位上限「{self.max_tier}」;"
                "「做」档需审批流治理,不在开源网关范围")
        if not conclusion or not conclusion.strip():
            raise ValueError("conclusion 不能为空:建议卡必须有明确结论")
        if not evidence:
            raise ValueError(
                "evidence 不能为空:先调用 query_objects / query_metrics 取数,"
                "再以 [{claim, query_id}] 引用其 meta.query_id")

        with self._lock:
            cited, caveats = [], []
            for i, item in enumerate(evidence):
                claim, qid = item.get("claim", ""), item.get("query_id", "")
                if not claim or not qid:
                    raise ValueError(f"evidence[{i}] 须同时含 claim 与 query_id")
                logged = self._query_log.get(qid)
                if logged is None:
                    raise ValueError(
                        f"evidence[{i}] 的 query_id '{qid}' 无法溯源:不是本会话的查询;"
                        "请先实际调用 query_* 工具,引用其返回的 meta.query_id")
                cited.append({"claim": claim,
                              "query": {k: logged[k]
                                        for k in ("query_id", "tool", "target", "at")}})
                caveats.extend(logged["warnings"])

            self._proposal_seq += 1
            proposal_id = f"p{self._proposal_seq}"
            self._audit({
                "tool": "propose_action", "proposal_id": proposal_id,
                "target": f"{object}.{action}", "detail": f"evidence={len(cited)}",
                "at": datetime.now().isoformat(timespec="seconds"),
            })
            return {
                "proposal_id": proposal_id,
                "at": datetime.now().isoformat(timespec="seconds"),
                "object": object,
                "action": act.name,
                "action_desc": act.desc,
                "tier": act.tier,
                "conclusion": conclusion.strip(),
                "evidence": cited,
                "caveats": sorted({c for c in caveats if c}),
                "governance": "「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理",
            }

    # ---- query_metrics ----

    def query_metrics(self, metric: str | None = None, group_by: str | None = None,
                      limit: int = 24) -> dict:
        if metric is None:
            return self._metric_catalog()

        mdef = next((m for m in self.pack.metrics if m.metric == metric), None)
        if mdef is None:
            raise ValueError(f"未知指标 '{metric}',可用:{[m.metric for m in self.pack.metrics]}")
        definition = {
            "metric": mdef.metric, "display_name": mdef.display_name, "status": mdef.status,
            "formula": mdef.formula, "grain": mdef.grain, "caveats": mdef.caveats,
            "freshness_sla": mdef.freshness_sla,
        }
        impl = self.metrics.get(metric)
        if impl is None:
            return {
                **definition, "implemented": False,
                "reason": "依赖应收 / 回款对象,不在首批对象内;口径定义保留,待对象补齐后实现",
            }

        dim = group_by or impl.default_dim
        if dim not in impl.dims:
            raise ValueError(f"'{metric}' 支持的 group_by:{sorted(impl.dims)},got '{dim}'")
        order = '"group" DESC' if dim == "月" else "value DESC"
        limit = max(1, min(int(limit), 200))
        con = self._connect()
        try:
            rows = [dict(r) for r in
                    con.execute(impl.sql.format(dim=impl.dims[dim], order=order), (limit,))]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                raise ValueError(
                    "对象层尚未物化:请先运行 python -m data2agent.connect sync 与 apply") from e
            raise
        finally:
            con.close()
        warning = "口径为 draft(未经校准),数值仅供演示环境参考" if mdef.status != "certified" else ""
        qid = self._log_query("query_metrics", metric,
                              f"group_by={dim} rows={len(rows)}", [warning, mdef.caveats])
        return {
            **definition, "implemented": True, "unit": impl.unit,
            "group_by": dim, "rows": rows,
            "meta": {"query_id": qid, "row_count": len(rows), "warning": warning},
        }

    def _metric_catalog(self) -> dict:
        return {
            "metrics": [
                {
                    "metric": m.metric, "display_name": m.display_name, "status": m.status,
                    "formula": m.formula, "grain": m.grain, "caveats": m.caveats,
                    "implemented": self.metrics.get(m.metric) is not None,
                    **({"group_by_options": sorted(self.metrics[m.metric].dims)}
                       if self.metrics.get(m.metric) else {}),
                }
                for m in self.pack.metrics
            ],
            "meta": {"usage": "带 metric 参数取数;group_by 见各指标 group_by_options"},
        }
