# data2agent

> **Data to Agent, for factories —— 把工厂数据接给 AI Agent。**

一句询单,Agent 查历史成交 / 毛利基线 / 账期后给出**每个数字可溯源**的接单评审建议。

> ✅ 首个工厂生产试点已完成验证,当前处于公开准备阶段。

## 这是什么

中小制造企业的数据(鼎捷 / 金蝶 / 用友 ERP、MES、SRM、CAD、Excel)大多锁在没有 API 的老系统里。
`data2agent` 已通过 E10-like 参考数据链与首个真实工厂生产试点双重验证,目标是把数据安全地接给任何 AI Agent:

- **抽取框架**:只读直连、ELT(原样落地 → 声明式映射)、水位 + 回看 + 分段对账、隔离区(API 轮询适配器按真实来源需求建设,当前未实现);
- **国产 ERP 连接器**:鼎捷 E10 / 易飞参考映射 + 表结构字典(持续积累于 [docs/dict](docs/dict/));
- **制造业本体模板 + 元模型**:业务对象的声明式模板(YAML,首批 5 个 / 规划 18 个),`validate` 一键校验;v0.3 已加入模板/绑定摘要、数据集版本和原子发布;
- **MCP Server(lite)**:`query_objects` / `query_metrics` 只读工具 + `propose_action` 建议卡(「说」档:依据必须引用已记录查询 ID 和结果摘要,默认脱敏、口径警示内建;主体/会话/结果摘要级证据已持久化),任何支持 MCP 的 Agent 五分钟接入;HTTP 部署默认强制 Token + 每工具限流 + 查询审计;
- **运维 / 管理界面**:平台 `console`(`:8849`)已统一为 Vue Console(`/`),覆盖首次配置、配置编辑、日志、日常监控、数据验证、字段血缘、MCP 证据和一键验收;
  中间机 `middle_admin`(`:8851`)按任务分组:**运维**(状态总览 / 运行历史 / 推送记录 / 日志)与**配置**三步流程(1 连接 → 2 元数据选表 → 3 抽取计划)。
  v0.6 起管理端生产化:真实进程监管(launcher 拉起/崩溃重启/熔断)、生产就绪度检查、统一请求认证与严格 CSP、
  推送记录携带 generation/回执/重试元数据、状态页按「总览 → 摘要 → 折叠详情」分层、状态库只读恢复指引页(`/recovery`)。
  新安装默认空 `tables`,未选表不会访问 ERP 业务表。现场推荐[便携包](docs/runbook/portable.md)
  双击 `data2agent.exe`,链路验收见 [push-validation](docs/runbook/push-validation.md);
  管理 API 契约快照见 `console-ui/openapi.json`(用 `python scripts/export_console_openapi.py` 重新生成);
- **数据驻留边界(v0.6)**:中间机只持久化控制状态(水位/运行/推送/对账),**不持久化业务 Raw**;
  生产默认 `strict_stream` 严格流式不落盘 spool(可选受控加密卷),Raw 表不变量检查、状态库备份与节点灾备的边界写入设计文档;
- **测试 fixture**:E10-like schema/seed 位于 `tests/fixtures/e10/`，仅服务自动测试与本地验收，不进入产品 wheel。

**安全承诺**:装进你内网、碰你数据库的每一行代码都在这个仓库里 —— 只读账号、白名单表、限时限流、错峰窗口,全部可审计,可逐行核对。

## 现场部署

现场部署使用[便携包](docs/runbook/portable.md):两台机器分别解压对应 zip,双击 `data2agent.exe`,
平台机在 `/setup` 完成首次配置。中间机在 `/config` 配连接后,经 `/metadata` 扫描选表、
`/tables` 确认保存抽取计划(默认空清单)。推送链路验收见 [push-validation](docs/runbook/push-validation.md)。

升级顺序:v0.6 起中间机发送 ingest 协议 v3,v0.6 平台同时接受 v2/v3 —— **先升级平台,后升级中间机**,
旧中间机在新平台上可继续推送,链路不中断;反向顺序会被平台按协议版本拒绝。

