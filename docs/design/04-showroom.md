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

当前形态(阶段①产物)是**短路版**:seed 直接生成 SQLite,MCP 直读 —— 保留为"5 分钟快速试玩"模式;compose 版是"完整链路"模式,两者并存。

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

演示脚本(人肉驱动版)已就绪:`docs/demo/quote-review.md`,主角客户 C002。多 Agent 编排版待"说"档 `propose_action` 落地后设计。

## 5. 待决事项(第③阶段前决定)

- mssql-sim 用 SQL Server 官方镜像(licensing:Developer Edition 仅限非生产,展厅符合)还是 Azure SQL Edge(arm64 友好);
- seed 数据是否加"每日自动演进"(模拟工厂持续下单,让增量抽取有活干);
- 演示链的编排载体:Claude Code 子代理 / 独立脚本 / 任意 MCP 客户端通用提示词。
