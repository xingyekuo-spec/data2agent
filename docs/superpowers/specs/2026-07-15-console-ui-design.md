# console-ui · 运维控制台前端重设计

> 状态: 当前实施规格(v1.2,已对齐产品路线)· 2026-07-17 · 消费者: 工厂 IT / 实施伙伴
> 上层基线:[产品开发路线图](../plans/2026-07-17-product-development-roadmap.md)。若范围或优先级冲突,以路线图为准。

> **修订记录 v1.1(2026-07-15 评审)**:① 补前置依赖 —— 后端全端点 response model
> 化,否则类型生成全是 unknown;② 决策本机(非 Docker)用户方案:ui.py 降级保留;
> ③ API 层重写(统一 openapi-fetch、修 baseUrl 双前缀 bug、加 schema 漂移 CI 检查、
> 明确动作语义 200/executed:false vs 409);④ 后端配合清单从 4 条补全到 10 条;
> ⑤ 隔离区改按对象重试(后端语义如此);⑥ Element Plus 主题语法更正;⑦ 新增测试与 CI 节。
>
> **修订记录 v1.2(2026-07-17)**:Vue Console 从远期规划升级为 v0.2 主产品路线;
> 增加管道、数据浏览与 MCP Lab;统一 MOCK/DEMO/REAL 标识;字段级血缘移入 v0.3;
> Token 改用 sessionStorage;Jinja 管理页继续作为安装与故障恢复入口。

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
| API 客户端 | openapi-typescript + openapi-fetch | 类型自动生成;统一 fetch 客户端(不引 axios),认证走 openapi-fetch 中间件 |

**硬性约束**:
- 静态资源全部打进 dist，不依赖外部 CDN
- Docker 多阶段构建产 nginx 镜像
- 仓库不进 node 工具链 (`.gitignore` dist 以外的构建产物)
- 顶栏始终显示 `MOCK / DEMO / REAL`;生产构建不得默认启用 Mock
- 未知或无法检测的状态必须显示 `unknown`,不得推断为正常
- API 失败不得转换为空列表后继续显示成功状态

### 2.1 Mock 与真实接口切换

- API 层支持显式 `mock / demo / real` 模式,领域组件不直接判断模式;
- fixture 至少覆盖:首次安装、全链路正常、运行中、推送失败、apply 熔断且使用旧版本、
  服务不可达、未处理隔离、draft 口径、Token 无效和未知错误;
- 每个 fixture 与真实 OpenAPI 响应使用同一 TypeScript 类型;
- Mock 只用于开发和演示,不能生成生产验收结论。

## 3. 布局与导航

> 2026-07-18 修订:顶栏由深色改为白色、品牌区移入侧栏、菜单选中改为浅蓝
> 圆角胶囊、内容区卡片化(按评审通过的参考 UI 调整)。菜单结构(分组与
> 路径)不变。

### 3.1 顶栏

- 白色底，高度 48px，底部 1px 分隔线，吸顶
- 左侧: 当前页面标题(跟随路由)
- 右侧: 模式标识 / 用户入口(源连接状态、上次抽取时间、隔离待处理数在 M3 加入)
- 无水平菜单

### 3.2 侧边栏

- 白色底，宽度 240px，右侧 1px 分隔线
- 顶部品牌区: 深色方块 logo + 产品名
- 两级菜单结构: 可折叠分组(右侧箭头) + 带图标页面项
- 选中项: 浅蓝圆角胶囊 (`#E6F4FF` 底 + `#1677FF` 文字)

### 3.3 菜单结构

```
运维监控 (分组标题)
  ├─ 仪表盘     /
  ├─ 管道状态   /pipeline
  ├─ 运行记录   /runs
  └─ 审计日志   /audit

数据管理
  ├─ 数据浏览   /data
  ├─ 隔离区     /quarantine
  └─ 模板       /templates

Agent
  └─ MCP Lab    /mcp

系统
  └─ 配置       /settings (占位)
```

### 3.4 配色

