"""FastMCP 封装:把 QueryService 暴露为 MCP 只读工具(stdio)。"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import QueryService

INSTRUCTIONS = """\
data2agent 只读数据网关(lite):把制造业 ERP 数据按业务对象模型提供给 Agent。
- query_objects:按对象查业务数据(客户 / 品号 / 报价单 / 销售订单 / 订单明细);
- query_metrics:按口径定义查经营指标(毛利率 / 报价响应时长等)。
两个工具均为只读;敏感字段(联系方式、标准成本)默认脱敏;
binding_status=draft 表示字段映射未经现场数据字典校准,结论请注明口径来源。
"""


def create_server(db: str | Path, templates: str | Path = "templates",
                  source: str = "digiwin_e10") -> FastMCP:
    svc = QueryService(db, templates, source)
    server = FastMCP("data2agent", instructions=INSTRUCTIONS)

    @server.tool()
    def query_objects(object: str | None = None,
                      filters: dict[str, str | int | float] | None = None,
                      order_by: str | None = None, desc: bool = False,
                      limit: int = 20) -> dict:
        """查询业务对象(只读,敏感字段默认脱敏)。

        不带 object 参数返回对象目录(含每个对象的属性 / 状态 / 可用数据源);
        带 object 参数返回数据行:filters 为 属性→值 的等值筛选
        (枚举属性用对象模型取值,如 result=成交),order_by / desc 排序,
        limit 默认 20、上限 200。
        """
        return svc.query_objects(object, filters, order_by, desc, limit)

    @server.tool()
    def query_metrics(metric: str | None = None, group_by: str | None = None,
                      limit: int = 24) -> dict:
        """查询经营指标(只读,附口径定义与 caveats)。

        不带 metric 参数返回指标目录(含公式 / 粒度 / 是否已实现 / 可用 group_by);
        带 metric 参数返回分组取数结果,group_by 缺省为该指标默认维度。
        指标 status=draft 表示口径未经校准,引用数值时请附带说明。
        """
        return svc.query_metrics(metric, group_by, limit)

    return server
