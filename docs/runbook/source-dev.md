# 源码开发运行

本文用于开发者在本机用源码启动 data2agent。现场部署请使用 Release 便携包，见 [portable.md](portable.md)。

## 1. 前置环境

- Python 3.11 或以上
- Node.js 22 或以上
- npm 10 或以上

以下命令均在项目根目录执行。

```bash
cd /Users/max/projects/data2agent
```

## 2. 安装依赖

创建并启用 Python 虚拟环境:

```bash
python -m venv .venv
source .venv/bin/activate
```

安装 Python 开发依赖:

```bash
pip install -U pip
pip install -e ".[dev,mcp,console,ingest,connect,middle_admin,excel]"
```

安装 Console 前端依赖:

```bash
cd console-ui
npm ci
cd ..
```

## 3. 生成本地参考数据

生成 E10-like 参考源库:

```bash
python -m data2agent.showroom.seed
```

抽取参考源数据到本地落地库:

```bash
python -m data2agent.connect sync --sqlite showroom/e10.sqlite
```

构建并发布对象层数据集:

```bash
python -m data2agent.connect apply
```

完成后会生成:

```text
landing/factory.sqlite
```

## 4. 启动平台 Console

开发时推荐使用「后端 API + Vite 前端」方式。

终端 1 启动平台后端:

```bash
source .venv/bin/activate
python -m data2agent.console \
  --landing landing/factory.sqlite \
  --templates templates \
  --host 127.0.0.1 \
  --port 8849
```

终端 2 启动 Vue Console:

```bash
cd console-ui
npm run dev:real
```

浏览器访问:

```text
http://127.0.0.1:5173/v1/
```

Vite 会把 `/api` 请求代理到平台后端 `http://127.0.0.1:8849`。

## 5. 按静态页面方式本地验证

如果要模拟 exe / Release 便携包里的平台页面托管方式，先构建 Vue 静态文件:

```bash
cd console-ui
npm run build
cd ..
```

再启动平台后端:

```bash
source .venv/bin/activate
python -m data2agent.console \
  --landing landing/factory.sqlite \
  --templates templates \
  --host 127.0.0.1 \
  --port 8849
```

浏览器访问:

```text
http://127.0.0.1:8849/v1/
```

这种方式不需要 `npm run dev:real` 常驻，页面和 API 都由 `8849` 端口提供。

## 6. 可选:启动 MCP HTTP

```bash
D2A_MCP_TOKEN=dev-mcp-token python -m data2agent.mcp_server \
  --db landing/factory.sqlite \
  --templates templates \
  --transport http \
  --host 127.0.0.1 \
  --port 8848
```

## 7. 可选:启动中间机管理界面

```bash
python -m data2agent.middle_admin \
  --host 127.0.0.1 \
  --port 8851
```

浏览器访问:

```text
http://127.0.0.1:8851/config
```

## 8. 常用检查

Python 回归测试:

```bash
pytest tests -q
```

模板校验:

```bash
python -m data2agent.metamodel.validate templates
```

Console 前端检查:

```bash
cd console-ui
npm run api:check
npm run typecheck
npm run lint -- --quiet
npm run test
npm run build
node scripts/check-dist.mjs
```

## 9. 常见问题

### `/v1/` 提示 Vue Console 未安装

说明 `console-ui/dist/index.html` 不存在。执行:

```bash
cd console-ui
npm run build
```

或者开发时直接访问 Vite 地址:

```text
http://127.0.0.1:5173/v1/
```

### MCP Server 提示落地库不存在

先生成本地参考数据:

```bash
python -m data2agent.showroom.seed
python -m data2agent.connect sync --sqlite showroom/e10.sqlite
python -m data2agent.connect apply
```

### 端口占用

默认端口:

| 服务 | 端口 |
| --- | --- |
| 平台 Console 后端 | `8849` |
| MCP HTTP | `8848` |
| 中间机管理界面 | `8851` |
| Vue Console 开发服务 | `5173` |
