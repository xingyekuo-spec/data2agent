# 外部 Agent HTTP MCP 对接指南

本文用于指导外部 Agent 通过 HTTP MCP 方式接入 data2agent 数据平台。

适用链路:

```text
ERP -> 中间服务器 -> 数据平台 -> MCP HTTP -> 外部 Agent
```

外部 Agent 不直接访问 `factory.sqlite`、`raw_*` 或 `objv_*` 表,只通过 MCP 工具读取当前已发布数据集。

## 1. 对接前提

| # | 确认项 | 说明 |
| --- | --- | --- |
| 1 | 数据平台已启动 | 平台机进程正常运行 |
| 2 | MCP HTTP 已启动 | 默认端口 `8848` |
| 3 | 外部 Agent 可访问平台机 | 能访问 `http://<平台IP>:8848/mcp` 或 HTTPS 网关地址 |
| 4 | 已配置 MCP Token | HTTP MCP 默认要求 Bearer Token |
| 5 | ERP 数据已同步 | `raw_*` 表已有数据 |
| 6 | 数据已构建 | YAML 对象模板已生成对象版本数据 |
| 7 | 数据集已发布 | MCP 只读取已发布数据集 |

如果数据只同步到 `raw_*`,但尚未构建或发布,外部 Agent 不能把它当作可用业务数据。

## 2. 平台侧启动 MCP HTTP

源码运行方式:

```bash
D2A_MCP_TOKEN=<MCP_TOKEN> python -m data2agent.mcp_server \
  --transport http \
  --db landing/factory.sqlite \
  --templates templates \
  --source digiwin_e10 \
  --host 0.0.0.0 \
  --port 8848
```

参数说明:

| 参数 | 说明 |
| --- | --- |
| `--transport http` | 使用 HTTP MCP 服务 |
| `--db` | 数据平台落地库,默认 `landing/factory.sqlite` |
| `--templates` | YAML 模板目录 |
| `--source` | 数据源名,需要与构建/发布的数据源一致 |
| `--host` | 监听地址。跨机器访问通常使用 `0.0.0.0` |
| `--port` | MCP HTTP 端口,默认 `8848` |
| `D2A_MCP_TOKEN` / `--token` | 外部 Agent 调用时使用的 Bearer Token |

便携包部署时,平台机默认提供 MCP HTTP 端口:

```text
8848
```

MCP Token 在平台首次配置 `/setup` 中填写或生成。

## 3. 外部 Agent 连接信息

MCP URL:

```text
http://<平台IP>:8848/mcp
```

认证头:

```text
Authorization: Bearer <MCP_TOKEN>
```

外部 Agent 的 MCP 配置通常类似:

```json
{
  "mcpServers": {
    "data2agent": {
      "url": "http://<平台IP>:8848/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_TOKEN>"
      }
    }
  }
}
```

如果现场通过 HTTPS 网关暴露 MCP,则把 URL 改为:

```text
https://<平台域名>/mcp
```

并保持 Bearer Token 不变。

## 4. MCP 工具清单

外部 Agent 连接成功后,应能看到以下工具:

| 工具 | 用途 | 是否写 ERP |
| --- | --- | --- |
| `query_objects` | 查询业务对象数据或对象目录 | 否 |
| `query_metrics` | 查询经营指标或指标目录 | 否 |
| `propose_action` | 基于前序查询生成结构化建议卡 | 否 |

当前 MCP 是只读数据网关。`propose_action` 只生成建议,不执行 ERP 写入、审批或状态变更。

## 5. `query_objects`

### 5.1 查询对象目录

不传 `object` 时返回当前 MCP 可见的对象目录:

```json
{}
```

外部 Agent 应先调用目录查询,确认可用对象、字段、状态和动作。

### 5.2 查询对象数据

示例:

```json
{
  "object": "Material",
  "filters": {
    "material_code": "A001"
  },
  "limit": 20
}
```

参数:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `object` | string | 对象名,例如 `Material`、`Customer`、`SalesOrder` |
| `filters` | object | 等值筛选,键为对象属性名 |
| `order_by` | string | 排序字段 |
| `desc` | boolean | 是否倒序 |
| `limit` | number | 返回行数,默认 `20`,上限 `200` |

返回结果会包含 `meta.query_id` 和 `meta.result_digest`。如果后续要调用 `propose_action`,必须保存这两个值。

## 6. `query_metrics`

### 6.1 查询指标目录

不传 `metric` 时返回指标目录:

```json
{}
```

### 6.2 查询指标数据

示例:

```json
{
  "metric": "dead_stock_amount",
  "group_by": "warehouse",
  "limit": 20
}
```

