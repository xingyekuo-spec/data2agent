# 中间机/平台机 Web 管理界面设计

## 目标

为中间机和平台机的所有服务增加 Web 管理界面，支持参数配置和调试。采用 FastAPI + Jinja2 + HTMX 方案，零前端构建，纯 Python 技术栈。

## 约束

- 中间机和平台机**各自独立**的 Web 管理界面，不互相依赖（平台机不一定能访问中间机）
- 中间机新增 `d2a-middle-admin` 服务，平台机在现有 `d2a-console` 上扩展
- **配置编辑 = 写 YAML 文件 + 提示需重启**，不做热加载和一键重启（一期不做，理由见下文）
- **凭据纪律**：界面只编辑 YAML 参数白名单，环境变量（DSN 连接串、Token）显示"已设置/未设置"不暴露真实值，不提供环境变量编辑
- **推送模式下中间机禁用对账**（跨机对账 E6b 未实现）
- Token 认证沿用 Bearer Token 机制，管理界面默认启用
- 界面仅在可信任内网段绑定（`--host` 默认 `127.0.0.1`，部署按需改为内网 IP）

## 与现有 console 路线的关系

`docs/design/05-console.md` 规定的 v1 路线为 Vue 3 + Vite + TypeScript（`console-ui/` 独立项目），面向**运维仪表盘与监控**。

本设计的管理界面面向**安装调试与参数配置**，是不同场景：

| | 运维仪表盘（console v1, 远期） | 管理界面（本设计, 当前） |
|---|---|---|
| 框架 | Vue 3 + Vite + TS | Jinja2 + HTMX（零构建） |
| 场景 | 日常监控、隔离区复核 | 部署初调、改参数、看日志排查 |
| 用户 | 工厂 IT / 实施 | 部署人员 / 开发调试 |
| 位置 | `console-ui/`（独立项目） | `middle_admin/` + `console/` 内模板 |

两者共存不冲突：
- 平台机 console 的 Jinja2 模板在访问 `/` 时渲染管理界面 HTML，JSON API（`/api/*`）同时服务于管理界面和远期 Vue 前端；
- `ui.py` 的 v0 内嵌单页**降级保留**（访问 `/v0` 或当 Jinja2 模板不可用时 fallback），Vue v1 上线后 mount 到 `/v1`；
- 当前先做 Jinja2 管理界面，Vue v1 仍按 `05-console.md` 远期推进。

## 技术选型

**FastAPI + Jinja2 模板 + HTMX**

- `jinja2` 需显式加入 extras 依赖（`console`、新建 `middle_admin`），它不是 fastapi/starlette 的强制依赖
- HTMX 14KB 单文件，vendored 到 `static/htmx.min.js`（不进 package 依赖，避免误解）
- 模板按功能模块拆分，通过 Jinja2 继承和宏复用
- HTMX 处理局部刷新和表单提交

## 架构

```
中间机（内网隔离段）                        平台机
┌─────────────────────────┐    ┌─────────────────────────────────┐
│ d2a-connector (已有)     │    │ d2a-ingest   (8850, 已有)        │
│ connect serve 调度常驻   │───▶│ d2a-apply    (定时, 已有)        │
│                         │    │ d2a-mcp      (8848, 已有)        │
│ d2a-middle-admin ✨新增  │    │ d2a-console  (8849, 已有 → 扩展) │
│ :8851 管理界面           │    │  + 配置编辑 + 日志 + 调试         │
│                         │    │                                  │
│ 配置读写 connect.yaml   │    │ 配置读写 platform.yaml          │
│ 日志/水位/连接测试      │    │ 日志聚合/服务状态/调试           │
└─────────────────────────┘    └─────────────────────────────────┘
```

## 目录结构

