# M7 展厅 / Mock 迁移核对清单

> 生成日期：2026-07-24  
> 分支：`feature/m7-showroom-cleanup`  
> 方法：删除前 `rg` 快照（实施计划历史段落除外）

## 1. Python import / `python -m` 入口

| 符号 | 引用位置（迁移前） | 处置 |
| --- | --- | --- |
| `data2agent.showroom.seed` (`build`/`write_db`) | ~30 个 `tests/test_*.py`、`scripts/smoke_admin_ui.py`、`console-ui/scripts/e2e-acceptance.mjs` | → `tests.fixtures.e10.seed` |
| `data2agent.showroom.e10_schema` | `tests/test_showroom.py`、`seed_mssql.py` | → `tests.fixtures.e10.schema` |
| `data2agent.showroom.seed_mssql` | `tests/integration/mssql/docker-compose.yml`、根 `docker-compose.yml` | → `tests.fixtures.e10.seed_mssql`；根 compose 删除 |
| `data2agent.showroom.review_demo` | `tests/test_mcp_core.py`、`deploy/render_hero_svg.py`、文档 | 断言迁入测试；演示脚本删除 |
| `mcp_server/__main__.py` 错误提示 | 指向 showroom.seed | 改为 fixtures 命令或中性提示 |

## 2. Compose / 配置路径

| 路径 | 处置 |
| --- | --- |
| 根 `docker-compose.yml` | 删除（演示拓扑） |
| `deploy/showroom-connect.yaml` | 删除 |
| `tests/integration/mssql/docker-compose.yml` | 保留；seed 命令改 fixtures |

## 3. 演示资产

| 路径 | 处置 |
| --- | --- |
| `deploy/demo.tape` | 删除 |
| `deploy/render_hero_svg.py` | 删除 |
| `data2agent/showroom/` 整包 | 删除 |

## 4. 前端 Mock

| 符号 | 处置 |
| --- | --- |
| `VITE_CONSOLE_MODE` | 删除；Console 恒 real |
| CI `e2e-mock` / `--mock` | 删除；仅保留 `--real` |
| `ScenarioSwitcher` / MSW browser worker | 从产品 UI/`main.ts` 移除 |
| Vitest 本地 handlers/fixtures | 可保留为测试基础设施（非产品 Mock 模式） |

## 5. 文档命令

| 文档 | 处置 |
| --- | --- |
| `README.md`、`docs/runbook/source-dev.md`、`docs/dict/digiwin_e10.md` | 改为 `python -m tests.fixtures.e10.seed` |
| `docs/design/00-overview.md`、`04-reference-chain.md`、`console-ui/README.md`、`docs/roadmap.md` | 去掉展厅/Mock 运行说明 |

## 6. 验收门禁（完成后）

- 全仓（除本清单与实施计划历史）无 `data2agent.showroom`、`showroom-connect.yaml`、旧模块名 `seed_mssql` 作为产品入口、`VITE_CONSOLE_MODE`
- wheel / portable 无 `showroom` 路径
