# 05 · 运维控制台

> 状态:已实现(2026-07-11)· 实现:`data2agent/console/` · 入口:`python -m data2agent.console`

## 1. 定位与边界调整记录

给工厂 IT / 实施伙伴的运维界面:一屏看清抽取管道的健康状态,并执行日常运维动作。

**边界调整(2026-07-11,用户决策)**:运维监控与隔离区复核界面原划商业版
(docs 02 v0.1 非目标),现改划开源 —— 开源版提供**完整**运维控制台;
商业版聚焦审批治理(做档)、口径校准等**服务**,不再以"面板"为商业边界。

**前端架构调整(2026-07-15,用户决策)**:v0 内嵌单页(`ui.py` 一个 HTML 字符串)
在对象/源扩展时不可维护,改为独立前端项目(`console-ui/`),Vue 3 + Vite +
TypeScript。后端 FastAPI 的 JSON API 协议保持不变;v0 `ui.py` **降级保留**
(本机 pip 场景的简版页,dist 可分发后再评估移除 —— 评审修订 v1.1)。

## 2. 架构

**后端与前端分离,API 不变**:

- 后端:FastAPI + uvicorn(`console` 依赖组),JSON API + 动作接口,行为与约束
  同 v0(动作复用 connect 引擎、窗口/白名单原样生效);
- 前端:独立项目(仓库内 `console-ui/` 目录),Vue 3 + Vite + TypeScript,
  **不依赖外部 CDN**(静态资源全部打进 dist,内网部署只需一份构建产物);
  Docker 多阶段构建产最终镜像,仓库内不进 node 工具链;
- 前端只是 JSON API 的一个消费者 —— v0 内嵌单页(`ui.py`)降级保留为本机
  简版页(dist 存在时优先 mount 完整版),后端 API 协议保持兼容。

**与 v0 内嵌单页的关键差异**:

| 维度 | v0 (降级保留为简版) | v1 |
|------|------------|-----|
| 代码位置 | `console/ui.py` 一个 HTML 字符串 | `console-ui/` 独立项目 |
| 框架 | 原生 JS + 内联 CSS | Vue 3 + Vite + TypeScript |
| 构建 | 零构建 | `npm run build` → 静态 dist |
| 部署 | FastAPI 直接 serve 字符串 | FastAPI mount 静态目录,或独立 nginx |
| 轮询 | 5 秒全量轮询 | 保留轮询为 fallback,主路径按需升级 SSE |
| 图表 | 无 | ECharts(vendored,非 CDN) |
| 页面结构 | 单页 6 section 垂直堆叠 | 多视图导航:仪表盘 / 抽取 / 对象 / 隔离 / 审计 |

## 3. 视图与动作

| 视图(GET /api/*) | 内容 |
| --- | --- |
| overview | 每源水位状态、对象层(行数 / 物化时间 / 未处理隔离)、只读/完整模式 |
| runs | d2a_sync_run 最近运行(状态徽章:ok / paused / failed) |
| quarantine | 未处理隔离明细(业务键 / 原因 / 时间) |
| audit | 发往源库的每条 SQL(审计承诺的可视化) |

| 动作(POST /api/actions/*) | 语义 |
| --- | --- |
| sync | 立即同步一轮(= 调度器的 run_sync_cycle) |
| reconcile(deep 可选) | L1 对账 / 深度全段修复 |
| apply | 重新映射全部对象 |
| retry | 单对象修复后重试(成功自动取代旧隔离记录) |

**动作不开旁路**:全部复用 connect 引擎,错峰窗口、白名单、只读适配器约束
原样生效(窗口外发起同步会被拒绝并说明)。

## 4. 安全

- 可选 Bearer Token(`--token` / 环境变量 `D2A_CONSOLE_TOKEN`),内网部署建议启用;
- 不带 `--config` 启动为纯只读模式,动作接口返回 409;
- 展厅 compose 暴露 `:8849`(不启用认证);生产部署必须配 Token 且仅内网可达。

## 5. 运行方式

```bash
python -m data2agent.console --config connect.yaml          # 完整模式(动作可用)
python -m data2agent.console --landing landing/factory.sqlite  # 只读模式
docker compose up --build                                    # 展厅:http://localhost:8849
```