```
data2agent/
├── admin_templates/           # 共享模板（两台机器复用）
│   ├── layout.html            # 母版布局（顶栏导航 + 内容区）
│   └── form_macros.html       # 表单组件宏（text、number、select、window_range）
├── middle_admin/ ✨新增
│   ├── __init__.py
│   ├── __main__.py            # 入口 argparse
│   ├── app.py                 # FastAPI 应用 + API
│   ├── templates/
│   │   ├── layout.html        # 继承 admin_templates/layout.html
│   │   ├── status.html        # 调度状态 + 水位
│   │   ├── config.html        # connect.yaml 配置编辑
│   │   └── logs.html          # 日志查看
│   └── static/
│       └── htmx.min.js        # vendored
├── console/                   # 已有 → 扩展
│   ├── __init__.py
│   ├── __main__.py            # 不变
│   ├── app.py                 # 扩展：新增配置/日志/调试 API
│   ├── ui.py                  # 降级保留（/v0 fallback 简版页）
│   ├── templates/
│   │   ├── layout.html
│   │   ├── dashboard.html     # 现有仪表盘 + 动作按钮
│   │   ├── config.html        # platform.yaml 配置编辑
│   │   ├── logs.html          # 四服务日志
│   │   └── debug.html         # 调试工具
│   └── static/
│       └── htmx.min.js        # vendored
```

## 配置生效机制（一期）

**默认行为：写文件 + 提示需重启。**

当前 `connect serve` 和 `apply --every` 均在启动时读一次配置，没有 reload API。一期不做热加载，原因：

- `connect serve` 在 `scheduler.py` 启动时读 `ConnectConfig`、构建 adapter/sink、注册 APScheduler 定时任务，改 YAML 不会让已注册的 IntervalTrigger 更新；
- `apply --every` 的间隔由 CLI 参数传入，不在 `platform.yaml` 中；
- windows 上从 Web 进程重启 NSSM 服务涉及权限、命令注入、误操作风险。

一期做法：
1. 界面编辑参数 → 校验通过 → 写回 YAML 文件
2. 保存成功后页面显示"配置已写入。参数变更需**重启服务**才能生效"并给出对应的重启方式（PowerShell 命令或"打开 Windows 服务管理器"说明）
3. 保存前自动备份原文件为 `.yaml.bak`

二期可选（不在本设计范围内）：
- 为 `connect serve` 增加 SIGHUP/config-reload 端点；
- 将 `apply --every` 收进 `platform.yaml`，apply 进程读配置循环。

## 凭据与字段白名单

YAML 中涉及凭据的字段（如 `dsn_env`）仅作为**环境变量名**出现在表单中（只读文本），
对应环境变量的实际值在界面上显示"已设置 / 未设置"状态，不可编辑、不暴露真实值。

**connect.yaml 可编辑字段白名单（中间机）：**

| 字段 | 类型 | 备注 |
|------|------|------|
| `sources.<name>.windows` | 字符串列表 | 错峰窗口 |
| `sources.<name>.rate.batch_size` | 整数 | 批次大小 |
| `sources.<name>.rate.rows_per_second` | 整数 | 限流 |
| `sources.<name>.lookback` | 字符串 | 回看天数 |
| `sources.<name>.sync_every` | 字符串 | 同步间隔 |
| `sources.<name>.extra_whitelist` | 字符串列表 | 额外白名单表 |
| `sources.<name>.sink.url` | 字符串 | 平台推送地址 |

禁止编辑：`adapter`、`dsn_env`、`whitelist_from_bindings`、`sink.type`、`sink.token_env`。

**platform.yaml 可编辑字段白名单（平台机）：**

| 字段 | 类型 | 备注 |
|------|------|------|
| `templates` | 字符串 | 模板路径 |
| `landing` | 字符串 | 落地库路径 |
| `sources.<name>.reconcile_at` | 字符串 | 对账时间（仅 `sink.type=local` 时） |

因 `apply --every` 当前不经 YAML，UI 不提供编辑入口，仅显示当前 NSSM AppParameters。

## 中间机 `d2a-middle-admin`（端口 8851）

### 功能

| 功能 | 说明 |
|------|------|
| 配置编辑 | 表单编辑 connect.yaml **白名单字段**，校验后写文件，提示重启 |
| 连接测试 | 一键测试 ERP 连通性，显示耗时、可访问表列表、错误原因 |
| 调度概览 | 当前调度状态、下次同步时间、窗口内外标记、最近运行记录 |
| 水位一览 | 各表当前水位值、上次同步时间 |
| 日志查看 | d2a-connector 最近 N 行日志文本（`?lines=200`），支持级别关键词过滤 |
| 手动触发 | 立即同步（`--once` 等价，受窗口约束）。**不提供对账**（推送模式下跨机对账未实现 E6b） |

