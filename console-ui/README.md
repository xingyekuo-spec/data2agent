# data2agent Vue Console(console-ui)

data2agent 的日常监控与数据验证主界面,已覆盖 v0.2 可观察与 v0.3 可验证能力。
Vue 3 + Vite + TypeScript + Element Plus + Pinia + Vue Router。

> 平台安装、配置和故障恢复均由本 Vue Console 提供;生产环境挂载在根路径 `/`。
> 开发与生产均只连接真实后端 API(无产品级 Mock 模式)。

## 环境要求

- Node 22+,npm 10+(`package-lock.json` 是唯一包管理基线,CI 用 `npm ci`)

## 快速开始

```bash
npm ci
# 先在仓库根启动平台 console(:8849),再:
npm run dev        # Vite 开发服;/api 代理到 http://127.0.0.1:8849
```

开发服务地址:<http://localhost:5173/>。顶栏显示 `REAL`。

Token 只保存在当前标签页 `sessionStorage`(键 `d2a_token`),不进 localStorage、
URL 或日志;401 时自动清除并弹出认证提示。

## 测试

```bash
npm test                 # Vitest(src/test 下 fetch stub + fixtures,非产品模式)
npm run build
node scripts/check-dist.mjs
node scripts/e2e-acceptance.mjs --real   # Playwright 真实 API 验收
```

## 目录概要

```
src/
├── api/                 # 类型化客户端
├── components/          # 布局与业务组件
├── config/              # 模式(恒 real)与路由辅助
├── test/                # Vitest fetch stub、场景 fixtures(不进入产品运行时)
├── stores/ views/ ...
```

生产构建不得携带测试 fixture 场景数据;由 `check-dist.mjs` 门禁。
