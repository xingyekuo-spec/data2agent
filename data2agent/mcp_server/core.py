"""只读查询服务:读落地库物化对象层(obj_*),默认脱敏,传输无关。

E4 起网关消费完整管道的产物:源系统 → sync(raw_*)→ apply(obj_*)→ 本服务。
枚举值 / 编码翻译在映射应用阶段已完成,网关只做属性校验、脱敏与口径警示。

安全边界(lite):
- 落地库以只读模式(mode=ro)打开;
- SQL 只引用模板声明的属性列与 obj_ 表,天然白名单;
- sensitive 属性一律脱敏为 "***",lite 不提供解敏开关(解敏属治理档位,商业版范围)。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..metamodel.loader import load_pack
from ..metamodel.schema import ObjectTemplate
from .metrics_impl import registry

MASK = "***"


class QueryService:
    def __init__(self, db_path: str | Path, templates_root: str | Path = "templates",
                 source: str = "digiwin_e10"):
        self.db_path = str(db_path)
        self.source = source
        self.pack = load_pack(templates_root)
        self.metrics = registry(source)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    # ---- query_objects ----

    def query_objects(self, object: str | None = None, filters: dict | None = None,
                      order_by: str | None = None, desc: bool = False, limit: int = 20) -> dict:
        if object is None:
            return self._object_catalog()

        tpl = next((o for o in self.pack.objects if o.object == object), None)
        if tpl is None:
            raise ValueError(f"未知对象 '{object}',可用:{sorted(self.pack.object_names())}")
        binding = next((b for b in tpl.bindings if b.source == self.source), None)
        if binding is None:
            raise ValueError(f"{object} 没有 source={self.source} 的 binding")

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
        return {
            "object": object,
            "display_name": tpl.display_name,
            "rows": rows,
            "meta": {
                "source": binding.source,
                "binding_status": binding.status,
                "masked_fields": sorted(sensitive),
                "row_count": len(rows),
                "quarantined": quarantined,
                "note": "binding 为 draft:字段映射按参考表形构造,口径未经现场校准"
                        if binding.status == "draft" else "",
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
        return {
            **definition, "implemented": True, "unit": impl.unit,
            "group_by": dim, "rows": rows,
            "meta": {"row_count": len(rows), "warning": warning},
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