### API（HTML 页 + JSON API 统一规约）

HTML 页面（浏览器直接访问）：

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/` | 管理首页 → `/status` |
| `GET` | `/status` | 调度状态页面 |
| `GET` | `/config` | 配置编辑页面 |
| `GET` | `/logs` | 日志查看页面 |

JSON API（HTMX 调用，路径 `/api/*`）：

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/api/status` | 调度状态、水位 JSON |
| `GET` | `/api/config` | 读取 connect.yaml 返回表单初始值 JSON |
| `POST` | `/api/config` | 校验并保存 connect.yaml，返回成功/字段级错误 |
| `POST` | `/api/config/validate` | 仅校验不保存 |
| `POST` | `/api/test-connection` | 测试 ERP 连接（超时 10s），返回连通性/耗时/表列表 |
| `GET` | `/api/logs` | 日志文本 `?lines=200&level=ERROR` |
| `POST` | `/api/actions/trigger` | 手动触发同步（`body: {action: "sync"}`） |

## 平台机 `d2a-console`（端口 8849）

### 现有功能

仪表盘（水位、对象层、运行记录、隔离区、审计日志）+ 动作按钮保留在 JSON API（`/api/*`），
`ui.py` v0 内嵌单页降级保留在 `/v0`。管理界面模板替代 `/` 的默认 HTML 页面。

### 新增功能

| 板块 | 功能 |
|------|------|
| 配置编辑 | 编辑 platform.yaml **白名单字段**，校验后写文件，提示重启 |
| 服务状态 | ingest（8850 HTTP 健康检查）、mcp（8848 HTTP 健康检查）、console（自身）、apply（检查 NSSM 进程名/日志文件心跳时间戳是否存在，不作 HTTP 探测） |
| 日志查看 | 按服务选择查看最近 N 行日志文本，按级别关键词过滤 |
| 调试工具 | raw 表数据浏览器（只读 SELECT）、MCP 工具试调用（白名单限 `query_objects`/`query_metrics`，不打 `propose_action`）、ingest 健康检查 |

### 新增 API

HTML 页面：

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/` | 管理首页（dashboard 模板渲染） |
| `GET` | `/config` | 配置编辑页面 |
| `GET` | `/logs` | 日志查看页面 |
| `GET` | `/debug` | 调试工具页面 |

JSON API（`/api/*`，管理界面和远期 Vue 前端共用）：

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/api/config` | 读取 platform.yaml JSON |
| `POST` | `/api/config` | 校验并保存 platform.yaml |
| `POST` | `/api/config/validate` | 仅校验不保存 |
| `GET` | `/api/services` | 服务状态 JSON（ingest/mcp HTTP 探测，apply 进程/日志检测，console 自身） |
| `GET` | `/api/logs` | 日志文本 `?service=ingest&lines=200&level=ERROR` |
| `GET` | `/api/debug/raw-table` | 浏览 raw 表 `?table=xxx&offset=0&limit=50` |
| `POST` | `/api/debug/mcp-call` | MCP 工具试调用（白名单：`query_objects`、`query_metrics`） |

现有 API（`/api/overview`、`/api/runs`、`/api/quarantine`、`/api/audit`、`/api/actions/*`）全部保留。

## 安全

- **Token 强制**：中间机 `d2a-middle-admin` 需配 `--token`（环境变量 `D2A_MIDDLE_ADMIN_TOKEN`），空则仅绑 `127.0.0.1` 并打印警告；平台机 console 沿用 `D2A_CONSOLE_TOKEN`
- **内网绑定**：默认 `--host 127.0.0.1`，部署到内网 IP 需显式传参
- **配置写 + 触发同步权限很大**：默认仅内网可达 + Token 认证，不暴露公网
- **MCP 试调用白名单**：仅 `query_objects`、`query_metrics`（只读），不开放 `propose_action`
- **凭据不落地**：环境变量值不传给浏览器，仅返回 `set: true/false`
- **日志读取**：仅读文件尾部 N 行（`?lines=` 上限 1000），防止内存溢出

## 错误处理

- 配置校验失败 → 返回 `{ok: false, errors: [{field, message}]}`，表单字段旁显示红色错误提示
  - 一期：对 `load_config()` 的 `ValueError` 做字符串解析，提取字段名；若无法精确定位则标记为整表错误
  - 二期可选：改造校验层返回结构化错误，实现真正的字段级定位
- 连接测试超时 10 秒 → 返回 `{ok: false, error: "超时", detail: "..."}  `
- 日志文件不存在/无权限 → 返回明确提示文本，不报 500
- Token 错误 → 返回 401，HTML 页显示登录表单（弹出 Token 输入框）

## 部署与打包

### extras 与依赖

`pyproject.toml` 变更：

- `console` extra 加 `jinja2>=3.0`（模板渲染）
- 新增 `middle_admin` extra：`fastapi>=0.110`, `uvicorn>=0.29`, `jinja2>=3.0`
  - 中间机当前仅装 `.[connect]`，不含 FastAPI；管理界面需额外装此 extra
  - 依赖 group 选 `middle_admin` 而非复用 `console`，保持中间机/平台机依赖精确、不引入不必要的包

### package_data

`pyproject.toml` 需配置 `[tool.setuptools.package-data]` 包含模板和静态文件：

```
[tool.setuptools.package-data]
data2agent = ["admin_templates/*.html", "middle_admin/templates/*.html",
              "middle_admin/static/*.js", "console/templates/*.html",
              "console/static/*.js"]
```

### 部署脚本变更

`deploy/setup-middle.ps1`：
- 新增设备提示：管理界面端口 8851、Token 环境变量 `D2A_MIDDLE_ADMIN_TOKEN`
- NSSM AppParameters 示例：`python -m data2agent.middle_admin --config C:\d2a\config\connect.yaml --host 0.0.0.0 --port 8851`
- 防火墙规则提示 8851（仅内网）

`deploy/setup-platform.ps1`：
- console 原有部署步骤不变（NSSM AppParameters 已有），仅新增说明管理界面入口

`docs/runbook/install-middle.md`：
- 依赖安装从 `.[connect]` 改为 `.[connect, middle_admin]`
- 加管理界面验证步骤

### 离线包（release CI）

`release.yml` 构建离线 wheel 时需确保 `jinja2` 及其传递依赖（`markupsafe`）被 bundle 进离线包。如现有离线打包流程不处理 extras 传递依赖，需修正。

## 测试

- API 层测试（pytest + TestClient）：配置读写/校验/保存、连接测试、状态查询、日志查询
- 表单校验测试：合法值通过、非法值拒绝、边界值处理
- 渲染冒烟测试：每个 HTML 页面 GET 返回 200 且包含关键元素（导航栏、表单字段），不做精确快照对比（时间戳/水位易碎）
- 配置保存回滚测试：写入无效配置 → 确认原文件不变、`.yaml.bak` 存在

## 变更文件清单

| 变更 | 位置 |
|------|------|
| 新增 `d2a-middle-admin` 服务 | `data2agent/middle_admin/` |
| 共享 Jinja2 模板 | `data2agent/admin_templates/` |
| console 新增管理界面模板 | `data2agent/console/templates/` |
| console API 扩展 | `data2agent/console/app.py` |
| `ui.py` 降级保留（`/v0` fallback）| `data2agent/console/ui.py` |
| extras 加 `jinja2`、新增 `middle_admin` | `pyproject.toml` |
| package_data 包含模板和静态文件 | `pyproject.toml` |
| 中间机部署脚本更新（端口 8851 + token + 防火墙 + extra） | `deploy/setup-middle.ps1` |
| 中间机安装文档更新 | `docs/runbook/install-middle.md` |
| 平台机部署脚本更新（说明管理界面入口） | `deploy/setup-platform.ps1` |
| 离线包 CI 确认 jinja2/markupsafe 被打包 | `.github/workflows/release.yml` |
