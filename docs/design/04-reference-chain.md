# 04 · 参考数据链与回归场景

> 状态:SQLite/SQL Server 参考数据链、Vue Console 真实 API 验收已实现(r4,2026-07-24)·
> 测试资产:`tests/fixtures/e10/` + `tests/integration/mssql/docker-compose.yml`
> 上层基线:[路线图](../roadmap.md)

## 1. 定位

本模块描述 E10-like **测试**参考数据链,用于自动测试、字典生成、本地冒烟和真实 API 验收。
它不是产品运行模式,不随 wheel / 便携包发布。

生产试点仍要求跨机对账、批次回执和加密传输。

## 2. 当前拓扑

```
mssql-sim     SQL Server 容器(tests/integration/mssql),fixtures seed 灌数
   ▼ 抽取(connect,走 mssql_readonly)
landing-sqlite SQLite 落地库(raw_* + 物化对象表)
   ├─ mcp     MCP 网关(streamable HTTP :8848)
   └─ console Vue Console(:8849 `/`) —— 仅真实 API
```

本机快速版:

```bash
python -m tests.fixtures.e10.seed --db /tmp/e10.sqlite
# 配置 connect.yaml 指向该库后:
python -m data2agent.middle.extract sync --config connect.yaml
python -m data2agent.middle.extract apply --config connect.yaml
```

SQL Server 集成:

```bash
docker compose -f tests/integration/mssql/docker-compose.yml up \
  --build --abort-on-container-exit --exit-code-from runner
```

## 3. 已实现

- E10 参考表形 + 确定性 seed(`tests/fixtures/e10/`);
- 表字典生成(`docs/dict/digiwin_e10.md`);
- binding↔表形一致性测试;
- 抽取表配置使用 `connect.yaml` 显式 `tables`(可为空);本地开发可显式列出基线表。

```yaml
sources:
  digiwin_e10:
    adapter: sqlite_readonly
    path: /tmp/e10.sqlite
    tables:
      CUSTOMER:       { mode: incremental, watermark: LAST_MODIFIED_DATE }
      CURRENCY:       { mode: full_refresh }
      ITEM:           { mode: incremental, watermark: LAST_MODIFIED_DATE }
      QUOTATION:      { mode: incremental, watermark: LAST_MODIFIED_DATE }
      SALES_ORDER:    { mode: incremental, watermark: LAST_MODIFIED_DATE }
      SALES_ORDER_D:  { mode: incremental, watermark: LAST_MODIFIED_DATE }
```

## 4. 接单评审参考链

离线脚本入口已删除。链路断言保留在 `tests/test_mcp_core.py`
（`tests.fixtures.e10.review.build_review`），与真 Agent 编排场景见历史设计说明。

## 5. 相关

- fixtures 源码:`tests/fixtures/e10/`
- MSSQL 集成:`tests/integration/mssql/`
- 迁移核对:[2026-07-24-m7-showroom-migration-checklist.md](../superpowers/plans/2026-07-24-m7-showroom-migration-checklist.md)
