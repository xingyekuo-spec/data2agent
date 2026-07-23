# data2agent 路线图

> 状态: `v0.3.0` (`2aff25b`) 之后的当前规划基线,更新于 2026-07-22。

data2agent 已完成 v0.2 可观察控制台和 v0.3 可验证数据链。项目现在处在“可做受控工厂影子试运行”和“正式生产试点加固”之间的边界。

## 当前版本: v0.3.0

v0.3.0 适合在工厂做受控、只读的影子试运行:系统与现有业务流程并行运行,不替代 ERP 流程,也不直接作为业务决策的唯一依据。

已完成:

- Vue Console `/v1`: Dashboard、Pipeline、Runs、Audit、Data、Quarantine、Templates、MCP Lab、Settings 和 Validation 页面。
- 类型化管理 API、OpenAPI 快照和生成式 TypeScript 客户端。
- raw/object 浏览:服务端分页、脱敏、访问审计,并能诚实表达 unknown、stale 和错误状态。
- 数据集与对象版本元数据、不可变对象版本表、数据集原子发布和回滚到上一稳定版本。
- Mapping Preview:复用正式转换核心,且不修改 published 数据、隔离区、水位或运行记录。
- 字段血缘:对象字段可追溯到 raw 记录、源列、转换规则、批次和 dataset/object version。
- MCP evidence:principal/session/query/proposal 记录、不可预测 query ID、result digest 和 proposal evidence 校验。
- Validation Run 与 JSON 报告,覆盖 v0.3 发布门槛。
- Windows 便携包路径和本地 E10-like 参考链仍可用于回归与验收检查。

仍未宣称:

- 正式生产试点就绪。
- 跨机器 commit receipt 与 schema fingerprint。
- E6b 跨机器对账。
- 生产 HTTPS/mTLS 与凭据轮换。
- 真实工厂负载下的 SQLite 容量、并发和备份恢复基线。
- ERP 写回、审批流、SaaS 多租户或完整 RBAC。

## 下一版本: v0.4

v0.4 是生产试点可靠性版本。目标是证明跨机器部署可以在真实工厂中正确搬运数据、可恢复故障,并形成可书面验收的证据链。

已完成的基础变更:

- **抽取表配置解耦**:ERP 表抽取配置从模板 binding 推导(`whitelist_from_bindings`)迁移到 `connect.yaml` 的显式 `tables` 字段。中间机 `connect.yaml` 负责"抽哪些表、怎么抽"(incremental/full_refresh + watermark),平台模板负责"怎么映射字段到业务对象",两个事实来源各司其职。中间机 `middle_admin` 配置页新增表策略编辑器,连接测试验证 PK 和 watermark 列存在,保存时做原子替换。

建议里程碑顺序:

| 里程碑 | 主要交付 | 发布门槛 |
| --- | --- | --- |
| M1 批次提交协议 | batch ID、行数、内容摘要、schema fingerprint、持久 commit receipt 和幂等重试 | 只有保存有效 receipt 后才推进水位 |
| M2 批次 Console | 批次状态、失败原因、receipt 详情和授权重放 | 工厂 IT 不打开 SQLite 也能诊断缺失、重复、重试中和摘要不一致的批次 |
| M3 E6b 对账 | 中间机驱动源侧统计、平台比对、分段重抽和软删 | 推送拓扑下可发现并修复删除和静默修改 |
| M4 传输与凭据 | HTTPS/mTLS 部署路径、ingest/console/MCP 凭据分离、轮换/吊销方案和主体审计 | 真实跨机器生产路径不使用明文 HTTP |
| M5 SQLite 试点基线 | 容量、并发、WAL checkpoint、备份和恢复测量,并定义 PostgreSQL 切换阈值 | 试点负载满足已记录的延迟和恢复目标 |
| M6 工厂试点验收 | 字典/binding 核对、连续运行一周、重启/网络/schema drift 演练和最终报告 | 试点窗口内无未解释的数据丢失或水位漂移 |

## 工厂试运行建议

v0.4 完成前,v0.3 只建议用于受控影子试运行:

- 单工厂或类似参考链的环境;
- 源系统使用只读账号和白名单表;
- 不做 ERP 写回,不替代现有业务决策;
- Agent 结论必须有人复核;
- 每日备份落地库;
- Console 中明确区分 `MOCK` / `REAL` 模式。

v0.4 通过后,项目才能进入正式工厂生产试点,并附带批次 receipt、对账、传输、容量和恢复证据。

## 当前非目标

- ERP 写回或“做”档自动化。
- 没有 preview、审核和回滚保护的在线生产 mapping 发布。
- 完整 SaaS 多租户。
- 超出试点凭据/主体控制范围的完整 RBAC。
- 在 SQLite 阈值被实际触发前迁移 PostgreSQL。
- 没有真实场景拉动时补齐全部 18 个制造业对象。
- 在 v0.4 具备稳定真实工厂验收数据前,暂不把 `data2agent/showroom` 重命名为测试 fixture 包;在此之前它仍作为回归资产保留,不是产品运行模式。
