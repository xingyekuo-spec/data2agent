# 中间机/平台机 Web 管理界面设计

## 目标

为中间机和平台机的所有服务增加 Web 管理界面，支持参数配置和调试。采用 FastAPI + Jinja2 + HTMX 方案，零前端构建，纯 Python 技术栈。

## 约束

- 中间机和平台机**各自独立**的 Web 管理界面，不互相依赖（平台机不一定能访问中间机）
- 中间机新增 `d2a-middle-admin` 服务，平台机扩展现有 `d2a-console`
- 全量参数配置 + 基础调试能力
- 配置保存即生效（热加载），少数需重启的参数提示用户
- 延续现有 Bearer Token 认证机制

## 技术选型

**方案 C：FastAPI + Jinja2 模板 + HTMX**

- Jinja2 已是 FastAPI 依赖，无需额外安装
- HTMX 14KB 单文件，直接内嵌到 static 目录
- 模板按功能模块拆分（配置、日志、状态），通过 Jinja2 继承和宏复用
- HTMX 处理局部刷新和表单提交，替代现有 `setInterval` 全量轮询

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
│   ├── layout.html            # 母版布局
│   ├── form_macros.html       # 表单组件宏
│   └── status_card.html       # 状态卡片宏
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
│       └── htmx.min.js
├── console/                   # 已有 → 扩展
│   ├── __init__.py
│   ├── __main__.py            # 不变
│   ├── app.py                 # 扩展：新增配置/日志/调试 API
│   ├── ui.py                  # 删除，被 templates/ 替代
│   ├── templates/
│   │   ├── layout.html
│   │   ├── dashboard.html     # 现有仪表盘 + 动作按钮
│   │   ├── config.html        # platform.yaml 配置编辑
│   │   ├── logs.html          # 四服务日志聚合
│   │   └── debug.html         # 调试工具
│   └── static/
│       └── htmx.min.js
```

## 中间机 `d2a-middle-admin`（端口 8851）

### 功能

| 功能 | 说明 |
|------|------|
| 配置编辑 | 表单编辑 connect.yaml 全参数（数据源、错峰窗口、限流、回看天数、同步间隔、推送目标），保存校验 + 热加载 |
| 连接测试 | 一键测试 ERP 连通性，显示耗时和表列表 |
| 调度概览 | 当前调度状态、下次同步时间、窗口内外、最近运行记录 |
| 水位一览 | 各表当前水位值、上次同步时间 |
| 日志查看 | d2a-connector 结构化日志，支持按级别过滤 |
| 手动触发 | 立即同步/对账（受窗口约束，窗口外提示） |

### API

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/` | 管理首页 → `/status` |
| `GET` | `/status` | 调度状态、水位、最近运行 |
| `GET` | `/config` | 读取当前 connect.yaml 为表单数据 |
| `POST` | `/config` | 保存 connect.yaml（校验通过后写入） |
| `POST` | `/config/validate` | 仅校验不保存 |
| `POST` | `/test-connection` | 测试 ERP 连接 |
| `GET` | `/logs` | 日志查询 `?level=INFO&limit=100` |
| `POST` | `/actions/trigger` | 手动触发同步/对账 |

## 平台机 `d2a-console`（端口 8849）

### 现有功能迁移

仪表盘（水位、对象层、运行记录、隔离区、审计日志）+ 动作按钮从内嵌 HTML 迁移到 Jinja2 模板，HTMX 局部自动刷新替代 5 秒全量轮询。

### 新增功能

| 板块 | 功能 |
|------|------|
| 配置编辑 | 编辑 platform.yaml（模板路径、落地库路径、apply 间隔等），表单校验 + 热加载 |
| 服务状态 | 四服务健康概览卡片（ingest 8850、apply、mcp 8848、console 8849） |
| 日志查看 | 聚合查看四个服务日志，按服务/级别/时间过滤，关键词搜索 |
| 调试工具 | raw 表浏览器、MCP 工具试调用、ingest 健康检查 |

### 新增 API

| 方法 | 路径 | 用途 |
|------|------|------|
| `GET` | `/api/config` | 读取 platform.yaml |
| `POST` | `/api/config` | 保存 platform.yaml |
| `POST` | `/api/config/validate` | 校验不保存 |
| `GET` | `/api/services` | 四服务健康状态 |
| `GET` | `/api/logs` | 聚合日志 `?service=ingest&level=WARNING&search=` |
| `GET` | `/api/debug/raw-table` | 浏览 raw 表 `?table=xxx&limit=50` |
| `POST` | `/api/debug/mcp-call` | 试调用 MCP 工具 |

现有 API（`/api/overview`、`/api/runs`、`/api/quarantine`、`/api/audit`、`/api/actions/*`）全部保留。

## 配置热加载

- YAML 参数：API 写回文件，调用服务内部 reload 方法实时生效
- 环境变量参数（DSN、Token）：界面弹出提示"需重启服务"，提供一键重启按钮
- 校验：保存前调用 `load_config()` 完整校验，失败返回字段级错误不写入
- 回滚：保存前自动备份 `.yaml.bak`，热加载失败时回滚

## 错误处理

- 配置校验失败 → 字段级错误列表，表单对应字段标红
- 连接测试超时 10 秒 → 返回具体错误（网络不可达、认证失败、表权限不足）
- 热加载失败 → 回滚配置到 `.yaml.bak`，返回错误信息
- 日志文件不存在/无权限 → 明确提示，不报 500
- Token 认证 → 沿用 Bearer Token 机制，与环境变量一致

## 测试

- API 层单元测试（pytest + TestClient）：config 读写、校验、连接测试
- 模板快照测试：每个页面渲染结果对比
- 配置回滚测试：无效配置保存 → 确认未写入且备份存在

## 变更文件清单

| 变更 | 位置 |
|------|------|
| 新增 `d2a-middle-admin` 服务 | `data2agent/middle_admin/` |
| 共享 Jinja2 模板 | `data2agent/admin_templates/` |
| console UI 重写（内嵌字符串 → 模板） | `data2agent/console/ui.py` → `data2agent/console/templates/` |
| console API 扩展 | `data2agent/console/app.py` |
| 部署脚本更新（中间机加 8851 端口说明） | `deploy/setup-middle.ps1` |
| 依赖说明（HTMX 静态文件） | `pyproject.toml` |
