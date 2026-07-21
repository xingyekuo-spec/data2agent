"""只读查询服务 + "说"档建议卡,传输无关。

E4 起网关消费完整管道的产物:源系统 → sync(raw_*)→ apply(objv_*)→ 本服务。
枚举值 / 编码翻译在映射应用阶段已完成,网关只做属性校验、脱敏与口径警示。

治理档位(看/说/做,docs/design/03 §3):
- 看:query_objects / query_metrics,只读;
- 说:propose_action 生成结构化建议卡 —— 不落任何写操作,卡内每条依据
  必须引用本会话某次查询的 meta.query_id(数字可溯源);
- 做:审批后的写回,不在开源网关范围;max_tier 为部署级档位上限。

安全边界(lite):
- 落地库以只读模式(mode=ro)打开;
- 对象/指标数据读取在同一 SQLite 读事务内 resolve_published_snapshot,
  再查询该快照的物理表;无 published 不回退遗留 obj_*;
- sensitive 属性一律脱敏为 "***",当前不提供解敏开关(解敏属"做"档治理,后续按权限模型提供)。
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..connect.dataset_publish import (
    PublishedDatasetSnapshot,
    PublishedSnapshotError,
    published_read_tx,
    resolve_published_snapshot,
)
from ..connect.landing import LandingStore
from ..metamodel.dataset_publish_contract import validate_build_table
from ..metamodel.loader import load_pack
from ..metamodel.schema import ObjectTemplate
from .metrics_impl import registry

MASK = "***"
TIER_ORDER = {"看": 0, "说": 1, "做": 2}
_QUERY_LOG_CAP = 500


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _safe_execute(con: sqlite3.Connection, sql: str, params: object = ()) -> list:
    """Execute published-data SQL; never surface SQLite messages to clients."""
    try:
        return list(con.execute(sql, params))
    except sqlite3.Error as e:
        raise ValueError("execution_failed: 数据集查询失败") from e


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
        # 每实例独立 epoch,避免配置重载后 q1/q2 重号导致旧 evidence 错绑新查询
        self._qid_epoch = secrets.token_hex(4)
        self._query_seq = 0
        self._proposal_seq = 0
        self._lock = threading.Lock()

    def _audit(self, record: dict) -> None:
        if self.audit_sink:
            self.audit_sink(record)

    def _log_query(
        self, tool: str, target: str, detail: str, warnings: list[str],
        *, dataset_version: str | None = None,
    ) -> str:
        with self._lock:
            self._query_seq += 1
            qid = f"q{self._qid_epoch}-{self._query_seq}"
            entry = {
                "query_id": qid, "tool": tool, "target": target, "detail": detail,
                "at": datetime.now().isoformat(timespec="seconds"),
                "warnings": [w for w in warnings if w],
                "dataset_version": dataset_version,
            }
            self._query_log[qid] = entry
            while len(self._query_log) > _QUERY_LOG_CAP:
                self._query_log.popitem(last=False)
            self._audit({
                k: entry[k]
                for k in ("query_id", "tool", "target", "detail", "at", "dataset_version")
            })
            return qid

    @contextmanager
    def _published_tx(self) -> Iterator[tuple[LandingStore, PublishedDatasetSnapshot]]:
        """同一只读事务内解析并持有 published 快照。"""
        store = LandingStore.open_readonly(self.db_path)
        try:
            with published_read_tx(store):
                try:
                    snap = resolve_published_snapshot(store, self.source)
                except PublishedSnapshotError as e:
                    raise ValueError(f"{e.reason_code}: {e.detail}") from None
                yield store, snap
        finally:
            store.con.close()

    def _public_meta(
        self, *, tool: str, target: str, query_id: str | None,
        row_count: int | None, duration_ms: int,
        masked_fields: list[str] | None = None,
        warnings: list[str] | None = None,
        **extra: object,
    ) -> dict:
        """Console/MCP Lab 公共 meta;保留额外兼容字段(source/binding_status 等)。"""
        meta = {
            "query_id": query_id,
            "tool": tool,
            "target": target,
            "row_count": row_count,
            "duration_ms": max(0, int(duration_ms)),
            "masked_fields": list(masked_fields or []),
            "warnings": [w for w in (warnings or []) if w],
            "evidence_scope": "process",
        }
        meta.update(extra)
        return meta

    # ---- query_objects ----

    @staticmethod
    def _require_scalar_filter_value(name: str, val: object) -> None:
        if isinstance(val, (dict, list)):
            raise ValueError(f"filters['{name}'] 须为标量值,不能为对象或数组")

    @staticmethod
    def _require_int_limit(limit: object) -> int:
        # bool 是 int 子类,必须排除
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit 须为整数")
        return limit

    def query_objects(self, object: str | None = None, filters: dict | None = None,
                      order_by: str | None = None, desc: bool = False, limit: int = 20) -> dict:
        started = time.perf_counter()
        if object is not None and not isinstance(object, str):
            raise ValueError("object 须为字符串")
        if filters is not None:
            if not isinstance(filters, dict):
                raise ValueError("filters 须为对象(属性→值映射),不能为数组或其他类型")
            for name, val in filters.items():
                if not isinstance(name, str):
                    raise ValueError("filters 键须为字符串")
                self._require_scalar_filter_value(name, val)
        if order_by is not None and not isinstance(order_by, str):
            raise ValueError("order_by 须为字符串")
        if not isinstance(desc, bool):
            raise ValueError("desc 须为布尔值")
        limit = self._require_int_limit(limit)
        if object is None:
            return self._object_catalog(started)

        # 目录仍用磁盘模板识别对象名;实际字段/敏感/绑定以 published 快照为准。
        disk_tpl = next((o for o in self.pack.objects if o.object == object), None)
        if disk_tpl is None:
            raise ValueError(f"未知对象 '{object}',可用:{sorted(self.pack.object_names())}")

        with self._published_tx() as (store, snap):
            tpl = next((o for o in snap.template_pack.objects if o.object == object), None)
            entry = snap.objects.get(object)
            if tpl is None or entry is None:
                raise ValueError("not_published: 对象未包含在已发布数据集中")
            binding = next(
                (b for b in tpl.bindings if b.source == self.source and b.enabled),
                None,
            )
            physical = validate_build_table(entry.physical_table)
            sql, params = self._object_sql(tpl, filters, order_by, desc, limit, physical)
            rows = [dict(r) for r in _safe_execute(store.con, sql, params)]
            quarantined = self._quarantine_count(store.con, object)
            dataset_version = snap.dataset_version
            template_version = snap.template_version
            binding_hashes = {object: entry.binding_hash}
            binding_status = binding.status if binding is not None else "published"
            display_name = tpl.display_name
            sensitive = {p.name for p in tpl.properties if p.sensitive}

        for row in rows:
            for prop in sensitive:
                if row.get(prop) is not None:
                    row[prop] = MASK
        note = ("binding 为 draft:字段映射按参考表形构造,口径未经现场校准"
                if binding is not None and binding.status == "draft" else "")
        warnings = [note] if note else []
        qid = self._log_query(
            "query_objects", object,
            f"filters={filters or {}} rows={len(rows)}", warnings,
            dataset_version=dataset_version,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "object": object,
            "display_name": display_name,
            "rows": rows,
            "meta": self._public_meta(
                tool="query_objects", target=object, query_id=qid,
                row_count=len(rows), duration_ms=duration_ms,
                masked_fields=sorted(sensitive), warnings=warnings,
                source=self.source, binding_status=binding_status,
                quarantined=quarantined, note=note,
                dataset_version=dataset_version,
                template_version=template_version,
                binding_hashes=binding_hashes,
            ),
        }

    def _object_sql(self, tpl: ObjectTemplate, filters: dict | None,
                    order_by: str | None, desc: bool, limit: int,
                    physical_table: str) -> tuple[str, list]:
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
        table = _quote_ident(validate_build_table(physical_table))
        return (f'SELECT {cols} FROM {table}{where_sql}{order} LIMIT {limit}',
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

    def _object_catalog(self, started: float | None = None) -> dict:
        t0 = time.perf_counter() if started is None else started
        duration_ms = int((time.perf_counter() - t0) * 1000)
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
            "meta": self._public_meta(
                tool="query_objects", target="", query_id=None,
                row_count=None, duration_ms=duration_ms,
                active_source=self.source,
                usage="带 object 参数查询数据;filters 为 属性→值 等值筛选",
            ),
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
        started = time.perf_counter()
        if metric is not None and not isinstance(metric, str):
            raise ValueError("metric 须为字符串")
        if group_by is not None and not isinstance(group_by, str):
            raise ValueError("group_by 须为字符串")
        limit = self._require_int_limit(limit)
        if metric is None:
            return self._metric_catalog(started)

        disk_def = next((m for m in self.pack.metrics if m.metric == metric), None)
        if disk_def is None:
            raise ValueError(f"未知指标 '{metric}',可用:{[m.metric for m in self.pack.metrics]}")
        impl = self.metrics.get(metric)
        if impl is None:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return {
                "metric": disk_def.metric, "display_name": disk_def.display_name,
                "status": disk_def.status, "formula": disk_def.formula,
                "grain": disk_def.grain, "caveats": disk_def.caveats,
                "freshness_sla": disk_def.freshness_sla,
                "implemented": False,
                "reason": "依赖应收 / 回款对象,不在首批对象内;口径定义保留,待对象补齐后实现",
                "meta": self._public_meta(
                    tool="query_metrics", target=metric, query_id=None,
                    row_count=None, duration_ms=duration_ms,
                    warnings=["指标尚未实现"],
                ),
            }

        dim = group_by or impl.default_dim
        if dim not in impl.dims:
            raise ValueError(f"'{metric}' 支持的 group_by:{sorted(impl.dims)},got '{dim}'")
        order = '"group" DESC' if dim == "月" else "value DESC"
        limit = max(1, min(int(limit), 200))

        with self._published_tx() as (store, snap):
            mdef = next(
                (m for m in snap.template_pack.metrics if m.metric == metric), None,
            )
            if mdef is None:
                raise ValueError("not_published: 指标未包含在已发布数据集中")
            tables = {
                name: snap.objects[name].physical_table
                for name in impl.depends_on
                if name in snap.objects
            }
            sql = impl.render_sql(tables, dim=impl.dims[dim], order=order)
            rows = [dict(r) for r in _safe_execute(store.con, sql, (limit,))]
            dataset_version = snap.dataset_version
            template_version = snap.template_version
            binding_hashes = {
                name: snap.objects[name].binding_hash for name in impl.depends_on
            }
            definition = {
                "metric": mdef.metric, "display_name": mdef.display_name,
                "status": mdef.status, "formula": mdef.formula,
                "grain": mdef.grain, "caveats": mdef.caveats,
                "freshness_sla": mdef.freshness_sla,
            }
            status = mdef.status
            caveats = mdef.caveats

        warning = "口径为 draft(未经校准),数值仅供演示环境参考" if status != "certified" else ""
        warnings = [w for w in (warning, caveats) if w]
        qid = self._log_query(
            "query_metrics", metric,
            f"group_by={dim} rows={len(rows)}", warnings,
            dataset_version=dataset_version,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            **definition, "implemented": True, "unit": impl.unit,
            "group_by": dim, "rows": rows,
            "meta": self._public_meta(
                tool="query_metrics", target=metric, query_id=qid,
                row_count=len(rows), duration_ms=duration_ms,
                warnings=warnings, warning=warning,
                dataset_version=dataset_version,
                template_version=template_version,
                binding_hashes=binding_hashes,
            ),
        }

    def _metric_catalog(self, started: float | None = None) -> dict:
        t0 = time.perf_counter() if started is None else started
        duration_ms = int((time.perf_counter() - t0) * 1000)
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
            "meta": self._public_meta(
                tool="query_metrics", target="", query_id=None,
                row_count=None, duration_ms=duration_ms,
                usage="带 metric 参数取数;group_by 见各指标 group_by_options",
            ),
        }
