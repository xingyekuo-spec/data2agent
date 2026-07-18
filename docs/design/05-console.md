# 05 · 控制台与管理界面

> 状态:Jinja 管理页已实现;Vue Console v0.2 为当前主产品路线(r2,2026-07-17)
> 实现:`data2agent/console/` + `data2agent/middle_admin/`;待建:`console-ui/`
> 上层基线:[产品开发路线图](../superpowers/plans/2026-07-17-product-development-roadmap.md)
> Vue 实施规格:[console-ui design](../superpowers/specs/2026-07-15-console-ui-design.md)

## 1. 产品定位与界面分工

两类界面长期共存,不互相替代:

| 界面 | 用户与场景 | 当前状态 | 路径 |
| --- | --- | --- | --- |
| Jinja2 + HTMX 管理页 | 部署人员首次配置、连接测试、日志、故障恢复 | 已实现 | 平台 `/`;中间机 `:8851` |
| v0 内嵌运维页 | 本机简版和兼容入口 | 保留 | 平台 `/v0` |
| Vue Console | 工厂 IT 日常监控、数据验证、隔离复核、MCP Lab | v0.2 当前主路线 | 平台 `/v1` |

Jinja 管理页可以编辑经过白名单限制的部署参数并提示重启;Vue v0.2 以观察、验证和调试为主,
不开放在线生产 mapping 发布、自动 `verified`、数据删除或 ERP 写回。

现场部署形态见[便携包](../runbook/portable.md);当前受控内网技术验证见
[push-validation](../runbook/push-validation.md)。正式试点还需满足产品 v0.4 门槛。

## 2. 总体架构与路由

```text
middle_admin(:8851)                  platform console(:8849)
  Jinja 配置/状态/日志                  ├─ /      Jinja 配置/日志/调试
          │                            ├─ /v0    内嵌简版运维页
          └── connector → ingest ──────┼─ /v1    Vue Console
                                       └─ /api/* 统一管理 API
                                                    │
                                   landing/raw/obj/MCP/templates
```

原则:

- Vue 和 Jinja 复用同一 `/api/*` 领域契约,不各自复制状态计算逻辑;
- 前端不能直读 SQLite,也不能把自由文本日志解析成核心运行状态;
- `/v1` 静态资源全部随 release/便携包分发,运行时不依赖 CDN;
- Vue 缺失或构建失败不能破坏 `/`、`/v0` 和管理 API;
- 前端开发使用 Vite proxy,生产同源部署,避免额外 CORS 面。

## 3. 环境模式与状态语义

### 3.1 模式

Vue 顶栏始终显示当前模式:

| 模式 | 数据来源 | 允许用途 |
| --- | --- | --- |
| `MOCK` | 提交在前端的 typed fixtures | UI 开发、失败场景演示;不得形成验收结论 |
| `DEMO` | 展厅 SQL Server/SQLite 真实链 | 产品演示和集成验收;不等于生产安全验收 |
| `REAL` | 真实部署 API | 现场验证;是否可正式试点由 v0.4 验收报告决定 |

生产构建不得默认进入 Mock。模式必须来自部署配置/构建配置和后端 metadata,不能仅由 URL
参数伪造。

### 3.2 统一状态

管道、服务和数据集使用统一状态集合:

| 状态 | 含义 |
| --- | --- |
| `unknown` | 后端无法检测或从未采集;不得显示成正常 |
| `idle` | 已知可用但当前无任务 |
| `running` | 正在执行,返回 run ID 和当前步骤 |
| `healthy` | 最近一次执行成功且未超过 freshness 阈值 |
| `warning` | 可继续服务但存在 draft、隔离、版本差异或策略性跳过 |
| `failed` | 最近执行失败或服务不可用 |
| `stale` | raw 已更新但对象/MCP 仍服务旧数据集,或超过 freshness SLA |

API 失败不能被前端转换为空列表并继续显示 `healthy`。所有卡片显示数据来源和更新时间。

## 4. v0.2 信息架构

```text
运维监控
  ├─ 总览          /
  ├─ 管道状态      /pipeline
  ├─ 运行记录      /runs
  └─ 审计日志      /audit

数据管理
  ├─ 数据浏览      /data
  ├─ 隔离区        /quarantine
  └─ 模板          /templates

Agent
  └─ MCP Lab       /mcp

系统
  └─ 配置(只读)    /settings
```

### 4.1 总览

- ERP→抽取→推送/落地→映射→对象层→MCP 管道摘要;
- 服务健康、最近成功/失败、未处理告警;
- raw/object/隔离数量和口径说明;
- 应用、模板、binding 与 dataset version;
- 最近 5 次运行和对象分布;
- 明确显示 `unknown`、`stale` 和“使用上一稳定版本”。

### 4.2 管道与运行

每个节点显示状态、最近成功/失败、输入输出、耗时、版本、错误摘要和详情入口。

