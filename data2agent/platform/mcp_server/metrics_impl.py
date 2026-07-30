"""指标实现注册表:按 MetricDef.metric 路由,SQL 只面向 published 物理表取数。

自 core.py 迁出(docs/design/03 §4);原毛利率的订单有效性过滤是唯一的
raw 穿透,派生状态(SalesOrder.state 决策表)落地后已清除 ——
指标层与源系统表形彻底解耦。

M2-T08:显式声明 depends_on,由同一 PublishedDatasetSnapshot 注入校验后的
物理表名;禁止硬编码遗留 obj_*。
"""

from __future__ import annotations

from dataclasses import dataclass

from data2agent.shared.metamodel.dataset_publish_contract import validate_build_table


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class MetricImpl:
    depends_on: frozenset[str]  # 显式依赖对象集合;必须来自同一 snapshot
    dims: dict[str, str]        # 维度名 -> SQL 分组表达式
    default_dim: str
    unit: str
    sql: str                    # 含 {dim}/{order}/{ObjectName} 占位;LIMIT 参数化

    def render_sql(self, tables: dict[str, str], *, dim: str, order: str) -> str:
        """用同一快照的物理表名渲染 SQL;缺依赖则失败(不回退 obj_*)。"""
        missing = sorted(self.depends_on - set(tables))
        if missing:
            raise ValueError("not_published: 指标依赖对象未发布")
        quoted: dict[str, str] = {}
        for name in self.depends_on:
            table = validate_build_table(tables[name])
            quoted[name] = _quote_ident(table)
        return self.sql.format(dim=dim, order=order, **quoted)


_MARGIN_SQL = """
SELECT {dim} AS "group",
       ROUND(1.0 - SUM(m.standard_cost * l.quantity)
                 / SUM(l.quantity * l.unit_price * o.fx_rate), 4) AS value,
       ROUND(SUM(l.quantity * l.unit_price * o.fx_rate), 2) AS revenue_cny,
       COUNT(DISTINCT o.order_no) AS order_count
FROM {SalesOrderLine} l
JOIN {SalesOrder} o ON o.order_no = l.order_no
JOIN {Material} m ON m.item_code = l.material
JOIN {Customer} c ON c.customer_code = o.customer
WHERE o.state NOT IN ('草稿', '已作废')
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_RESPONSE_SQL = """
SELECT {dim} AS "group",
       ROUND(AVG((JULIANDAY(q.submitted_at) - JULIANDAY(q.inquiry_at)) * 24), 1) AS value,
       COUNT(*) AS quote_count
FROM {Quotation} q
JOIN {Customer} c ON c.customer_code = q.customer
WHERE q.submitted_at IS NOT NULL
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_DEAD_STOCK_AMOUNT_SQL = """
SELECT {dim} AS "group",
       ROUND(SUM(d.dead_stock_amount), 2) AS value,
       COUNT(DISTINCT d.item_code) AS item_count
FROM {DeadStockItem} d
WHERE d.determination_status = 'dead_stock'
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_DEAD_STOCK_QUANTITY_SQL = """
SELECT {dim} AS "group",
       ROUND(SUM(d.inventory_qty), 4) AS value,
       COUNT(DISTINCT d.item_code) AS item_count
FROM {DeadStockItem} d
WHERE d.determination_status = 'dead_stock'
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_DEAD_STOCK_ITEM_COUNT_SQL = """
SELECT {dim} AS "group",
       COUNT(DISTINCT d.item_code) AS value,
       COUNT(DISTINCT d.warehouse_code) AS warehouse_count
FROM {DeadStockItem} d
WHERE d.determination_status = 'dead_stock'
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_ATTRIBUTION_COVERAGE_SQL = """
WITH dead AS (
  SELECT d.plant_id, d.warehouse_code, d.item_code
  FROM {DeadStockItem} d
  WHERE d.determination_status = 'dead_stock'
), attributed AS (
  SELECT DISTINCT plant_id, warehouse_code, item_code
  FROM {DeadStockAttribution}
)
SELECT {dim} AS "group",
       ROUND(1.0 * COUNT(DISTINCT CASE WHEN a.item_code IS NOT NULL THEN d.item_code END)
                 / NULLIF(COUNT(DISTINCT d.item_code), 0), 4) AS value,
       COUNT(DISTINCT d.item_code) AS dead_stock_item_count,
       COUNT(DISTINCT CASE WHEN a.item_code IS NOT NULL THEN d.item_code END) AS attributed_item_count