| 元素 | 颜色 |
|------|------|
| 顶栏背景 | `#FFFFFF` |
| 品牌方块 | `#001529` |
| 主强调色 | `#1677FF` |
| 菜单选中底 | `#E6F4FF` |
| 成功/正常 | `#52c41a` |
| 危险/告警 | `#cf1322` |
| 警告/暂停 | `#faad14` |
| 内容区背景 | `#f5f7fa` |
| 卡片背景 | `#FFFFFF`(圆角 8px,1px `#E5E6EB` 边) |
| 文字主色 | `#001529` |
| 文字辅色 | `#999999` |

## 4. 页面设计

### 4.1 仪表盘 (Dashboard)

默认首页。4 个统计卡片 + 抽取趋势图 + 对象分布条 + 最近运行表。

- **统计卡片**: 对象层总行数(口径:obj_* 合计,与 raw 层因隔离/软删有差,卡片脚注标明)/
  对象层覆盖率(已物化对象数 ÷ 模板对象数)/ 隔离区待处理 / 上次运行时间
- **抽取趋势**: ECharts 柱状图,最近 24 小时各轮 sync 的行数
  (依赖后端 d2a_sync_run 增加 run_type 字段区分 sync/apply/reconcile,见 §8)
- **对象分布**: 水平数据条，各对象行数占比
- **最近运行**: 仅展示最近 5 条，更多通过"抽取状态"视图查看

### 4.2 管道状态 (Pipeline)

- 按 `ERP → 抽取 → 推送 → Raw → 映射 → 对象层 → MCP` 展示链路;
- 节点状态统一为 `unknown / idle / running / healthy / warning / failed / stale`;
- 节点展示最近成功/失败时间、输入输出、耗时、版本、错误摘要和详情入口;
- 明确显示对象层是否仍在使用上一稳定版本。

### 4.3 运行记录 (Runs)

- 每源的水位状态表: 源 / 表 / 水位列 / 高水位 / 最近同步
- 运行列表与详情:sync / apply / reconcile / ingest,含表/对象/批次步骤
- 动作按钮组: 立即同步 / 对账 L1 / 深度对账 / 重新映射
- 按钮复用后端动作接口，错峰窗口外点击返回提示

### 4.4 审计日志 (Audit)

- 发往源库的每条 SQL 记录表: 时间 / 源 / 动作 / 行数 / 耗时 / SQL
- 分页加载
- 支持按源和动作类型筛选

### 4.5 数据浏览 (DataBrowser)

- raw 表和对象层只读浏览,服务端分页并强制 limit 上限;
- 支持按业务键搜索、查看原始 JSON、批次和更新时间;
- 敏感字段与脱敏状态必须显著标识;
- 对象卡片展示对象名、显示名、行数、最后物化时间和未处理隔离数。

### 4.6 隔离区 (Quarantine)

- 未处理隔离行: # / 对象 / 业务键 / 原因 / 时间
- 支持按对象筛选
- **按对象重试**(非单行):后端 retry 语义是对该对象整体重新映射(apply 为全量重建,
  不存在行级重试)。UI 按对象分组,组头放 [修复后重试该对象] 按钮,
  并提示"将重新映射整个对象,该对象全部隔离记录会被重新评估"

### 4.7 模板 (Templates)

- 展示当前模板包对象列表 (从 API 获取)
- 展示属性、binding 状态、field map、枚举 map 与 derived 规则;
- v0.2 只读展示，不做编辑。

### 4.8 MCP Lab

- 调用 `query_objects` / `query_metrics`,显示原始 JSON、脱敏字段与口径警示;
- 生成“说”档建议卡并展开 evidence;
- 建议卡使用独立的 gateway/proposal API,不扩大 Jinja `/api/debug/mcp-call` 的只读白名单;
- v0.3 接入主体、会话和结果摘要级证据。

### 4.9 配置 (Settings) — 占位

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

