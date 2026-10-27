# data2agent 路线图

> 状态:**平台端已移出本仓(2026-10-27)**,data2agent 聚焦中间机:只读抽取 + 推送到任意平台。
> v0.4 跨机可靠性随首个工厂生产试点通过现场验证。

本仓库此前包含的 Vue Console、映射 / 数据集发布、字段血缘、MCP 网关与平台便携包已移除;
中间机元数据发现与抽取表管理保持不变
（见 [ERP 元数据实施计划](superpowers/plans/2026-07-23-erp-metadata-extraction-management.md)，已收尾）。

## 当前版本基线

已完成:

- **通用 Webhook 推送**(`sink.type: webhook`,d2a-push v1):CloudEvents 信封、事件 id 幂等、
  HMAC 签名、快照原子替换语义;见 [webhook-push](runbook/webhook-push.md)。
- **管理界面选择推送出口**:首配向导与 `/config`「推送目标」可选 ingest v3 / 通用 Webhook,
  凭据只写 `secrets.env`,切到 Webhook 自动清除对账计划。
- **抽取表显式配置**、**元数据发现**、**配置业务键 / 复合键增量**、**full_refresh 快照**、ingest 协议 v2。
- M6 文档、便携包/smoke 与 Release MSSQL 门禁。
- E10 测试资产位于 `tests/fixtures/e10/`（不进产品包）。
- **C1-B** — AI Hub 对象级推送适配器(`sink.type: ai_hub` / `AiHubObjectPushSink`),
  与表级 `HttpPushSink`、通用 `WebhookSink` 并存,不得改 URL 混用。加密 spool、pending 排空、
  崩溃恢复去重与 mock 契约已在本仓落地;见 [push-validation](runbook/push-validation.md) §2
  与 [source-dev](runbook/source-dev.md) §6.1。

已完成（补充）:

- **正式生产试点**:首个工厂试点已完成验证(跨机 receipt / generation 屏障 /
  E6b 与 SQLite 备份基线随试点通过现场演练)。

进行中 / 下一里程碑:

- **C1-C** — 与 AI Hub 跨仓联调、按来源打开 Push、再评估生产
  `deployment_mode` 放行 `ai_hub`。C1-B **不得**打开 AI Hub
  `DATA_INGEST_PUSH_ENABLED`,也不与 v0.6 工厂试点混为一谈。

仍未宣称:

- 生产 HTTPS 证书/反代验收、mTLS 与凭据轮换。
- ERP 写回、审批流、SaaS 多租户或完整 RBAC。
- AI Hub 推送的生产启用(属 C1-C)。

## 下一版本: v0.4（跨机可靠性）

> 编号沿用既有“可试点”门槛命名（批次回执 / E6b / TLS / SQLite 基线），与已发布的
> 产品版本号 `v0.5.1`（ERP 元数据能力）并行；正式试点需同时满足两者。

批次 receipt、generation 屏障、E6b、传输 fail-closed 默认与 SQLite 在线备份基线
已落地，并随首个工厂生产试点通过现场演练；下一步是生产 HTTPS 证书/反代与
凭据轮换等传输层加固验收。

## 工厂试运行建议

- 源系统使用只读账号；抽取表经现场元数据确认;
- 不做 ERP 写回,不替代现有业务决策;
- Agent 结论必须有人复核;
- 每日备份中间机状态库(`/recovery` 有恢复指引)。

## 待办事项

- **接收侧存储代码瘦身**:`shared/store/landing.py` 仍保留 ingest v3 接收侧方法
  (generation / 回执 / 对账 staging),当前只被 `tests/support/reference_ingest.py` 使用;
  可迁入测试支持包,进一步缩小产品包。
- **webhook 协议演进**:按接入方反馈评估批量多事件请求、gzip 压缩与 JSON Lines 变体。

## 当前非目标

- ERP 写回或“做”档自动化。
- 完整 SaaS 多租户与超出试点范围的完整 RBAC。
- 在 SQLite 阈值被实际触发前迁移 PostgreSQL。
