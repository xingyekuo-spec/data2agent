# 05 · 控制台与管理界面

> 状态:平台 Vue Console 已成为唯一平台管理入口;中间机保留轻量 `middle_admin` 本机配置入口(r5,2026-07-22)
> 实现:`data2agent/console/` + `console-ui/` + `data2agent/middle_admin/`
> 上层基线:[路线图](../roadmap.md)

## 1. 界面分工

| 界面 | 部署位置 | 路径 | 用途 |
| --- | --- | --- | --- |
| Vue Console | 数据平台 `:8849` | `/` | 首次配置、配置编辑、日志、监控、数据验证、MCP Lab、字段血缘、一键验收 |
| middle_admin | 中间服务器 `:8851` | `/` `/config` `/logs` | 中间机 ERP 连接、平台 URL、connector 状态与日志 |

平台端不再提供 Jinja 管理页和 v0 内嵌页。旧平台路径会重定向:

| 旧路径 | 新位置 |
| --- | --- |
| `/config` | `/settings` 或首次配置时 `/setup` |
| `/debug` | `/mcp` |
| `/v0` | `/` |
| `/v1/*` | 保留兼容跳转到对应根路径 |

## 2. 平台路由

```text
platform console(:8849)
  ├─ /             Vue Console
  ├─ /setup        首次配置
  ├─ /settings     配置编辑
  ├─ /logs         日志
  └─ /api/*       统一管理 API
```

Vue 生产 base 固定为 `/`;便携包必须包含 `app/console-ui/dist`。

## 3. Vue Console 页面

导航按数据生命周期分六模块(2026-08 重构,与后端模块边界一一对应):

```text
总览
  └─ 仪表盘        /

数据源
  ├─ 数据源管理    /sources
  ├─ 管道状态      /pipeline
  ├─ 运行记录      /runs
  └─ 隔离区        /quarantine

数据管理
  └─ 数据浏览      /data

本体库
  ├─ 模板          /templates
  └─ 对象关系      /object-graph

MCP 服务
  ├─ 呆滞验证      /dead-stock
  └─ MCP Lab       /mcp

平台管理
  ├─ 配置          /settings
  ├─ 日志          /logs
  ├─ 审计日志      /audit
  └─ 验收报告      /validation

隐藏入口
  └─ 首次配置      /setup
```

### 3.1 页面类型

每页必须归属四类之一,结构按类型约束:

| 类型 | 适用 | 结构 |
| --- | --- | --- |
| A. 列表管理页 | 运行记录、隔离区、数据浏览、审计日志、模板 | §3.2 标准三段式 |
| B. 监控可视化页 | 仪表盘、管道状态、对象关系 | 状态卡 → 可视化区 → 详情区 |
| C. 表单向导页 | 首次配置、配置 | 卡片分组表单 + 底部操作条 |
| D. 工作台页 | 数据源管理、MCP Lab、呆滞验证 | 自定义分区 |

### 3.2 A 类列表页标准结构(以运行记录页为基准实现)

```text
顶栏(页面标题,由 AppLayout 统一展示,页内不重复 H1)
├──────────────────────────────────────────┤  ← 零间距、与顶栏同宽
│ [搜索/筛选…]                   [主操作] │  ← 工具栏卡片(通栏白卡)
└──────────────────────────────────────────┘
   ┌────────────────────────────────────┐
   │ 表格(标识列|业务列|状态列|操作列) │  ← 表格卡片(内容边距)
   │              共 N 条 [每页条数] [页码]│  ← 分页栏,表格卡内右下
   └────────────────────────────────────┘
```

强制规则:

1. **工具栏**:`<div class="d2a-card d2a-toolbar">`,通栏贴顶栏(配合页面根
   节点 `d2a-page-flush`);左侧搜索框(220px)+ 筛选下拉(140px,EP 新版
   select 默认 100% 宽必须约束),右侧 `d2a-toolbar__actions` 放操作按钮,
   主操作在最右;
2. **分页栏**:统一 `Pager` 组件(`components/shared/Pager.vue`),
   「共 N 条 + 20/50/100 条选择 + 页码」三件套右对齐,事件为
   `change(offset, limit)`,换条数自动回第一页;
3. **表格**:状态列用彩色语义标签;操作列固定最右、文字链接式,危险操作
   红色 + 二次确认;空值显示 `—`;行点击开详情;
4. **三态必备**:加载 `LoadingState` / 失败 `ErrorState`(可重试)/ 空数据
   `EmptyState`(带引导文案);
5. **详情用右侧抽屉**(el-drawer),不页内跳转;
6. **文案**:辅助说明 12px 灰字;术语用专业口径(水位/隔离区等),不白话化;
7. **响应式**:≤900px 工具栏折行、表格横向滚动;
8. **测试**:每页视图测试覆盖三态 + 关键交互;挂载 ElementPlus 须带
   `{ locale: zhCn }`(与生产 el-config-provider 对齐,否则分页等文案为英文)。

共享资产(新增 A 类页面直接用):`.d2a-page-flush` / `.d2a-toolbar` /
`.d2a-toolbar__actions`(tokens.css)、`Pager.vue`、三态组件。

历史页面以此规范为准逐步改造;新页面必须从规范起步。

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
- Vue dist 缺失时 `/` 返回明确错误页,不回落到另一套平台管理页面。

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
d2a-portable-platform-<版本>.zip → 解压 → 双击 data2agent.exe → 打开 /setup
d2a-portable-middle-<版本>.zip   → 解压 → 双击 data2agent.exe → 打开中间机管理界面
```
