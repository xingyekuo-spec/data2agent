# data2agent 路线图

> 状态: **`v0.5.1`** — ERP 元数据 M0–M7 已完成（产品包无 showroom，Console 仅真实 API）;
> v0.4 跨机可靠性随首个工厂生产试点通过现场验证;更新于 2026-08-03。

data2agent 已完成 v0.2 可观察控制台和 v0.3 可验证数据链,以及中间机元数据发现与抽取表管理
（见 [ERP 元数据实施计划](superpowers/plans/2026-07-23-erp-metadata-extraction-management.md)，已收尾）。

## 当前版本基线

已完成:

- Vue Console `/` 与类型化管理 API（仅真实 API）。
- 数据集原子发布、Mapping Preview、字段血缘、MCP evidence。
- **抽取表显式配置**、**元数据发现**、**配置业务键 / 复合键增量**、**full_refresh 快照**、ingest 协议 v2。
- M6 文档、便携包/smoke 与 Release MSSQL 门禁。
- E10 测试资产位于 `tests/fixtures/e10/`（不进产品包）。
- 产品包无展厅 / 无 Console Mock 运行模式。

进行中：无。

已完成（补充）:

- **正式生产试点**:首个工厂试点已完成验证(跨机 receipt / generation 屏障 /
  E6b 与 SQLite 备份基线随试点通过现场演练)。

仍未宣称:

- 生产 HTTPS 证书/反代验收、mTLS 与凭据轮换。
- ERP 写回、审批流、SaaS 多租户或完整 RBAC。

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
- 每日备份落地库。

## 当前非目标

- ERP 写回或“做”档自动化。
- 完整 SaaS 多租户与超出试点范围的完整 RBAC。
- 在 SQLite 阈值被实际触发前迁移 PostgreSQL。
