# data2agent Vue Console(console-ui)

data2agent 的日常监控与数据验证主界面,已覆盖 v0.2 可观察与 v0.3 可验证能力。
Vue 3 + Vite + TypeScript + Element Plus + Pinia + Vue Router。

> 安装/配置/故障恢复仍由 Jinja2 管理页(`/` 与 `/v0`)承担;本工程挂载在 `/v1`。

## 环境要求

- Node 22+,npm 10+(`package-lock.json` 是唯一包管理基线,CI 用 `npm ci`)

## 快速开始

```bash
npm ci
npm run dev        # 开发服务,默认 MOCK 模式:后端不启动也能完整演示
npm run dev:real   # 连接本机真实控制台 http://127.0.0.1:8849(/api 代理)
npm run dev:demo   # 连接展厅演示环境(真实 API,不用 fixture)
```

开发服务地址:<http://localhost:5173/v1/>。MOCK 模式下右下角有场景切换面板,
顶栏持续显示大写 `MOCK` 标识;DEMO / REAL 不加载 MSW。

## 环境模式

模式只来自构建/部署配置 `VITE_CONSOLE_MODE`,URL 不能切换模式:

| 模式 | 数据来源 | 说明 |
| --- | --- | --- |
| `mock` | 本地 typed fixture(MSW 拦截) | 开发默认;生产构建永远进不了 mock |
| `demo` | 展厅真实 API | 不用 fixture |
| `real` | 真实后端 | 生产构建默认(未设置时强制 real) |

Token 只保存在当前标签页 `sessionStorage`(键 `d2a_token`),不进 localStorage、
URL、日志或 fixture;401 时自动清除并弹出认证提示。

## Mock 场景(10 个)

`healthy`(全链路正常)、`empty-install`(首次安装)、`sync-running`(同步运行中)、
`ingest-failed`(推送失败)、`apply-circuit-broken`(apply 熔断用旧版本)、
`partial-services-down`(部分服务不可达)、`quarantine-pending`(未处理隔离)、
`draft-governance`(binding 仍为 draft)、`token-invalid`(401)、`unknown-error`(500)。

约定:

- fixture 全部 `satisfies` 生成类型,禁止 `as any`;`healthy` 不是解析失败的回退;
- 未匹配的请求直接报错,不静默穿透到真实网络;
- `unknown`、请求失败、未执行不得显示为绿色、零数据或正常空列表;
- `token-invalid` / `unknown-error` 不出现健康或空成功视图。

## API 类型(生成物,禁止手改)

```bash
npm run api:generate   # console-ui/openapi.json → src/types/api.ts
npm run api:check      # 漂移检查:改了 openapi.json 没重新生成则失败
```

`openapi.json` 由后端生成:`python scripts/export_console_openapi.py --check console-ui/openapi.json`。
`src/types/api.ts` 与递归 `JsonValueInput/Output` 别名由
`scripts/generate-api-types.mjs` 生成(生成器对自递归 $ref 的内联展开会触发
TS2502,脚本注入等价手写别名并校验 anyOf 形状防漂移)。

## 测试与构建

```bash
npm run lint         # ESLint(核心代码禁止显式 any)
npm run typecheck    # vue-tsc(含生成类型编译期校验 src/types/api.compile-check.ts)
npm run test         # Vitest + jsdom(MSW node server,不依赖已启动的后端)
npm run test:e2e     # Playwright 浏览器验收(Mock 场景 + Real 临时 SQLite/后端)
npm run build        # 生产构建:默认 REAL,base=/v1/
npm run preview      # 预览构建产物
node scripts/check-dist.mjs   # 产物检查:base=/v1/、无 CDN、全量产物无 Mock 痕迹
```

`test:e2e` 的 Real 部分需要 Python 后端(默认 `../.venv/bin/python`,可用
`D2A_PYTHON` 覆盖)与 Playwright 浏览器(`npx playwright install chromium`)。

## 目录速览

```text
src/
├── config/mode.ts        # mock|demo|real 解析(生产默认 REAL)
├── api/                  # client(唯一 openapi-fetch)/ errors / services
├── types/                # api.ts(生成物)、state.ts(请求/领域状态模型)
├── stores/               # session / overview(垂直切片)/ settings
├── mocks/                # MSW handlers、scenario 注册表、fixtures/(10 场景)
├── components/layout/    # AppLayout / TopBar / SideMenu / AuthDialog
├── components/shared/    # EnvironmentBadge / StatusBadge / 状态组件 / ScenarioSwitcher
├── router/               # 8 主页面 + 只读 Settings,history 模式
└── views/                # 页面骨架;Dashboard / Settings / Pipeline 已接垂直切片
```

产品边界见 [docs/roadmap.md](../docs/roadmap.md),控制台设计见 [docs/design/05-console.md](../docs/design/05-console.md)。
