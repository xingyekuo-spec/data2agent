# 00 · 产品总览

> 状态:产品设计基线 r4(2026-10-27,平台端移出本仓)· 本文是其余设计文档的入口;版本优先级与发布门槛以[路线图](../roadmap.md)为准。

## 1. 一句话定位

**Data to Agent, for factories** —— 一台装在工厂内网的**只读抽取中间机**:把锁在无 API 老系统里的 ERP 数据
安全、可审计地抽出来,推送到**任意**数据平台,再由平台交给 AI Agent。

本仓库只包含中间机。映射、对象层、MCP 网关与控制台属于接收方平台,不在本仓库内。

## 2. 问题与用户

中小制造企业(50–500 人)的经营数据分散在鼎捷 / 金蝶 / 用友 ERP 里。这些系统普遍没有可用的 API,
厂商接口贵且慢;而直接让平台连客户数据库,工厂 IT 不敢放行。

| 用户 | 诉求 | 对应交付 |
| --- | --- | --- |
| 工厂 IT / 实施伙伴 | 敢把它装进内网:只读、白名单、错峰、可审计 | 抽取框架(docs 02)+ 便携包 |
| 数据平台 / Agent 平台开发者 | 用最低成本接收干净、可增量的 raw 数据 | 通用 Webhook 推送([d2a-push v1](../runbook/webhook-push.md)) |
| 需要强一致的平台 | 发现物理删除与原地改动 | ingest v3 推送 + E6b 对账 |
| 连接器贡献者 | 清晰的表字典与抽取计划 | [E10 表字典](../dict/digiwin_e10.md) |

## 3. 总体架构

```
源系统(鼎捷 E10 / 易飞 / 金蝶 / 用友 …)
   │  只读适配器:白名单 / 错峰限时 / 限流 / 全量 SQL 审计          → docs 02
   ▼
中间机(本仓库,工厂内网)
   │  元数据发现 → 显式抽取计划(connect.yaml tables)              → docs 06
   │  水位增量 + 回看 + 复合键续跑 / full_refresh 快照              → docs 02
   │  控制状态库:水位 / 运行 / 推送记录(不持久化业务 Raw)
   │  管理界面 middle_admin(:8851)                                 → docs 08
   ▼  出站推送(sink)
   ├─ webhook   通用 HTTP 批量推送,CloudEvents 信封,任意平台一个端点即可接收
   ├─ http      ingest v3:generation 屏障 + 批次回执 + E6b 对账(接收方完整实现)
   └─ ai_hub    AI Hub PUSH_AGENT v1 对象级推送(生产启用属 C1-C)
   ▼
接收方数据平台(不在本仓库):映射 / 对象层 / 指标 / MCP / Agent
```

依赖方向只允许 `middle → shared → protocol`,由 `scripts/check_architecture_layers.py` 强制。

核心设计原则(违反即 bug):

1. **只读与白名单是代码强制**:抽取表清单在 `connect.yaml` 显式配置,适配器拒绝白名单外访问与写语句。
2. **数据驻留最小化**:中间机只保存控制状态;生产默认 `strict_stream` 不落盘 spool。
3. **推送契约先行**:所有跨机格式定义在 `data2agent/protocol` 与 [webhook 接入说明](../runbook/webhook-push.md);
   幂等键、重试语义、快照原子替换写入契约而不是靠约定。
4. **诚实声明**:webhook 无远端对账,配置层直接禁止 `reconcile_at`,不假装一致。

## 4. 产品范围(诚实声明)

- 已实现:SQLite / SQL Server 只读适配器、元数据发现与选表、水位增量 / 复合键 / full_refresh 快照、
  L1/L2 对账(local / http sink)、调度与错峰窗口、三种推送出口、管理界面与进程监管、Windows 便携包、
  控制状态库备份。
- 不在本仓库:映射与对象层、数据集发布、MCP 网关、平台控制台。旧版本中的这些能力已随平台端移出。
- 生产扩大部署前仍待完成:传输层加固(证书 / 反代 / mTLS)、凭据轮换、断网重放与连续运行验收。

## 5. 文档索引

- [02 抽取框架](02-extraction.md) —— 适配器、增量、对账、调度与部署形态
- [04 参考数据链](04-reference-chain.md) —— E10-like 测试资产与回归链
- [06 中间机抽取表配置](06-middle-table-extraction-config.md) —— 元数据发现与显式表计划
- [08 中间机管理界面](08-middle-admin-ui-optimization.md)
- [路线图](../roadmap.md)
- 现场 runbook:[便携包](../runbook/portable.md) · [Webhook 推送](../runbook/webhook-push.md) · [推送验收](../runbook/push-validation.md) · [源码开发](../runbook/source-dev.md)
- [E10 参考表字典](../dict/digiwin_e10.md) —— 生成文件,勿手改
