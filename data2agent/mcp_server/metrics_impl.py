"""指标实现注册表:按 MetricDef.metric 路由,SQL 面向对象层(obj_*)取数。

自 core.py 迁出(docs/design/03-mcp-gateway.md §4 记录的过渡债务,已清偿)。
唯一保留的 raw 穿透:毛利率的订单有效性过滤(INVALID_STATE / APPROVE_DATE)——
对象层尚无派生状态属性(状态推导属映射层扩展,见 docs/design/01 §3.1),
补齐后删除此穿透。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..connect.landing import raw_table_name


@dataclass(frozen=True)
class MetricImpl:
    dims: dict[str, str]      # 维度名 -> SQL 分组表达式
    default_dim: str
    unit: str
    sql: str                  # 含 {dim} / {order} 占位;LIMIT 参数化


_MARGIN_SQL = """
SELECT {dim} AS "group",
       ROUND(1.0 - SUM(m.standard_cost * l.quantity)
                 / SUM(l.quantity * l.unit_price * o.fx_rate), 4) AS value,
       ROUND(SUM(l.quantity * l.unit_price * o.fx_rate), 2) AS revenue_cny,
       COUNT(DISTINCT o.order_no) AS order_count
FROM "obj_SalesOrderLine" l
JOIN "obj_SalesOrder" o ON o.order_no = l.order_no
JOIN "obj_Material" m ON m.item_code = l.material
JOIN "obj_Customer" c ON c.customer_code = o.customer
JOIN "{raw_sales_order}" so ON so.DOC_NO = o.order_no
WHERE so.INVALID_STATE = 'N' AND so.APPROVE_DATE IS NOT NULL
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_RESPONSE_SQL = """
SELECT {dim} AS "group",
       ROUND(AVG((JULIANDAY(q.submitted_at) - JULIANDAY(q.inquiry_at)) * 24), 1) AS value,
       COUNT(*) AS quote_count
FROM "obj_Quotation" q
JOIN "obj_Customer" c ON c.customer_code = q.customer
WHERE q.submitted_at IS NOT NULL
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""


def registry(source: str) -> dict[str, MetricImpl | None]:
    """指标 id -> 实现;None 表示口径已定义但依赖对象未覆盖。"""
    return {
        "gross_margin_rate": MetricImpl(
            dims={
                "月": "SUBSTR(o.order_date, 1, 7)",
                "客户": "o.customer || ' ' || c.name",
                "品类": "m.category",
                "区域": "c.region",
            },
            default_dim="月",
            unit="比率(0-1,CNY 口径,收入按订单汇率折算)",
            sql=_MARGIN_SQL.replace("{raw_sales_order}",
                                    raw_table_name(source, "SALES_ORDER")),
        ),
        "quote_response_hours": MetricImpl(
            dims={
                "月": "SUBSTR(q.quote_date, 1, 7)",
                "客户": "q.customer || ' ' || c.name",
            },
            default_dim="月",
            unit="小时(均值)",
            sql=_RESPONSE_SQL,
        ),
        # 依赖应收 / 回款对象,不在首批对象内;口径定义保留于 templates/metrics
        "overdue_receivable_amount": None,
    }
