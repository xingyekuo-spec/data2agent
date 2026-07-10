"""只读查询服务:消费元模型 binding 生成 SQL,默认脱敏,传输无关。

安全边界(lite):
- SQLite 以只读模式(mode=ro)打开;
- SQL 只由 binding 声明的表 / 字段生成,天然白名单;
- sensitive 属性一律脱敏为 "***",lite 不提供解敏开关(解敏属治理档位,商业版范围)。

指标实现说明:query_metrics 的 SQL 目前绑定展厅 E10 表形,
待抽取管道 / 语义层就位后改为面向对象层取数。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..mapping import build_select
from ..metamodel.loader import load_pack

MASK = "***"


@dataclass(frozen=True)
class MetricImpl:
    dims: dict[str, str]      # 维度名 -> SQL 分组表达式
    default_dim: str
    unit: str
    sql: str                  # 含 {dim} 占位;LIMIT 参数化


_MARGIN_SQL = """
SELECT {dim} AS "group",
       ROUND(1.0 - SUM(i.STANDARD_COST * d.QUANTITY) / SUM(d.AMOUNT * h.EXCHANGE_RATE), 4) AS value,
       ROUND(SUM(d.AMOUNT * h.EXCHANGE_RATE), 2) AS revenue_cny,
       COUNT(DISTINCT h.Id) AS order_count
FROM SALES_ORDER_D d
JOIN SALES_ORDER h ON h.Id = d.SALES_ORDER_ID
JOIN ITEM i ON i.Id = d.ITEM_ID
JOIN CUSTOMER c ON c.Id = h.CUSTOMER_ID
WHERE h.INVALID_STATE = 'N' AND h.APPROVE_DATE IS NOT NULL
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_RESPONSE_SQL = """
SELECT {dim} AS "group",
       ROUND(AVG((JULIANDAY(q.SUBMIT_DATE) - JULIANDAY(q.INQUIRY_DATE)) * 24), 1) AS value,
       COUNT(*) AS quote_count
FROM QUOTATION q
JOIN CUSTOMER c ON c.Id = q.CUSTOMER_ID
WHERE q.SUBMIT_DATE IS NOT NULL
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_METRICS: dict[str, MetricImpl | None] = {
    "gross_margin_rate": MetricImpl(
        dims={
            "月": "SUBSTR(h.DOC_DATE, 1, 7)",
            "客户": "c.CUSTOMER_CODE || ' ' || c.CUSTOMER_SHORT_NAME",
            "品类": "i.CATEGORY_CODE",
            "区域": "c.COUNTRY_REGION",
        },
        default_dim="月",
        unit="比率(0-1,CNY 口径,收入按订单汇率折算)",
        sql=_MARGIN_SQL,
    ),
    "quote_response_hours": MetricImpl(
        dims={
            "月": "SUBSTR(q.DOC_DATE, 1, 7)",
            "客户": "c.CUSTOMER_CODE || ' ' || c.CUSTOMER_SHORT_NAME",
        },
        default_dim="月",
        unit="小时(均值)",
        sql=_RESPONSE_SQL,
    ),
    # 依赖应收 / 回款对象,不在首批 5 个对象内;口径定义保留于 templates/metrics
    "overdue_receivable_amount": None,
}


class QueryService:
    def __init__(self, db_path: str | Path, templates_root: str | Path = "templates",
                 source: str = "digiwin_e10"):
        self.db_path = str(db_path)
        self.source = source
        self.pack = load_pack(templates_root)

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

        sql, params, exprs = build_select(
            tpl, binding, filters=filters, order_by=order_by, desc=desc, limit=limit)
        con = self._connect()
        try:
            rows = [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()

        sensitive = {p.name for p in tpl.properties if p.sensitive}
        for row in rows:
            for prop, e in exprs.items():
                if e.value_map and row.get(prop) is not None:
                    row[prop] = e.value_map.get(row[prop], row[prop])
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
                "masked_fields": sorted(sensitive & set(exprs)),
                "row_count": len(rows),
                "note": "binding 为 draft:字段映射按参考表形构造,口径未经现场校准"
                        if binding.status == "draft" else "",
            },
        }

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
        impl = _METRICS.get(metric)
        if impl is None:
            return {
                **definition, "implemented": False,
                "reason": "依赖应收 / 回款对象,不在首批 5 个对象内;口径定义保留,待对象补齐后实现",
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
                    "implemented": _METRICS.get(m.metric) is not None,
                    **({"group_by_options": sorted(_METRICS[m.metric].dims)}
                       if _METRICS.get(m.metric) else {}),
                }
                for m in self.pack.metrics
            ],
            "meta": {"usage": "带 metric 参数取数;group_by 见各指标 group_by_options"},
        }
