# console-ui · 运维控制台前端重设计

> 状态: 设计完成 · 2026-07-15 · 消费者: 工厂 IT / 实施伙伴

## 1. 背景与动机

v0 运维控制台 (`data2agent/console/ui.py`) 是一个嵌入 Python 源码的 HTML 字符串，原生
JS + 内联 CSS。5 个对象 / 1 个源时勉强可用，扩展到 18 个对象 / 多源后不可维护。

**决策(2026-07-15)**: 拆为独立前端项目 `console-ui/`，Vue 3 + Vite + TypeScript。

## 2. 技术栈

| 层 | 选择 | 理由 |
|----|------|------|
| 框架 | Vue 3 (Composition API) | 生态成熟，团队熟悉 |
| 构建 | Vite | 快速 HMR，标准 SPA 构建 |
| 语言 | TypeScript | 类型安全，OpenAPI 类型对接 |
| UI 组件库 | Element Plus | 国内社区最活跃的 Vue 3 组件库 |
| 状态管理 | Pinia | Vue 3 标准，devtools 支持 |
| 路由 | Vue Router (history 模式) | 干净 URL，需后端 SPA fallback |
| 图表 | ECharts (vendored) | 不依赖 CDN，内网部署友好 |
| API 客户端 | openapi-typescript | FastAPI 有 OpenAPI schema，自动生成类型 |

**硬性约束**:
- 静态资源全部打进 dist，不依赖外部 CDN
- Docker 多阶段构建产 nginx 镜像
- 仓库不进 node 工具链 (`.gitignore` dist 以外的构建产物)

## 3. 布局与导航

### 3.1 顶栏

- 深色底 (`#001529`)，高度 48px
- 左侧: logo 文字 "data2agent 运维控制台"
- 右侧: 源连接状态 / 上次抽取时间 / 隔离待处理数 / 模式标识
- 无水平菜单

### 3.2 侧边栏

- 白色底，宽度 210px
- 两级菜单结构，一级分组标题 + 二级页面项
- 选中项: 蓝色高亮 (`#1677FF`) + 右侧竖线

### 3.3 菜单结构

```
运维监控 (分组标题)
  ├─ 仪表盘     /
  ├─ 抽取状态   /sync
  └─ 审计日志   /audit

数据管理
  ├─ 对象层     /objects
  ├─ 隔离区     /quarantine
  └─ 模板       /templates (占位)

系统
  └─ 配置       /settings (占位)
```

### 3.4 配色

| 元素 | 颜色 |
|------|------|
| 顶栏背景 | `#001529` |
| 主强调色 | `#1677FF` |
| 成功/正常 | `#52c41a` |
| 危险/告警 | `#cf1322` |
| 警告/暂停 | `#faad14` |
| 内容区背景 | `#f5f7fa` |
| 卡片背景 | `#ffffff` |
| 文字主色 | `#001529` |
| 文字辅色 | `#999999` |

## 4. 页面设计

### 4.1 仪表盘 (Dashboard)

默认首页。4 个统计卡片 + 抽取趋势图 + 对象分布条 + 最近运行表。

- **统计卡片**: 已抽取总行数 / 对象层覆盖率 / 隔离区待处理 / 上次运行时间
- **抽取趋势**: ECharts 柱状图，最近 24 小时增量行数
- **对象分布**: 水平数据条，各对象行数占比
- **最近运行**: 仅展示最近 5 条，更多通过"抽取状态"视图查看

### 4.2 抽取状态 (SyncStatus)

- 每源的水位状态表: 源 / 表 / 水位列 / 高水位 / 最近同步
- 动作按钮组: 立即同步 / 对账 L1 / 深度对账 / 重新映射
- 按钮复用后端动作接口，错峰窗口外点击返回提示

### 4.3 审计日志 (Audit)

- 发往源库的每条 SQL 记录表: 时间 / 源 / 动作 / 行数 / 耗时 / SQL
- 分页加载
- 支持按源和动作类型筛选

### 4.4 对象层 (Objects)

- 对象卡片列表: 对象名/显示名 / 行数 / 最后物化时间 / 未处理隔离数
- 支持按对象重试映射

### 4.5 隔离区 (Quarantine)

- 未处理隔离行: # / 对象 / 业务键 / 原因 / 时间 / [修复后重试]
- 支持按对象筛选
- 单行重试按钮

### 4.6 模板 (Templates) — 占位

- 展示当前模板包对象列表 (从 API 获取)
- 初版只读展示，不做编辑

### 4.7 配置 (Settings) — 占位

- 展示当前 connect.yaml 的核心配置
- DSN 脱敏展示 (仅显示环境变量名)
- 初版只读展示

## 5. 状态管理 (Pinia)

每个功能域一个 store，数据流模式一致:

```
store.refresh()           → 调用 GET API 获取最新数据
store.execute(action)     → 调用 POST 动作接口
  └─ 成功后自动 refresh() → 视图自动更新
```

轮询: 页面 `onMounted` 时启动 `setInterval(store.refresh, 5000)`，
`onUnmounted` 清除。后续按需升级 SSE。

Stores:
- `useOverviewStore` — overview API
- `useSyncStore` — overview + runs API
- `useObjectStore` — overview (对象层部分) + apply/retry action
- `useQuarantineStore` — quarantine API + retry action
- `useAuditStore` — audit API

## 6. API 层