参数:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `metric` | string | 指标名 |
| `group_by` | string | 分组维度 |
| `limit` | number | 返回行数,默认 `24` |

指标返回会附带口径、状态和 caveats。外部 Agent 在生成结论时应保留口径说明,尤其是 `draft` 状态的指标。

## 7. `propose_action`

`propose_action` 用于生成结构化建议卡。它不会写 ERP,也不会执行动作。

调用前提:

1. 先调用 `query_objects` 或 `query_metrics` 获取数据。
2. 从查询结果中取出:
   - `meta.query_id`
   - `meta.result_digest`
3. 在建议卡 evidence 中引用这些值。

示例:

```json
{
  "object": "DeadStockItem",
  "action": "suggest_disposal",
  "conclusion": "建议优先处理超过180天未动销且库存金额较高的物料。",
  "evidence": [
    {
      "claim": "该物料长期未动销且占用金额较高。",
      "query_id": "<前序查询返回的 meta.query_id>",
      "result_digest": "<前序查询返回的 meta.result_digest>"
    }
  ]
}
```

参数:

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `object` | string | 建议所属对象 |
| `action` | string | 对象模板中声明的动作 |
| `conclusion` | string | 明确的人类可读结论 |
| `evidence` | array | 依据列表,必须引用真实查询 |

如果 `query_id` 或 `result_digest` 不存在、不匹配、过期或来自其他会话,调用会被拒绝。

## 8. Agent 推荐调用流程

```text
1. 连接 MCP HTTP
2. list tools
3. 调用 query_objects({}) 获取对象目录
4. 根据用户问题选择对象和字段
5. 调用 query_objects 或 query_metrics 查询数据
6. 向用户回答只读事实
7. 如需建议,调用 propose_action 生成建议卡
8. 在最终回答中保留口径、数据版本或 evidence 信息
```

Agent 不应绕过 MCP 直接访问数据库,也不应臆造不存在的对象、字段或指标。

## 9. 数据可见性规则

MCP 读取的是当前已发布数据集。

```text
raw_* 有数据,但未构建      -> MCP 不可用
已构建,但未发布           -> MCP 不使用该候选版本
已发布                    -> MCP 可查询
今天 raw 有新数据未构建    -> MCP 仍读取上一已发布版本
```

因此外部 Agent 的回答应理解为:

```text
基于当前已发布的数据集
```

而不是直接等同于 ERP 当前最新状态。

## 10. 安全与审计

| 项 | 说明 |
| --- | --- |
| 认证 | HTTP MCP 默认要求 `Authorization: Bearer <MCP_TOKEN>` |
| 限流 | 默认每工具 `120` 次/分钟,可通过 `--rate-per-minute` 调整 |
| 脱敏 | 联系方式、成本等敏感字段按规则脱敏 |
| 审计 | HTTP 模式默认在落地库旁写入 `gateway_audit.jsonl` |
| 证据 | 查询 evidence 持久化到 `d2a_gateway_query_evidence` 等表 |
| 动作边界 | 当前只读,不写 ERP |

生产或真实试点环境不建议使用 `--allow-anonymous`。

## 11. 常见错误

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `401 unauthorized` | Token 缺失或错误 | 检查 Bearer Token |
| `unknown_target` | 对象或指标不存在 | 先调用目录查询 |
| `not_published` | 当前来源没有已发布数据集 | 在控制台完成构建并发布 |
| `not_materialized` | 对象尚未构建 | 检查 raw 数据、YAML 映射和隔离区 |
| `invalid_params` | 参数形状错误 | 检查 filters、limit、对象名 |
| `rate_limited` | 调用超过限流 | 降低频率或调整 `--rate-per-minute` |
| `evidence_*` | 建议卡引用的查询证据无效 | 使用同一会话内真实查询返回的 `query_id` 和 `result_digest` |

## 12. 对接验收清单

| # | 验收项 | 期望 |
| --- | --- | --- |
| 1 | 外部 Agent 能连接 MCP URL | 连接成功 |
| 2 | 外部 Agent 能列出工具 | 看到 `query_objects`、`query_metrics`、`propose_action` |
| 3 | 调用 `query_objects({})` | 返回对象目录 |
| 4 | 查询一个已发布对象 | 返回数据行和 `meta.query_id` |
| 5 | 查询一个指标 | 返回指标结果或明确错误 |
| 6 | 使用前序查询调用 `propose_action` | 返回建议卡 |
| 7 | 使用错误 Token 调用 | 返回 `401` |
| 8 | 控制台审计/日志可见 | 能追踪 MCP 查询记录 |

完成以上检查后,外部 Agent 即可进入业务提示词和场景验证阶段。
