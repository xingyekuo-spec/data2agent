# 04 · 数字厂长展厅

> 状态:占位级设计(2026-07-10)· 已实现部分:`data2agent/showroom/` · 详设待第③阶段前补充

## 1. 定位

`docker compose up` 一键起一座模拟渔具工厂,跑通**与真实部署完全相同的链路**,让两类人各取所需:老板看接单评审演示,IT 看抽取框架在"生产库"上的真实行为(窗口、限流、审计日志)。

## 2. 拓扑(目标形态)

```
mssql-sim     SQL Server 容器,init 脚本灌入 E10 表形 + seed 数据(源系统替身)
   ▼ 抽取(connect,走 mssql_readonly 适配器,窗口/限流/审计全开)
landing-pg    PostgreSQL 落地库(raw_* + 对象视图)
   ▼
mcp           MCP 网关(HTTP/SSE 模式)
   ▼
demo          接单评审演示链(脚本或 Agent 编排)
```

当前形态(E4 后)已是**全链路的 SQLite 快速版**:seed → connect sync → connect apply → MCP 读对象层,与生产链路同构,只是源库 / 落地库都是本地 SQLite;compose 版把源库换成真 SQL Server 并常驻调度,是同一条管道的容器化。

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
  完全相同的链路(query_objects ×2 → query_metrics → propose_action),终端输出建议卡;
- **真 Agent 版**:任意 MCP 客户端按 `docs/demo/quote-review.md` 的提示词驱动,主角客户 C002。

## 5. 决议记录

- compose 已落地(仓库根 `docker-compose.yml`):mssql(SQL Server 2022,`MSSQL_IMAGE`/`MSSQL_PLATFORM` 可切 Azure SQL Edge)→ seed(灌数 + 建只读账号 d2a_reader)→ connector(serve 常驻,走只读账号)→ 共享卷 → mcp(streamable-http :8848);
- 演示链编排载体 = 脚本版(入库,离线可跑)+ 真 Agent 版(MCP 提示词)并存,见 §4;
- 待议:seed 数据"每日自动演进"(模拟工厂持续下单,让增量抽取有活干)—— 留给使用反馈拉动。
