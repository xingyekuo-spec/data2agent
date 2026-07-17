# 04 · 数字厂长展厅

> 状态:SQLite/SQL Server 展厅数据链已实现;v0.2 Vue 展厅验收待建(r1,2026-07-17)· 实现:`data2agent/showroom/` + 根目录 `docker-compose.yml`
> 上层基线:[产品开发路线图](../superpowers/plans/2026-07-17-product-development-roadmap.md)

## 1. 定位

`docker compose up` 一键起一座模拟渔具工厂,跑通与真实部署**逻辑同构**的链路,让两类人各取所需:老板看接单评审演示,IT 看抽取框架面对 SQL Server 源时的窗口、限流和审计行为。展厅不等同生产部署:生产试点还要求跨机对账、批次回执和加密传输。

## 2. 当前拓扑

```
mssql-sim     SQL Server 容器,init 脚本灌入 E10 表形 + seed 数据(源系统替身)
   ▼ 抽取(connect,走 mssql_readonly 适配器,窗口/限流/审计全开)
landing-sqlite SQLite 落地库(raw_* + 物化对象表)
   ├─ mcp     MCP 网关(streamable HTTP :8848) → demo(脚本或 Agent 编排)
   └─ console Jinja 管理页(:8849);v0.2 增加 Vue Console(`/v1`)
```

本机快速版是 `seed → connect sync → connect apply → MCP`;源库和落地库均为 SQLite。
compose 版把源库换成 SQL Server 并常驻调度,落地库仍为共享卷内 SQLite。
PostgreSQL 不是当前展厅目标;只有达到产品路线定义的容量/并发阈值后才单独评估迁移。

## 3. 已实现

- E10 参考表形(6 表)+ 确定性 seed(渔具外销厂,seed=42 / asof=2026-07-10,金额/状态/溯源自洽);
- 表字典生成(`docs/dict/digiwin_e10.md`);
- binding↔表形一致性测试(防漂移)。

## 4. 接单评审演示链(粗颗粒,详设前置条件:网关"说"档)

```
询单(自然语言)→ 解析规格/数量/目标价
  → 历史检索:该客户 + 同型谱产品的成交/未成交报价(query_objects)
  → 口径取数:毛利率、响应时长基线(query_metrics)
  → 风险项:账期、交期档期(钓季)、汇率假设
  → 输出:接单评审建议卡(接/谨慎接/不接 + 依据 + 口径警示)
```

两个可用版本("说"档已落地):
- **脚本版(离线可跑)**:`python -m data2agent.showroom.review_demo` —— 走与真 Agent
  相同的工具调用链(query_objects ×2 → query_metrics → propose_action),终端输出建议卡;
- **真 Agent 版**:任意 MCP 客户端按 `docs/demo/quote-review.md` 的提示词驱动,主角客户 C002。

## 5. v0.2 Vue 展厅验收(待建)

展厅是 Vue Console 接入真实 API 的发布门槛,不能只演示静态 Mock。

### 5.1 两种明确模式

| 模式 | 数据来源 | 用途 | 页面标识 |
| --- | --- | --- | --- |
| Mock | 提交在前端的 typed fixtures | 首次安装、运行中、推送失败、熔断、stale、服务不可达等难稳定复现状态 | `MOCK` 水印 |
| Demo | Docker MSSQL → connector → SQLite → MCP/Console 真实链 | 验证真实 API、数据浏览、隔离、口径警示和建议卡 | `DEMO` 标识 |

Mock 不得生成正式验收结论;Demo 也不等于生产安全验证。

### 5.2 v0.2 必验页面

1. `/v1` 总览显示 ERP→抽取→推送/落地→映射→对象→MCP 节点;
2. 运行详情能展开表、水位、批次、输入输出与错误;
3. raw/object 浏览返回真实 seed 样本,有分页和敏感标识;
4. 模板页展示 5 个对象、binding draft 和字段映射;
5. 隔离页能用注入坏数据的 fixture/测试链展示隔离与熔断,不能把空隔离区写死为正常;
6. MCP Lab 完成 `query_objects → query_metrics → propose_action` 并展开 evidence;
7. `/` Jinja、`/v0` 简版页和 `/v1` Vue 同时可访问;
8. 前端资产不依赖外部 CDN。

### 5.3 后续版本验收

- v0.3:增加字段血缘、映射 preview、dataset version、原子发布和会话证据场景;
- v0.4:增加网络失败/重试、commit receipt、schema mismatch、E6b 和 TLS 入口验证;
- PostgreSQL 只有达到 SQLite 换库阈值后才进入展厅矩阵。

## 6. 决议记录

- compose 已落地(仓库根 `docker-compose.yml`):mssql(SQL Server 2022,`MSSQL_IMAGE`/`MSSQL_PLATFORM` 可切 Azure SQL Edge)→ seed(灌数 + 建只读账号 d2a_reader)→ connector(serve 常驻,走只读账号)→ 共享卷 → mcp(streamable-http :8848);
- 演示链编排载体 = 脚本版(入库,离线可跑)+ 真 Agent 版(MCP 提示词)并存,见 §4;
- Vue Console 进入 v0.2 当前主路线;Jinja 管理页继续作为安装与故障恢复入口;
- 待议:seed 数据"每日自动演进"(模拟工厂持续下单,让增量抽取有活干)—— 留给使用反馈拉动。
