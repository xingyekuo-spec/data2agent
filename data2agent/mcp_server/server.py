"""FastMCP 封装:把 QueryService 暴露为 MCP 只读工具(stdio)。"""

from __future__ import annotations

import secrets
import threading
import weakref
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from .core import QueryService
from .evidence import EvidenceContext

INSTRUCTIONS = """\
data2agent 数据网关(lite):把制造业 ERP 数据按业务对象模型提供给 Agent。
- query_objects:按对象查业务数据(客户 / 品号 / 报价单 / 销售订单 / 订单明细);
- query_metrics:按口径定义查经营指标(毛利率 / 报价响应时长等);
- propose_action:生成结构化建议卡(「说」档)—— 不执行任何写操作,
  每条依据必须引用前序查询的 meta.query_id 与 meta.result_digest,数字可溯源。
查询均为只读;敏感字段(联系方式、标准成本)默认脱敏;
binding_status=draft 表示字段映射未经现场数据字典校准,结论请注明口径来源。
"""


class _SessionEvidenceContextResolver:
    """按实际 ServerSession 分配 evidence session，且不延长连接生命周期。"""

    def __init__(self, *, principal: str, channel: str) -> None:
        self._principal = principal
        self._channel = channel
        self._session_ids: weakref.WeakKeyDictionary[object, str] = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    def context_for(self, session: object) -> EvidenceContext:
        with self._lock:
            session_id = self._session_ids.get(session)
            if session_id is None:
                session_id = f"mcp_{secrets.token_urlsafe(18)}"
                self._session_ids[session] = session_id
        return EvidenceContext(
            principal=self._principal,
            session_id=session_id,
            channel=self._channel,
        )


def create_server(db: str | Path, templates: str | Path = "templates",
                  source: str = "digiwin_e10", max_tier: str = "说",
                  host: str = "127.0.0.1", port: int = 8848,
                  rate_per_minute: int = 0, audit_sink=None,
                  transport: str = "stdio",
                  principal: str | None = None) -> FastMCP:
    from .http import RateLimiter

    svc = QueryService(db, templates, source, max_tier=max_tier, audit_sink=audit_sink)
    limiter = RateLimiter(rate_per_minute)
    server = FastMCP("data2agent", instructions=INSTRUCTIONS, host=host, port=port)
    channel = "mcp_http" if transport == "http" else "mcp_stdio"
    principal_name = principal or ("mcp:http" if transport == "http" else "mcp:local")
    session_contexts = _SessionEvidenceContextResolver(
        principal=principal_name,
        channel=channel,
    )

    def _session_context(ctx: Context) -> EvidenceContext:
        return session_contexts.context_for(ctx.session)

    @server.tool()
    def query_objects(object: str | None = None,
                      filters: dict[str, str | int | float] | None = None,
                      order_by: str | None = None, desc: bool = False,
                      limit: int = 20, ctx: Context | None = None) -> dict:
        """查询业务对象(只读,敏感字段默认脱敏)。

        不带 object 参数返回对象目录(含每个对象的属性 / 状态 / 可用数据源);
        带 object 参数返回数据行:filters 为 属性→值 的等值筛选
        (枚举属性用对象模型取值,如 result=成交),order_by / desc 排序,
        limit 默认 20、上限 200。
        """
        limiter.check("query_objects")
        return svc.query_objects(
            object, filters, order_by, desc, limit,
            context=_session_context(ctx) if ctx is not None else None,
        )

    @server.tool()
    def query_metrics(metric: str | None = None, group_by: str | None = None,
                      limit: int = 24, ctx: Context | None = None) -> dict:
        """查询经营指标(只读,附口径定义与 caveats)。

        不带 metric 参数返回指标目录(含公式 / 粒度 / 是否已实现 / 可用 group_by);
        带 metric 参数返回分组取数结果,group_by 缺省为该指标默认维度。
        指标 status=draft 表示口径未经校准,引用数值时请附带说明。
        """
        limiter.check("query_metrics")
        return svc.query_metrics(
            metric, group_by, limit,
            context=_session_context(ctx) if ctx is not None else None,
        )

    @server.tool()
    def propose_action(object: str, action: str, conclusion: str,
                       evidence: list[dict[str, str]], ctx: Context | None = None) -> dict:
        """生成结构化建议卡(「说」档,不执行任何写操作)。

        action 须为对象模板声明的动作(见 query_objects 目录);
        conclusion 为明确结论(如:谨慎接,建议还价至 30 USD);
        evidence 为依据列表 [{claim: 人话陈述, query_id: 前序查询的 meta.query_id,
        result_digest: 该查询 meta.result_digest}],
        引用不到真实查询会被拒绝 —— 建议卡里的每个数字都必须可溯源。
        返回卡片含档位、聚合口径警示与治理声明。
        """
        limiter.check("propose_action")
        return svc.propose_action(
            object,
            action,
            conclusion,
            evidence,
            context=_session_context(ctx) if ctx is not None else None,
        )

    return server