Stores(**overview 只有一个 store、一处轮询**,避免三个页面各自打同一接口):
- `useOverviewStore` — overview API 的唯一持有者;Dashboard / SyncStatus / Objects
  页面都消费它(computed 切片),轮询由它统一管理(按当前路由决定间隔)
- `usePipelineStore` — pipeline/services API 与节点状态
- `useRunStore` — runs API + sync/reconcile actions
- `useDataStore` — raw/object 分页浏览与业务键搜索
- `useObjectStore` — apply/retry actions(数据来自 useOverviewStore)
- `useQuarantineStore` — quarantine API + retry action
- `useAuditStore` — audit API(分页 + 筛选参数)
- `useMcpLabStore` — 查询、指标、建议卡和 evidence

## 6. API 层(v1.2)

### 6.1 前置依赖:后端 response model 化

**这是整个类型链路的地基,前端动工前必须完成。**现有 console API 全部返回裸
`dict` / `list[dict]`,FastAPI 生成的 OpenAPI 里响应 schema 为空对象,
openapi-typescript 会产出 `unknown` —— 类型安全名存实亡。
后端须为全部端点定义 pydantic response model(Overview / SyncStateRow /
RunRecord / QuarantineRecord / AuditRecord / ActionResult 及各动作的扩展),见 §8。

### 6.2 契约工作流(快照 + 漂移防护)

```bash
# 后端起服务后导出 schema 快照(提交进仓库,前端离线可构建)
curl http://localhost:8849/openapi.json > console-ui/openapi.json
# 生成类型
npx openapi-typescript console-ui/openapi.json -o console-ui/src/types/api.ts
```

**漂移防护(必须)**:快照提交进仓库意味着后端改接口后快照会过期,而类型照样
编译通过,问题拖到运行期才暴露。CI 增加契约检查:从后端代码直接导出 schema
(`python -c "...create_app(...).openapi()"`,无需起服务)与提交的快照 diff,
不一致即失败,提示重新生成快照与类型。

### 6.3 客户端(统一 openapi-fetch,不引 axios)

```typescript
import createClient from "openapi-fetch";
import type { paths } from "@/types/api";

// 注意:openapi-typescript 的 paths 已含完整路径(/api/overview),
// baseUrl 只填源(同域为空),不要再加 /api 前缀(否则请求成 /api/api/overview)
const client = createClient<paths>({ baseUrl: "" });

// Token 认证走中间件(§9.4 与此统一,不用 axios 拦截器)
client.use({
  onRequest({ request }) {
    const token = sessionStorage.getItem("d2a_token");
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    return request;
  },
});

const { data, error } = await client.GET("/api/overview");
await client.POST("/api/actions/sync", { body: { source: "digiwin_e10" } });
```

### 6.4 动作语义(与后端现状对齐)

| 后端返回 | 含义 | 前端处理 |
| --- | --- | --- |
| `200 {executed: true, ...}` | 动作已执行 | 成功 Toast + refresh |
| `200 {executed: false, note}` | **策略性跳过**(如错峰窗口外)—— 不是错误 | 信息级 Toast 显示 note |
| `409` | 状态冲突:只读模式(未加载 --config)/ 熔断 | 警告 Toast 显示 detail |
| `401` | 未认证 | 弹 Token 输入框(§9.4) |
| `404 / 422` | 参数错误(未知源 / 缺参数) | 错误 Toast 显示 detail |

错误体统一 `{detail: string}`(FastAPI HTTPException 默认形状);
动作结果统一以 `ActionResult` 为基座(executed + note),各动作扩展自有字段
(apply: results/aborted;retry: mapped/quarantined)。

### 6.5 列表接口参数