```typescript
// 后端启动后，导出 OpenAPI schema
// curl http://localhost:8849/openapi.json > console-ui/openapi.json

// 生成类型安全客户端
// npx openapi-typescript openapi.json -o src/types/api.ts

// 封装
const client = createClient<paths>({ baseUrl: "/api" })

// 使用示例
const { data } = await client.GET("/api/overview")
await client.POST("/api/actions/sync", { body: { source: "digiwin_e10" } })
```

## 7. 目录结构

```
console-ui/
├── index.html
├── package.json
├── vite.config.ts
├── tsconfig.json
├── openapi.json                 # 后端 OpenAPI schema (构建时读)
├── Dockerfile                   # 多阶段构建
├── nginx.conf                   # SPA fallback + API proxy
├── public/
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── router/
│   │   └── index.ts             # history 模式路由
│   ├── stores/
│   │   ├── overview.ts
│   │   ├── sync.ts
│   │   ├── objects.ts
│   │   ├── quarantine.ts
│   │   └── audit.ts
│   ├── api/
│   │   └── client.ts            # openapi-typescript 生成的客户端封装
│   ├── views/
│   │   ├── Dashboard.vue
│   │   ├── SyncStatus.vue
│   │   ├── Objects.vue
│   │   ├── Quarantine.vue
│   │   ├── Audit.vue
│   │   ├── Templates.vue
│   │   └── Settings.vue
│   ├── components/
│   │   ├── layout/
│   │   │   ├── AppLayout.vue
│   │   │   ├── TopBar.vue
│   │   │   └── SideMenu.vue
│   │   ├── dashboard/
│   │   │   ├── StatCard.vue
│   │   │   ├── SyncTrend.vue
│   │   │   └── ObjectBar.vue
│   │   └── shared/
│   │       ├── ActionButton.vue
│   │       └── StatusBadge.vue
│   └── types/
│       └── api.ts               # openapi-typescript 自动生成
└── dist/                        # 构建产物 (gitignored)
```

## 8. 部署

### 开发模式

```bash
cd console-ui
npm run dev   # Vite dev server :5173, proxy /api → localhost:8849
```

### 生产模式

Docker Compose 加 nginx 容器:

```yaml
console-ui:
  build: ./console-ui
  ports: ["80:80"]
  depends_on: [console-api]
```

`nginx.conf`:
```
server {
  root /usr/share/nginx/html;
  location /api/ { proxy_pass http://console-api:8849/api/; }
  location / { try_files $uri /index.html; }  # SPA fallback
}
```

Dockerfile 多阶段构建:
```
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
```

### 后端需配合的改动

1. 暴露标准 OpenAPI schema (`/openapi.json`)
2. 新增 `/api/config` 端点支持配置页面
3. CORS 放行 (开发用; 生产 nginx 同域不需要)
4. `ui.py` 标记废弃

## 9. 通用交互规范

### 9.1 加载状态

- 首次加载: 页面内容区显示 Element Plus `<el-skeleton>` 骨架屏
- 后续轮询刷新: 静默更新，不显示 loading
- 动作执行中: 按钮 `loading` 态 + 禁用，完成后 Toast 通知

### 9.2 空状态

| 视图 | 空状态文案 |
|------|-----------|
| 仪表盘 | 统计卡片显示 "—"，图表区显示"暂无数据" |
| 抽取状态 | "尚无水位状态，执行首次同步后将显示" |
| 隔离区 | `<el-empty description="隔离区为空，系统运行良好" />` |
| 审计日志 | "暂无审计记录" |
| 模板/配置 | "页面建设中" (占位阶段) |

### 9.3 错误处理

- 网络错误: Toast 提示 "连接失败"，不刷新页面
- API 错误: 根据 HTTP 状态码显示对应消息
  - 401: 弹出 Token 输入框，静默刷新（不跳转）
  - 409: Toast 显示后端返回的 reason（如"错峰窗口外"）
  - 5xx: Toast 提示"服务异常，请稍后重试"
- 动作错误: 在按钮旁内联显示错误消息，不弹窗

### 9.4 Token 认证

- 页面加载时检测 401 → 弹出输入框，用户输入后存入 `localStorage`
- 所有 API 请求通过 axios 拦截器自动带 `Authorization: Bearer <token>`
- 顶栏显示认证状态：未认证 / 已认证
- Token 无效时不清除本地存储，提示用户重新输入

### 9.5 自动刷新

- 仪表盘: 10 秒轮询（数据量大，降低频率）
- 抽取状态: 10 秒轮询
- 审计日志: 不自动刷新（历史数据）
- 隔离区: 不自动刷新
- 对象层: 不自动刷新
- 页面切到后台时暂停轮询 (`document.visibilitychange`)

## 10. Element Plus 主题覆盖

Element Plus 默认主题为亮蓝色 (`#409EFF`)，需覆盖为项目配色:

```scss
// element-plus 主题变量覆盖
$--el-color-primary: #1677FF;
$--el-color-success: #52c41a;
$--el-color-warning: #faad14;
$--el-color-danger: #cf1322;
```

## 11. 不做的事情

- 对象流程图 / 血缘图 (场景未到)
- 移动端适配 (内网桌面浏览器场景)
- 国际化 (中文硬编码)
- 实时 SSE 推送 (先轮询，后续升级)
- 离线支持 / PWA
- 深色模式
