# 03 · MCP 网关

> 状态:lite 已实现;证据与试点认证目标已对齐产品路线(r1,2026-07-17)· 实现:`data2agent/mcp_server/`
> 上层基线:[产品开发路线图](../superpowers/plans/2026-07-17-product-development-roadmap.md)

## 1. 定位

Agent 侧唯一的数据入口。承诺:**任何支持 MCP 的 Agent 五分钟接入**,拿到的是业务对象和指标口径,而不是表和 SQL。

## 2. lite 现状(已实现)

三个工具,查询类全部自描述(无参调用返回目录,Agent 不需要外部文档):

| 工具 | 无参 | 有参 |
| --- | --- | --- |
| `query_objects` | 对象目录(属性/状态/敏感标记/可用源) | 等值筛选 + 排序 + limit(≤200)取数 |
| `query_metrics` | 指标目录(公式/粒度/是否已实现/可用 group_by) | 分组取数,附口径定义 |
| `propose_action` | —— | 「说」档建议卡:结论 + 依据(须引用 meta.query_id)→ 卡片 |

行为契约(违反即 bug,均有测试锁定):

1. **只读**:落地库以 `mode=ro` 打开;SQL 只由 binding 生成(`data2agent/mapping.py`),表和字段天然白名单;
2. **默认脱敏**:`sensitive` 属性一律返回 `***`,`meta.masked_fields` 声明;lite 无解敏开关;
3. **口径警示贯穿**:draft binding → `meta.note`;draft 指标 → `meta.warning`;未实现指标 → `implemented: false` + 原因,绝不静默返回空数据;
4. **源码值不出网**:枚举经 `map` 翻译为对象模型取值,筛选自动反向;传源码值(如 `result=W`)会报错并给出合法取值;
5. **错误即引导**:所有 ValueError 携带可用选项清单,让 Agent 自行纠正。

架构:`core.py`(QueryService,传输无关、可独立测试)与 `server.py`(FastMCP 封装)分离。

## 3. 治理档位:看 / 说 / 做

模板中每个 action 声明 `tier`,网关按部署配置的档位上限(`max_tier`)硬校验:

| 档位 | 含义 | 载体 | 状态 |
| --- | --- | --- | --- |
| 看 | 只读查询 | query_objects / query_metrics | ✅ |
| 说 | 生成建议,不落任何写操作 | `propose_action(object, action, conclusion, evidence)` → 建议卡 | ✅ |
| 做 | 经审批的写回 | 审批流 + 回执 | 路线(需审批治理) |

"说"档当前实现的基础约束是:每次查询响应带 `meta.query_id`,建议卡的每条依据必须引用
进程内已记录的 query_id,引用不到即拒绝;卡片同时聚合被引用查询的口径警示
(draft binding / draft 指标 / caveats),并附治理声明。当前 query 日志仍为进程级、简单序号,
也不会自动校验 claim 中每个数字是否等于查询结果,因此这里只能证明“引用过某次查询”,
不能宣称已经具备主体/会话/结果摘要级强证据。该缺口列入 v0.3。部署以 `max_tier`
设置档位上限(默认“说”),超档动作被硬拒绝并说明治理边界。

### 3.1 v0.3 查询证据契约(待建)

v0.3 将进程级简单序号升级为持久、隔离、可审计的证据对象:
v0.3 单 Token 部署可先映射为唯一部署主体;v0.4 再扩展为可轮换的多主体凭据,但请求体始终不能伪造主体。

| 字段 | 含义 |
| --- | --- |
| `principal` | 调用主体,来自认证凭据映射,不接受请求体自行声明 |
| `session_id` | MCP/Console 会话标识 |
| `query_id` | 不可预测的全局唯一 ID |
| `tool/target` | 工具和对象/指标 |
| `normalized_query` | 规范化后的筛选、排序、分组和 limit |
| `dataset_version` | 查询实际读取的数据集版本 |
| `result_digest` | 脱敏后规范化结果的摘要 |
| `result_summary` | 建议卡可展示的必要聚合/关键值,不是完整敏感快照 |
| `warnings` | draft、caveats、stale 等查询时警示 |
| `created_at/expires_at` | 证据生命周期 |

建议卡规则:

- evidence 只能引用同一 `principal` 且仍有效的 query;
- 默认要求同一 `session_id`;确需跨会话复用时必须显式授权并留下审计;
- 引用不同 `dataset_version` 的证据时拒绝或显示不可忽略的版本警示;
- 卡片固化 query ID、result digest/summary 和全部 warnings,后续数据变化不能改写旧卡证据;
- 当前不承诺从自然语言 claim 中可靠解析并逐个校验所有数字;结构化指标值的强校验按真实场景扩展。

典型错误语义:

| 条件 | 结果 |
| --- | --- |
| query ID 不存在或过期 | 拒绝建议卡并提示重新查询 |
| 主体不一致 | `403`,不透露目标证据内容 |
| 会话不一致且无授权 | `409 evidence_session_mismatch` |
| 数据集版本不一致 | `409 dataset_version_mismatch` 或显式警示策略 |
| result digest 无法核验 | 拒绝发布建议卡,记录审计事件 |

## 4. 指标实现(E4 后)

指标 SQL 注册表位于独立模块 `metrics_impl.py`,按 MetricDef.metric 路由,**只面向对象层(obj_*)取数**,与源系统表形彻底解耦(E4 清偿直读展厅表形的债务;原毛利率订单有效性过滤的最后一处 raw 穿透,已随派生状态 SalesOrder.state 决策表落地而清除,等价性由回归锚点锁定)。

## 5. HTTP 基础安全件(已实现,不等于生产就绪)

stdio(本机进程)不需要;暴露 HTTP(streamable-http)即三件齐上(`mcp_server/http.py`):

| 件 | 行为 |
| --- | --- |
| Bearer 认证 | **默认强制**:无 Token 拒绝启动,`--allow-anonymous` 仅限展厅;只认 Authorization 头,不接受 URL 参数(避免 Token 进访问日志) |
| 每工具限流 | 滑动窗口,默认 120 次/分钟,`--rate-per-minute` 可调(0 关闭) |
| 查询审计 | JSONL 追加(默认写落地库旁 `gateway_audit.jsonl`),每次工具调用一条,与抽取侧 d2a_audit_log 对称 |

当前边界:

- 单一共享 Token 不能识别真实调用主体;
- 限流按进程/工具共享,不是按主体;
- JSONL 审计没有轮换、完整性保护和主体字段;
- MCP 服务自身不终止 TLS,跨机正式试点必须经 TLS 反向代理或等价安全入口;
- `--allow-anonymous` 只允许展厅,任何真实数据环境禁止使用。

v0.4 正式试点要求:ingest/console/MCP 凭据分离,Token 可轮换/吊销,凭据映射到主体,
按主体限流和审计,跨机访问强制 HTTPS。该范围不等于多租户 RBAC。

## 6. 分阶段演进

- **查询能力**:分页游标、范围筛选(日期区间)、对象级聚合 —— 由演示链和真实 Agent 的使用反馈拉动;
- **多源**:`--source` 已支持切换;同对象多源并读(易飞+E10 并存客户)暂不做;
- **v0.3 证据隔离**:引入主体、会话、不可预测 query_id 与结果摘要,建议卡只能引用同主体/同会话证据;
- **v0.4 试点认证**:ingest / console / MCP 凭据分离,支持 Token 轮换、按主体限流与审计;
- **多租户治理**:完整租户隔离和 RBAC 仍由真实多租户需求拉动,不属于当前试点范围。