## 代码结构

两端(monorepo 内的逻辑分层,由 `scripts/check_architecture_layers.py` 强制):

```
data2agent/
├── protocol/    # 跨机契约:ingest 推送协议模型与版本协商(两端唯一共享接口)
├── middle/      # 中间端产物:admin(:8851 管理界面)+ extract(只读抽取/调度/推送)
├── platform/    # 平台端产物:ingest(接收端)+ console(:8849)+ mcp_server + updater
└── shared/      # 共享领域层:store(落地/映射/发布/证据)、metamodel、config、
                 #   scenarios、admin(界面公共件)——不得依赖任何端目录
```

依赖方向仅允许 `middle → shared → protocol` 与 `platform → shared → protocol`;
两端之间禁止互相 import(契约测试见 `tests/contract/test_architecture_layers.py`)。

## 开发者本地快速开始

完整源码开发运行步骤见 [docs/runbook/source-dev.md](docs/runbook/source-dev.md)。
本地 `sink: local` 落地仅为开发/参考链路径;生产部署只有跨机推送一种形态
(见[现场部署](#现场部署))。

```bash
pip install -e ".[dev,mcp,console,ingest,connect,middle_admin,excel]"
python scripts/verify.py quick                    # 日常:按 Git 变更选择测试
python scripts/verify.py module erp               # 模块完成:ERP/抽取回归
python scripts/verify.py full                     # 合并前:完整回归(含前端与 E2E)
python -m data2agent.shared.metamodel.validate templates # 模板校验
python -m tests.fixtures.e10.seed --db /tmp/e10.sqlite   # 生成 E10-like 参考库(测试用)
python -m data2agent.middle.extract sync --config connect.example.yaml   # 抽取:水位增量 → 落地库(只读/白名单/审计)
python -m data2agent.middle.extract apply                # 映射:raw_* → 物化对象层 obj_*(隔离区 + 熔断)
python -m data2agent.platform.console --landing landing/factory.sqlite --templates templates
```

## 设计文档

产品定位、架构、各组件详设见 [docs/design](docs/design/00-overview.md)(00 总览 → 01 元模型 → 02 抽取框架 → 03 MCP 网关 → 04 参考数据链 → 05 控制台)。

现场拆机部署:[便携包](docs/runbook/portable.md) · [推送验收](docs/runbook/push-validation.md)。

## 边界(诚实声明)

data2agent 当前基线为 **`v0.6.1`**:

- **v0.3–v0.5**:可观察/可验证能力、ERP 元数据发现、抽取表显式管理、配置业务键、复合键增量、
  `full_refresh` 快照、中间机 `/metadata` `/tables`,以及跨机可靠性(批次回执、generation 屏障、
  E6b 对账、传输 fail-closed、SQLite 备份基线),随首个工厂生产试点完成现场验证;
- **v0.6**:ingest 协议 v3 与跨机一致性加固(generation 心跳/租约恢复/对账屏障);
  数据源平台签发制(登记分配 source + 按源 Token);平台便携包自更新(latest.json);
  中间机管理端生产化(进程监管、就绪度检查、统一认证与严格 CSP、推送元数据、状态页分层);
  数据驻留边界显式化(控制状态库语义、strict_stream/加密卷 spool 策略、Raw 不变量检查)。

传输层加固(HTTPS 证书/反代、mTLS、凭据轮换)仍是生产扩大部署前的待办;
口径校准、主数据对齐、“做”档审批治理和行业知识包仍属后续能力。
详见[路线图](docs/roadmap.md)。

## 贡献与安全

- 欢迎贡献其他 ERP(金蝶 / 用友 / E10)的 binding 与表字典,见 [CONTRIBUTING](CONTRIBUTING.md);
- 安全问题请走 [SECURITY.md](SECURITY.md),勿发公开 issue;
- 小团队维护,issue 响应尽力而为(通常一周内)。

Apache-2.0 © data2agent contributors