统一运行模型:

```text
Run
  id / type(sync|ingest|apply|reconcile|validation)
  status(running|ok|paused|failed|aborted)
  source / dataset_version / started_at / finished_at / duration_ms
  steps_state(available|legacy_unavailable)
  steps[]
    kind(table|object|segment|batch) / name
    rows_in / rows_out / quarantined
    watermark_before / watermark_after
    status / error_id / error
```

`validation` 从 v0.3 起复用同一 Run 模型。历史记录没有 step 证据时返回
`legacy_unavailable`,不得用空数组伪装为“实际处理 0 项”。时间均返回带时区 ISO 8601;
页面按浏览器时区展示,详情保留原始值。

### 4.3 数据浏览

- raw 表与对象层只读浏览;
- 表、对象、字段只能来自后端白名单/元模型,请求不能拼任意 SQL;
- 服务端分页、稳定排序、limit 上限和业务键搜索;
- 显示批次、抽取/映射时间、数据集版本;
- 对象敏感字段按元模型脱敏;
- raw 浏览可能含未分类的敏感原值,仅配置有效管理 Token 的授权主体可访问,访问允许/拒绝均逐次审计;
- v0.2 对能够识别的 raw 敏感列同样服务端脱敏,未知分类持续警告,不提供 unmask;
- 表格和原始 JSON 使用同一个已脱敏、已截断响应;v0.4 前仍需明确源端列裁剪和 raw 保护策略。

### 4.4 隔离与模板

- 隔离区按对象分组,显示业务键、原始值、原因、批次、时间和隔离率;
- 熔断时显示当前仍服务的稳定对象版本;
- retry 是对象级重新映射,UI 必须明确提示影响范围;
- 模板页展示对象、属性、敏感标记、binding 状态、field map、enum map、derived 和 watermark;
- v0.2 模板只读,不提供在线编辑或自动 `verified`。

### 4.5 MCP Lab

- 调用 `query_objects` 和 `query_metrics`,展示完整 JSON、脱敏字段、口径警示与 dataset version;
- 通过独立建议卡端点调用 `propose_action`,展开 evidence;
- Jinja `/api/debug/mcp-call` 继续只允许查询类工具,不因 Vue 扩大安装调试入口权限;
- v0.2 展示当前 query ID 边界;v0.3 接入主体/会话/result digest 证据契约。

## 5. API 契约

### 5.1 当前 v0 API(保持兼容)

| API | 当前语义 |
| --- | --- |
| `GET /api/overview` | 水位、对象数量、隔离概览 |
| `GET /api/runs` | 最近 `d2a_sync_run` |
| `GET /api/quarantine` | 未处理隔离 |
| `GET /api/audit` | 源 SQL 审计 |
| `POST /api/actions/sync` | 立即同步一轮 |
| `POST /api/actions/reconcile` | 本地 L1/deep;推送模式 E6b 前禁用 |
| `POST /api/actions/apply` | 重新映射全部对象 |
| `POST /api/actions/retry` | 重新映射单个对象 |

动作必须复用 connect 引擎,不能绕过窗口、白名单、只读和熔断策略。

### 5.2 v0.2 新增/标准化 API

```text
GET  /api/overview
GET  /api/pipeline
GET  /api/services
GET  /api/runs?limit&offset&type&status
GET  /api/runs/{run_id}
GET  /api/audit?limit&offset&source&action&from&to
GET  /api/audit/access?limit&offset
GET  /api/data/raw
GET  /api/data/raw/{source}/{table}?limit&offset&q
GET  /api/objects
GET  /api/objects/{object}?limit&offset&q
GET  /api/quarantine?object&limit&offset
GET  /api/templates
GET  /api/config
POST /api/debug/mcp-call                 # 只允许 query_objects/query_metrics
POST /api/gateway/proposals              # 独立建议卡入口
```

既有 `/api/runs` 和 `/api/audit` 保持数组正文兼容,分页总数通过声明的
`X-Total-Count` 响应头返回;新增浏览接口使用具名分页响应。每个端点必须定义
Pydantic request/response model。OpenAPI 快照提交到 `console-ui/openapi.json`,
由 `openapi-typescript` 生成类型;CI 从后端代码重新导出 schema 并与快照比较。

```bash
# 重新生成快照(契约变更后必须提交)
python scripts/export_console_openapi.py console-ui/openapi.json
# CI / 本地漂移检查
python scripts/export_console_openapi.py --check console-ui/openapi.json
```

M1 已将现有 `/api/**` 契约集中在 `data2agent/console/contracts.py`;成功响应最外层
wire shape(数组列表、overview/config 对象)保持兼容。历史落库时间字段仍为 legacy
local ISO text,OpenAPI 描述中标明不保证时区 offset。

### 5.3 通用动作和错误语义

