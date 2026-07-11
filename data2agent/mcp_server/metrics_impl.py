"""指标实现注册表:按 MetricDef.metric 路由,SQL 只面向对象层(obj_*)取数。

自 core.py 迁出(docs/design/03 §4);原毛利率的订单有效性过滤是唯一的
raw 穿透,派生状态(SalesOrder.state 决策表)落地后已清除 ——
指标层与源系统表形彻底解耦。
"""

from __future__ import annotations

from dataclasses import dataclass


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
WHERE o.state NOT IN ('草稿', '已作废')
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
    """指标 id -> 实现;None 表示口径已定义但依赖对象未覆盖。
    (source 暂未使用 —— 指标只读对象层;保留参数供多源差异化时用。)"""
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
            sql=_MARGIN_SQL,
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