审计日志:`GET /api/audit?limit&offset&source&action&from&to`;
隔离区:`GET /api/quarantine?object`(现状够用);runs:`?limit&offset&type&status`。
既有 runs/audit 保持数组正文,分页总数由 `X-Total-Count` 响应头返回;
raw/object 新接口使用具名分页对象。

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
│   │   ├── pipeline.ts
│   │   ├── runs.ts
│   │   ├── data.ts
│   │   ├── quarantine.ts
│   │   ├── audit.ts
│   │   └── mcpLab.ts
│   ├── api/
│   │   └── client.ts            # openapi-typescript 生成的客户端封装
│   ├── views/
│   │   ├── Dashboard.vue
│   │   ├── Pipeline.vue
│   │   ├── Runs.vue
│   │   ├── DataBrowser.vue
│   │   ├── Quarantine.vue
│   │   ├── Audit.vue
│   │   ├── Templates.vue
│   │   ├── McpLab.vue
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

`vite.config.ts` 的生产 `base` 固定为 `/v1/`。Docker Compose 加 nginx 容器,
Vue 固定挂 `/v1`;Jinja 管理页继续使用 `/`:

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
  location /v1/ { try_files $uri $uri/ /index.html; }  # Vue SPA fallback
  location / { proxy_pass http://console-api:8849; }    # Jinja 管理页与 /v0
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
COPY --from=build /app/dist /usr/share/nginx/html/v1
COPY nginx.conf /etc/nginx/conf.d/default.conf
```

### 本机(非 Docker)用户方案 —— v1.2 决策

`dist/` 不进仓库 + 生产走 nginx 容器,会让 `pip install` + `python -m
data2agent.console` 的本机用户没有页面可看(README 快速开始里就有这条命令)。决策:

1. **ui.py 降级保留**(不是废弃):作为本机场景的简版页,顶部加横幅
   "简版控制台;完整版见 console-ui(Docker / npm run build)";
2. FastAPI 启动时检测已打包的 `console-ui/dist/`，存在则挂载到 `/v1` 并提供 SPA fallback;
   不存在时 `/` 与 `/v0` 仍由 Jinja/v0 页面服务,`/v1` 明确提示未安装 Vue 产物;
3. dist 随 release 附件 / PyPI 包数据分发后,再评估移除 ui.py。

### 后端需配合的改动(v1.2,前 3 条为前端动工前置)

1. **全部端点 response model 化**(§6.1,类型链路地基);
2. `d2a_sync_run` 增加 `run_type` 字段(sync / apply / reconcile),
   runs 接口对外支持 `?type=` 筛选(数据库 `run_type` 仅为内部列名);
3. 审计接口分页 + 筛选:`?limit&offset&source&action&from&to`,数组正文保持
   兼容并通过 `X-Total-Count` 返回总数;
4. 新增 `/api/templates`(模板页,只读:对象/属性/binding 状态,来自元模型);
5. 新增 `/api/config`(配置页,只读;SourceConfig 本身只存 dsn_env 环境变量名,
   不含凭据,天然安全 —— 实现时禁止任何解析环境变量的路径);
6. 动作接口统一 ActionResult 基座(§6.4);
7. 新增 `/api/pipeline`、`/api/services` 与标准化运行详情 `/api/runs/{run_id}`;
8. 新增 raw/object 安全分页浏览接口,所有表名/字段名来自后端白名单;
9. 新增独立建议卡端点;Jinja `/api/debug/mcp-call` 继续只允许查询类工具;
10. dist 存在时挂载 `/v1` + SPA fallback,否则保留 Jinja `/` 与 `/v0`(见上);
11. 根 docker-compose.yml 增加 Vue 构建/静态服务,同时保留 Jinja 路由;
12. CI:契约漂移检查(§6.2)+ `npm ci && npm run build` 前端构建检查
   (dist 不进仓库,不构建即腐);
13. `.gitignore` 补 `console-ui/node_modules/`、`console-ui/dist/`。

注:CORS 无需放行 —— 开发模式 Vite proxy 转发 /api(同源),生产 nginx 同域。
FastAPI 的 /openapi.json 默认已暴露,无需改动。

## 9. 通用交互规范

### 9.1 加载状态

- 首次加载: 页面内容区显示 Element Plus `<el-skeleton>` 骨架屏
- 后续轮询刷新: 静默更新，不显示 loading
- 动作执行中: 按钮 `loading` 态 + 禁用，完成后 Toast 通知

### 9.2 空状态

| 视图 | 空状态文案 |
|------|-----------|
| 仪表盘 | 统计卡片显示 "—"，图表区显示"暂无数据" |
| 管道状态 | "尚无运行状态，执行首次同步后将显示" |
| 数据浏览 | "尚无可浏览数据，完成同步与映射后将显示" |
| MCP Lab | "对象层尚未就绪，完成同步与映射后再试" |
| 隔离区 | `<el-empty description="隔离区为空，系统运行良好" />` |
| 审计日志 | "暂无审计记录" |
| 模板/配置 | "页面建设中" (占位阶段) |

### 9.3 错误处理

- 网络错误: Toast 提示 "连接失败"，不刷新页面
- API 错误: 根据 HTTP 状态码显示对应消息(与 §6.4 动作语义表一致)
  - 401: 弹出 Token 输入框，静默刷新（不跳转）
  - 409: 警告 Toast 显示 detail(只读模式 / 熔断;**窗口外不是 409**,
    是 200 + executed:false,走信息级 Toast)
  - 404/422: 错误 Toast 显示 detail
  - 5xx: Toast 提示"服务异常，请稍后重试"
- 动作错误: 在按钮旁内联显示错误消息，不弹窗

### 9.4 Token 认证

- 页面加载时检测 401 → 弹出输入框，用户输入后存入 `sessionStorage`
- 所有 API 请求经 openapi-fetch 中间件自动带 `Authorization: Bearer <token>`
  (统一 §6.3 的客户端,不引 axios)
- 顶栏显示认证状态：未认证 / 已认证
- Token 无效时清除当前 sessionStorage 值并提示用户重新输入

### 9.5 自动刷新

- 仪表盘: 10 秒轮询（数据量大，降低频率）
- 抽取状态: 10 秒轮询
- 审计日志: 不自动刷新（历史数据）
- 隔离区: 不自动刷新
- 对象层: 不自动刷新
- 页面切到后台时暂停轮询 (`document.visibilitychange`)

## 10. Element Plus 主题覆盖

Element Plus 默认主题为亮蓝色 (`#409EFF`)，需覆盖为项目配色。
**CSS 变量运行时覆盖**(与 wangjia-sh admin-web 同款做法,v1.1 评审后统一;
只覆盖四个色值,无需引入 sass 依赖与编译期注入):

```css
/* src/styles.css —— Element Plus 2.x 全部色值走 CSS 变量,直接覆盖即可 */
:root {
  --el-color-primary: #1677FF;
  --el-color-success: #52c41a;
  --el-color-warning: #faad14;
  --el-color-danger:  #cf1322;
}
```

## 11. 测试与 CI(v1.1 新增)

后端已有 103 个测试的工程文化,前端不能是裸奔的例外;按投入产出排:

1. **契约漂移检查(CI,最高价值)**:后端导出 schema vs 提交快照 diff(§6.2);
2. **构建检查(CI)**:`npm ci && npx vue-tsc --noEmit && npm run build` ——
   dist 不进仓库,不在 CI 构建就会腐;
3. **vitest 冒烟**:store 数据流(refresh/execute→refresh)与关键组件
   (StatusBadge 状态映射、ActionButton 三态)的单测,不追求覆盖率指标;
4. 后端配合项(response model / run_type / 分页)各自带 pytest,归后端测试套件。

CI 新增 job `console-ui`(node 22),与现有 python job 并行,只在
`console-ui/**` 或 console API 相关路径变更时触发(paths 过滤)。

## 12. 分阶段范围与非目标

- v0.2 不做字段级血缘;v0.3 必须实现字段追溯详情,但不做自由布局的复杂全局血缘大图
- v0.2 不做在线生产 mapping 编辑、自动 verified 或数据删除
- 移动端适配 (内网桌面浏览器场景)
- 国际化 (中文硬编码)
- 实时 SSE 推送 (先轮询，后续升级)
- 离线支持 / PWA
- 深色模式