FROM dead d
LEFT JOIN attributed a ON a.plant_id = d.plant_id
                       AND a.warehouse_code = d.warehouse_code
                       AND a.item_code = d.item_code
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_ATTRIBUTION_DISTRIBUTION_SQL = """
SELECT {dim} AS "group",
       COUNT(DISTINCT a.item_code) AS value,
       ROUND(SUM(d.dead_stock_amount), 2) AS attributed_dead_stock_amount,
       COUNT(*) AS attribution_count
FROM {DeadStockAttribution} a
JOIN {DeadStockItem} d ON d.plant_id = a.plant_id
                       AND d.warehouse_code = a.warehouse_code
                       AND d.item_code = a.item_code
WHERE d.determination_status = 'dead_stock'
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_SUBSTITUTE_CONSUMABLE_SQL = """
SELECT {dim} AS "group",
       ROUND(SUM(s.potential_consume_qty), 4) AS value,
       COUNT(*) AS candidate_count
FROM {MaterialSubstituteCandidate} s
WHERE s.candidate_type = 'bom_consumption'
  AND s.calculation_status = 'candidate'
GROUP BY "group"
ORDER BY {order}
LIMIT ?
"""

_DEAD_STOCK_DIMS = {
    "工厂": "d.plant_id",
    "仓库": "d.warehouse_code",
    "物料类型": "d.material_type",
    "账龄段": (
        "CASE "
        "WHEN d.dead_stock_days <= 180 THEN '91-180天' "
        "WHEN d.dead_stock_days <= 365 THEN '181-365天' "
        "ELSE '365天以上' END"
    ),
}

_ATTRIBUTION_COVERAGE_DIMS = {
    "工厂": "d.plant_id",
}

_ATTRIBUTION_DISTRIBUTION_DIMS = {
    "工厂": "a.plant_id",
    "根因": "a.root_cause",
    "置信度等级": "a.confidence_level",
}

_SUBSTITUTE_CONSUMABLE_DIMS = {
    "来源工厂": "s.source_plant_id",
    "目标工厂": "s.target_plant_id",
    "物料": "s.item_code",
}


def registry(source: str) -> dict[str, MetricImpl | None]:
    """指标 id -> 实现;None 表示口径已定义但依赖对象未覆盖。
    (source 暂未使用 —— 指标只读对象层;保留参数供多源差异化时用。)"""
    return {
        "gross_margin_rate": MetricImpl(
            depends_on=frozenset({
                "SalesOrder", "SalesOrderLine", "Material", "Customer",
            }),
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
            depends_on=frozenset({"Quotation", "Customer"}),
            dims={
                "月": "SUBSTR(q.quote_date, 1, 7)",
                "客户": "q.customer || ' ' || c.name",
            },
            default_dim="月",
            unit="小时(均值)",
            sql=_RESPONSE_SQL,
        ),
        "dead_stock_amount": MetricImpl(
            depends_on=frozenset({"DeadStockItem"}),
            dims=_DEAD_STOCK_DIMS,
            default_dim="工厂",
            unit="CNY",
            sql=_DEAD_STOCK_AMOUNT_SQL,
        ),
        "dead_stock_quantity": MetricImpl(
            depends_on=frozenset({"DeadStockItem"}),
            dims=_DEAD_STOCK_DIMS,
            default_dim="工厂",
            unit="数量(物料原始单位混合,仅适合按同类/单位进一步分析)",
            sql=_DEAD_STOCK_QUANTITY_SQL,
        ),
        "dead_stock_item_count": MetricImpl(
            depends_on=frozenset({"DeadStockItem"}),
            dims=_DEAD_STOCK_DIMS,
            default_dim="工厂",
            unit="品号数",
            sql=_DEAD_STOCK_ITEM_COUNT_SQL,
        ),
        "attribution_coverage_rate": MetricImpl(
            depends_on=frozenset({"DeadStockItem", "DeadStockAttribution"}),
            dims=_ATTRIBUTION_COVERAGE_DIMS,
            default_dim="工厂",
            unit="比率(0-1)",
            sql=_ATTRIBUTION_COVERAGE_SQL,
        ),
        "attribution_distribution": MetricImpl(
            depends_on=frozenset({"DeadStockItem", "DeadStockAttribution"}),
            dims=_ATTRIBUTION_DISTRIBUTION_DIMS,
            default_dim="根因",
            unit="品号数（金额为多标签重复口径）",
            sql=_ATTRIBUTION_DISTRIBUTION_SQL,
        ),
        "substitute_consumable_quantity": MetricImpl(
            depends_on=frozenset({"MaterialSubstituteCandidate"}),
            dims=_SUBSTITUTE_CONSUMABLE_DIMS,
            default_dim="来源工厂",
            unit="数量(物料原始单位混合,仅适合按同类/单位进一步分析)",
            sql=_SUBSTITUTE_CONSUMABLE_SQL,
        ),
        # 依赖应收 / 回款对象,不在首批对象内;口径定义保留于 templates/metrics
        "overdue_receivable_amount": None,
    }