| HTTP/响应 | 含义 |
| --- | --- |
| `200 executed=true` | 同步动作已经执行 |
| `200 executed=false` | 策略性跳过,例如窗口外 |
| `202 + run_id` | 已接受异步任务 |
| `401/403` | 未认证/无权限 |
| `409` | 运行冲突、只读模式、熔断、版本或 evidence 冲突 |
| `422` | 参数错误 |
| `500 + error_id` | 未处理异常,`error_id` 可关联服务日志 |

列表统一返回 `items/total/limit/offset` 或明确 cursor,不得无限加载。

## 6. Mock 与契约工作流

fixture 至少覆盖:

1. 首次安装、没有数据;
2. 全链路正常;
3. 同步运行中;
4. 推送失败;
5. apply 熔断且对象层使用旧版本;
6. 部分服务不可达;
7. 未处理隔离;
8. draft binding/指标;
9. Token 无效;
10. 未知服务错误。

Mock 与 real 使用相同 TypeScript 类型和领域组件。推荐交付顺序:

```text
response model → OpenAPI → fixture → 页面全部状态 → 真实 API → 集成/E2E
```

## 7. 安全边界

### 7.1 当前能力

- Jinja/Console 支持 Bearer Token,凭据来自 CLI、环境变量或 `config/secrets.env`;
- 默认绑定 `127.0.0.1`;
- 凭据真实值不返回浏览器;
- Vue Token 只保存在 `sessionStorage`,关闭标签页清除;
- 展厅可以匿名,但必须显示 `DEMO` 且不能接真实数据。

### 7.2 v0.4 正式试点门槛

- 生产模式 Token 强制启用,不再只是“建议”;
- console/ingest/MCP 凭据分离并支持轮换/吊销;
- 跨机管理和 API 访问强制 HTTPS;本机 `127.0.0.1` 可保留 HTTP;
- API 将凭据映射到主体,按主体限流和审计;
- raw 浏览单独授权并逐次审计;
- 安全响应不泄露其他主体的 evidence、配置或敏感原值;
- 完整多租户/RBAC 仍不属于当前试点范围。

## 8. 后续版本接口

### v0.3 可验证

```text
GET  /api/objects/{object}/{key}/lineage
POST /api/mappings/{object}/preview
GET  /api/datasets
GET  /api/datasets/{version}
POST /api/datasets/{version}/publish
POST /api/datasets/{version}/rollback
GET  /api/gateway/queries/{query_id}
GET  /api/gateway/proposals/{proposal_id}
POST /api/validation/run
GET  /api/validation/runs/{run_id}
```

### v0.4 可试点

```text
GET  /api/ingest/batches
GET  /api/ingest/batches/{batch_id}
POST /api/ingest/batches/{batch_id}/replay
POST /ingest/reconcile
GET  /api/reconcile/runs
GET  /api/reconcile/runs/{run_id}
```

## 9. 构建与部署

- 开发:`console-ui` Vite `:5173`,代理 `/api` 到 `:8849`;
- Vue 生产 base 固定 `/v1/`;
- Docker:nginx/静态服务提供 `/v1`,代理 `/api`,并将 `/`/`/v0` 保留给平台 console;
- 本机/便携包:release 构建 `dist`,随包分发并由 FastAPI 挂载 `/v1`;
- `dist` 不提交仓库,但 CI 必须执行类型检查和生产构建;
- Vue 产物缺失时 `/v1` 返回明确安装提示,不能静默回落成另一套页面;
- Jinja 与 v0 页面在 Vue 构建失败时仍可使用。

## 10. v0.2 发布门槛

- [ ] 总览、管道、运行、审计、数据、隔离、模板、MCP Lab 可用;
- [ ] 所有页面覆盖 loading/empty/running/warning/failed/unknown;
- [ ] Mock、Demo、Real 标识不可混淆;
- [ ] OpenAPI 漂移检查、TypeScript 类型检查和前端构建通过;
- [ ] SQLite 本机链与 Docker MSSQL 展厅链均通过真实 API;
- [ ] raw/object 浏览分页、白名单、脱敏/授权和审计通过;
- [ ] Jinja `/`、v0 `/v0`、Vue `/v1` 同时可用;
- [ ] 前端资产不依赖 CDN;
- [ ] 用户无需查看 SQLite 即可在 3 分钟内定位失败节点;
- [ ] 便携包包含 Vue dist,且 Vue 故障不影响应急管理页。

## 11. 运行方式

```bash
# Jinja 管理/应急入口
python -m data2agent.console --home C:\d2a --host 127.0.0.1 --port 8849
python -m data2agent.middle_admin --home C:\d2a --host 127.0.0.1 --port 8851

# 开发 / 展厅
python -m data2agent.console --config connect.example.yaml
docker compose up --build
# 当前 Jinja:http://localhost:8849;v0.2 完成后 Vue:http://localhost:8849/v1
```
