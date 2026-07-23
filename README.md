# data2agent

> **Data to Agent, for factories —— 把工厂数据接给 AI Agent。**

一句询单,Agent 查历史成交 / 毛利基线 / 账期后给出**每个数字可溯源**的接单评审建议。

> ⚠️ 当前处于 pre-release 私有开发阶段,首个工厂验证完成后公开。

## 这是什么

中小制造企业的数据(鼎捷 / 金蝶 / 用友 ERP、MES、SRM、CAD、Excel)大多锁在没有 API 的老系统里。
`data2agent` 正在把一条已通过 E10-like 参考数据链验证的方案推进到真实工厂试点,目标是把数据安全地接给任何 AI Agent:

- **抽取框架**:只读直连、ELT(原样落地 → 声明式映射)、水位 + 回看 + 分段对账、隔离区(API 轮询适配器按真实来源需求建设,当前未实现);
- **国产 ERP 连接器**:鼎捷 E10 / 易飞参考映射 + 表结构字典(持续积累于 [docs/dict](docs/dict/));
- **制造业本体模板 + 元模型**:业务对象的声明式模板(YAML,首批 5 个 / 规划 18 个),`validate` 一键校验;v0.3 已加入模板/绑定摘要、数据集版本和原子发布;
- **MCP Server(lite)**:`query_objects` / `query_metrics` 只读工具 + `propose_action` 建议卡(「说」档:依据必须引用已记录查询 ID 和结果摘要,默认脱敏、口径警示内建;主体/会话/结果摘要级证据已持久化),任何支持 MCP 的 Agent 五分钟接入;HTTP 部署默认强制 Token + 每工具限流 + 查询审计;
- **运维 / 管理界面**:平台 `console`(`:8849`)已统一为 Vue Console(`/`),覆盖首次配置、配置编辑、日志、日常监控、数据验证、字段血缘、MCP 证据和一键验收;
  中间机 `middle_admin`(`:8851`)保留轻量本机配置与日志入口。现场推荐[便携包](docs/runbook/portable.md)
  双击 `data2agent.exe`,链路验收见 [push-validation](docs/runbook/push-validation.md);
  管理 API 契约快照见 `console-ui/openapi.json`(用 `python scripts/export_console_openapi.py` 重新生成);
- **参考数据与回归资产**:仓库保留 E10-like seed、SQL Server/SQLite 参考链和接单评审脚本,用于自动测试、字典生成和本地验收,不作为产品运行模式对外承诺。

**安全承诺**:装进你内网、碰你数据库的每一行代码都在这个仓库里 —— 只读账号、白名单表、限时限流、错峰窗口,全部可审计,可逐行核对。

## 现场部署

现场部署使用[便携包](docs/runbook/portable.md):两台机器分别解压对应 zip,双击 `data2agent.exe`,
平台机在 `/setup` 完成首次配置。推送链路验收见 [push-validation](docs/runbook/push-validation.md)。

## 开发者本地快速开始

完整源码开发运行步骤见 [docs/runbook/source-dev.md](docs/runbook/source-dev.md)。

```bash
pip install -e ".[dev,mcp,console,ingest,connect,middle_admin,excel]"
pytest tests -q                                   # Python 回归测试(mssql 集成测试需 Docker)
python -m data2agent.metamodel.validate templates # 模板校验
python -m data2agent.showroom.seed                # 生成 E10-like 参考库 showroom/e10.sqlite
python -m data2agent.connect sync --config connect.example.yaml   # 抽取:水位增量 → 落地库(只读/白名单/审计)
python -m data2agent.connect apply                # 映射:raw_* → 物化对象层 obj_*(隔离区 + 熔断)
python -m data2agent.console --landing landing/factory.sqlite --templates templates
```

## 设计文档

产品定位、架构、各组件详设见 [docs/design](docs/design/00-overview.md)(00 总览 → 01 元模型 → 02 抽取框架 → 03 MCP 网关 → 04 参考数据链 → 05 控制台)。

现场拆机部署:[便携包](docs/runbook/portable.md) · [推送验收](docs/runbook/push-validation.md)。

## 边界(诚实声明)

data2agent 当前已完成 `v0.3.0`:在完整 MVP / 参考数据链基础上,补齐了可观察控制台、数据集原子发布、映射 Preview、字段血缘、MCP 会话证据和一键验收报告。它适合在工厂做**受控只读影子试运行**,但还未宣告正式生产试点就绪。正式试点前仍需完成 v0.4 的跨机批次回执、E6b 对账、生产加密传输、凭据治理、SQLite 负载/备份基线和连续运行验收;详见[路线图](docs/roadmap.md)。口径校准、主数据对齐、“做”档审批治理和行业知识包仍属后续能力。

## 贡献与安全

- 欢迎贡献其他 ERP(金蝶 / 用友 / E10)的 binding 与表字典,见 [CONTRIBUTING](CONTRIBUTING.md);
- 安全问题请走 [SECURITY.md](SECURITY.md),勿发公开 issue;
- 小团队维护,issue 响应尽力而为(通常一周内)。

Apache-2.0 © data2agent contributors
