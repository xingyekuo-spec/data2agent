# data2agent 路线图

> 状态: `v0.3.0` 之后进入 v0.5 ERP 元数据与抽取表管理加固;更新于 2026-07-24。

data2agent 已完成 v0.2 可观察控制台和 v0.3 可验证数据链。当前主线是把中间机抽取配置
与现场元数据发现做成可验收的生产路径（见
[ERP 元数据实施计划](superpowers/plans/2026-07-23-erp-metadata-extraction-management.md)）。

## 当前版本基线

已完成（含 v0.3 + ERP 元数据 M0–M5）:

- Vue Console `/` 与类型化管理 API。
- 数据集原子发布、Mapping Preview、字段血缘、MCP evidence。
- **抽取表显式配置**:`connect.yaml` 的 `tables` 为唯一事实来源；默认空清单。
- **元数据发现**:只读扫描、键/水位校验；中间机 `/metadata` + `/tables`。
- **配置业务键与复合键增量**；**`full_refresh` 快照原子替换**；ingest 协议 v2 严格校验。
- Windows 便携包路径与本地 E10-like 参考链仍可用于回归。

进行中（M7）:展厅 / Mock 迁出产品包。

M6（文档与全链路验收）已完成：干净切换审计、首次选表引导、便携包/smoke 门禁、文档与门控式 MSSQL 集成骨架。

仍未宣称:

- 正式生产试点就绪。
- 跨机器 commit receipt 与 E6b 跨机器对账。
- 生产 HTTPS/mTLS 与凭据轮换。
- ERP 写回、审批流、SaaS 多租户或完整 RBAC。

## 下一版本: v0.4（跨机可靠性）与并行清理

v0.4 仍聚焦批次 receipt、E6b、传输与凭据、SQLite 试点基线。与 ERP 元数据线并行的
**M7** 将把展厅/Mock 迁出产品包（见实施计划）。

建议里程碑顺序（跨机可靠性）:

| 里程碑 | 主要交付 | 发布门槛 |
| --- | --- | --- |
| M1 批次提交协议 | batch ID、摘要、schema fingerprint、commit receipt | 只有有效 receipt 后才推进水位 |
| M2 批次 Console | 批次状态与授权重放 | 不打开 SQLite 也能诊断批次 |
| M3 E6b 对账 | 源侧统计、平台比对、分段重抽 | 推送拓扑下可修复删除与静默修改 |
| M4 传输与凭据 | HTTPS/mTLS、凭据分离 | 生产路径不使用明文 HTTP |
| M5 SQLite 试点基线 | 容量/并发/备份恢复 | 满足已记录延迟与恢复目标 |
| M6 工厂试点验收 | 连续运行与演练报告 | 无未解释数据丢失或水位漂移 |

## 工厂试运行建议

正式试点前建议:

- 单工厂或类似参考链的环境;
- 源系统使用只读账号；抽取表经现场元数据确认;
- 不做 ERP 写回,不替代现有业务决策;
- Agent 结论必须有人复核;
- 每日备份落地库。

## 当前非目标

- ERP 写回或“做”档自动化。
- 完整 SaaS 多租户与超出试点范围的完整 RBAC。
- 在 SQLite 阈值被实际触发前迁移 PostgreSQL。
- 在 M7 完成前,暂不把 `data2agent/showroom` 从仓库删除;它仍是回归资产,不是产品运行模式。
