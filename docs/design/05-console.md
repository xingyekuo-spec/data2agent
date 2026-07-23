# 05 · 控制台与管理界面

> 状态:平台 Vue Console 已成为唯一平台管理入口;中间机保留轻量 `middle_admin` 本机配置入口(r5,2026-07-22)
> 实现:`data2agent/console/` + `console-ui/` + `data2agent/middle_admin/`
> 上层基线:[路线图](../roadmap.md)

## 1. 界面分工

| 界面 | 部署位置 | 路径 | 用途 |
| --- | --- | --- | --- |
| Vue Console | 数据平台 `:8849` | `/v1/` | 首次配置、配置编辑、日志、监控、数据验证、MCP Lab、字段血缘、一键验收 |
| middle_admin | 中间服务器 `:8851` | `/` `/config` `/logs` | 中间机 ERP 连接、平台 URL、connector 状态与日志 |

平台端不再提供 Jinja 管理页和 v0 内嵌页。旧平台路径会重定向:

| 旧路径 | 新位置 |
| --- | --- |
| `/` | `/v1/` 或首次配置时 `/v1/setup` |
| `/config` | `/v1/settings` 或首次配置时 `/v1/setup` |
| `/logs` | `/v1/logs` |
| `/debug` | `/v1/mcp` |
| `/v0` | `/v1/` |

## 2. 平台路由

```text
platform console(:8849)
  ├─ /v1/          Vue Console
  ├─ /v1/setup    首次配置
  ├─ /v1/settings 配置编辑
  ├─ /v1/logs     日志
  └─ /api/*       统一管理 API
```

Vue 生产 base 固定为 `/v1/`;便携包必须包含 `app/console-ui/dist`。

## 3. Vue Console 页面

```text
运维监控
  ├─ 仪表盘        /
  ├─ 管道状态      /pipeline
  ├─ 运行记录      /runs
  ├─ 验收报告      /validation
  └─ 审计日志      /audit

数据管理
  ├─ 数据浏览      /data
  ├─ 隔离区        /quarantine
  └─ 模板          /templates

Agent
  └─ MCP Lab       /mcp

系统
  ├─ 配置          /settings
  └─ 日志          /logs

隐藏入口
  └─ 首次配置      /setup
```

## 4. 管理 API

平台 Vue Console 消费同源 `/api/*`:

```text
GET  /api/setup/status
POST /api/setup
GET  /api/config
POST /api/config
POST /api/config/validate
GET  /api/logs
GET  /api/overview
GET  /api/pipeline
GET  /api/services
GET  /api/runs
GET  /api/audit
GET  /api/audit/access
GET  /api/data/raw
GET  /api/objects
GET  /api/quarantine
GET  /api/templates
POST /api/debug/mcp-call
POST /api/gateway/proposals
GET  /api/objects/{object}/{key}/lineage
POST /api/mappings/{object}/preview
GET  /api/datasets
POST /api/validation/run
GET  /api/validation/runs/{run_id}
```

API request/response model 集中在 `data2agent/console/contracts.py`。OpenAPI 快照提交到
`console-ui/openapi.json`,由 `openapi-typescript` 生成类型:

```bash
python scripts/export_console_openapi.py console-ui/openapi.json
python scripts/export_console_openapi.py --check console-ui/openapi.json
```

## 5. 安全与凭据

- Console Token、ingest Token、MCP Token 写入 `config/secrets.env`;
- Vue Token 只保存在 `sessionStorage`;
- raw 浏览、隔离详情、mapping preview 和字段血缘强制 Bearer;
- 首次配置接口仅允许本机访问;
- 敏感 Token 不通过配置读取接口返回浏览器。

## 6. 构建与发布

- 开发:`console-ui` Vite,代理 `/api` 到平台 `:8849`;
- CI 执行 lint、typecheck、test、build 和 dist 检查;
- release workflow 在组装平台便携包前执行 `npm ci && npm run build`;
- `deploy/build_portable.ps1 -Role platform` 要求 `console-ui/dist/index.html` 存在,并复制到
  `app/console-ui/dist`;
- Vue dist 缺失时 `/v1/` 返回明确错误页,不回落到另一套平台管理页面。

## 7. 中间机管理界面(middle_admin)

middle_admin(`:8851`)提供中间服务器本机配置入口,核心功能:

### 7.1 表策略编辑器

配置页(`/config`)提供表格化的抽取表管理:

- 每张表显示 `mode`(incremental / full_refresh)和 `watermark` 列名;
- 支持添加、删除、编辑表条目;
- `mode: full_refresh` 的表不显示 watermark 字段;
- 编辑 buffer 保存时原子替换 `connect.yaml` 的 `tables` 段落,避免部分写入。

### 7.2 连接测试增强

连接测试按钮现在额外验证:

- 每张配置表的 **主键(PK)列** 是否存在;
- 增量模式表的 **watermark 列** 是否存在;
- 测试结果逐表列出 OK/失败原因。

### 7.3 与平台模板的关系

中间机的 `tables` 配置控制"从 ERP 抽哪些表",平台模板控制"raw 字段如何映射到业务对象"。两者独立维护:

- 中间机新增表 → 数据落入 raw 层 → 平台侧可在模板中映射新表的字段;
- 中间机删除表 → 该表不再参与后续抽取,已落地的 raw 数据保留;
- 模板的 binding 字段映射使用表中的物理字段名,不感知抽取策略。

## 8. 运行方式

现场运行使用便携包:

```text
d2a-portable-platform-<版本>.zip → 解压 → 双击 data2agent.exe → 打开 /v1/setup
d2a-portable-middle-<版本>.zip   → 解压 → 双击 data2agent.exe → 打开中间机管理界面
```
