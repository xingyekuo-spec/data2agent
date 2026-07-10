# data2agent

> **Data to Agent, for factories —— 把工厂数据接给 AI Agent。**

<!-- 老板测试留位(public 前必须完成):
     首屏放一张"接单评审卡"截图 + Agent 生成过程 GIF,
     让非技术人 30 秒看到 Agent 的工作成果,而不是架构图。 -->

> ⚠️ 当前处于 pre-release 私有开发阶段,首个工厂验证完成后公开。

## 这是什么

中小制造企业的数据(鼎捷 / 金蝶 / 用友 ERP、MES、SRM、CAD、Excel)大多锁在没有 API 的老系统里。
`data2agent` 提供一条经过真实工厂验证的路,把这些数据安全地接给任何 AI Agent:

- **抽取框架**:只读直连 / API 轮询适配器、ELT(原样落地 → 声明式映射)、水位 + 回看 + 分段 checksum 增量、隔离区;
- **国产 ERP 连接器**:鼎捷 E10 / 易飞参考映射 + 表结构字典(持续积累于 [docs/dict](docs/dict/));
- **制造业本体模板 + 元模型**:业务对象的声明式模板(YAML,首批 5 个 / 规划 18 个),`validate` 一键校验;
- **MCP Server(lite)**:`query_objects` / `query_metrics` 两个只读工具(默认脱敏、口径警示内建),任何支持 MCP 的 Agent 五分钟接入;
- **数字厂长展厅**:模拟工厂数据已就绪(渔具外销厂,E10 参考表形,一条命令生成);多 Agent 接单评审演示链规划中。

**安全承诺**:装进你内网、碰你数据库的每一行代码都在这个仓库里 —— 只读账号、白名单表、限时限流、错峰窗口,全部可审计。这也是我们开源它的首要原因。

## 快速开始

```bash
pip install -e ".[dev,mcp]"
pytest tests -q                                   # 35 passed
python -m data2agent.metamodel.validate templates # 模板校验
python -m data2agent.showroom.seed                # 生成展厅模拟库 showroom/e10.sqlite(E10 参考表形)
python -m data2agent.connect sync --sqlite showroom/e10.sqlite   # 抽取:水位增量 → 落地库(只读/白名单/审计)
python -m data2agent.connect apply                # 映射:raw_* → 物化对象层 obj_*(隔离区 + 熔断)
python -m data2agent.mcp_server                   # MCP Server 读对象层(stdio,只读 + 默认脱敏)

# 接入 Claude Code 试玩:
#   claude mcp add d2a-factory -- .venv/bin/python -m data2agent.mcp_server
# docker compose up   ← 展厅版本提供:模拟工厂 + MCP + 演示链(规划中)
```

## 设计文档

产品定位、架构、各组件详设见 [docs/design](docs/design/00-overview.md)(00 总览 → 01 元模型 → 02 抽取框架 → 03 MCP 网关 → 04 展厅)。

## 边界(诚实声明)

本仓库解决"数据如何安全地到达 Agent"。生产环境的**口径校准、数据对账、主数据对齐、审批治理、行业知识包**不在开源范围 —— 那是让 Agent 在真实工厂可靠干活的部分,属于商业版 BizMind。

## 贡献与安全

- 欢迎贡献其他 ERP(金蝶 / 用友 / E10)的 binding 与表字典,见 [CONTRIBUTING](CONTRIBUTING.md);
- 安全问题请走 [SECURITY.md](SECURITY.md),勿发公开 issue;
- 小团队维护,issue 响应尽力而为(通常一周内)。

Apache-2.0 © data2agent contributors
