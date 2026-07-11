# 03 · MCP 网关

> 状态:lite 已实现(2026-07-10)· 实现:`data2agent/mcp_server/` · 本文记录现状契约与治理档位演进

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
| 做 | 经审批的写回 | 审批流 + 回执 | 商业版(BizMind)范围 |

"说"档的实现兑现了设计约束:每次查询的响应带 `meta.query_id`(网关持会话内查询日志);建议卡的每条依据必须引用真实 query_id,引用不到即拒绝 —— **Agent 无法在卡里塞入没有出处的数字**。卡片聚合被引用查询的全部口径警示(draft binding / draft 指标 / caveats),附治理声明。部署以 `max_tier` 设档位上限(默认 说),超档动作被硬拒绝并说明治理边界。

## 4. 指标实现(E4 后)

指标 SQL 注册表位于独立模块 `metrics_impl.py`,按 MetricDef.metric 路由,**面向对象层(obj_*)取数**,与源系统表形解耦(E4 前直读展厅表形的过渡债务已清偿)。残留一处显式 raw 穿透:毛利率的订单有效性过滤(INVALID_STATE / APPROVE_DATE)—— 对象层尚无派生状态属性(状态推导属映射层扩展,见 docs 01 §3.1),补齐后删除。

## 5. HTTP 部署安全件(已实现)

stdio(本机进程)不需要;暴露 HTTP(streamable-http)即三件齐上(`mcp_server/http.py`):

| 件 | 行为 |
| --- | --- |
| Bearer 认证 | **默认强制**:无 Token 拒绝启动,`--allow-anonymous` 仅限展厅;只认 Authorization 头,不接受 URL 参数(避免 Token 进访问日志) |
| 每工具限流 | 滑动窗口,默认 120 次/分钟,`--rate-per-minute` 可调(0 关闭) |
| 查询审计 | JSONL 追加(默认写落地库旁 `gateway_audit.jsonl`),每次工具调用一条,与抽取侧 d2a_audit_log 对称 |

## 6. 演进(按需拉动,不提前建)

- **查询能力**:分页游标、范围筛选(日期区间)、对象级聚合 —— 由演示链和真实 Agent 的使用反馈拉动;
- **多源**:`--source` 已支持切换;同对象多源并读(易飞+E10 并存客户)暂不做;
- **认证升级**:多 Token / 按 Token 记审计主体(who),真实多租户需求出现时做。
