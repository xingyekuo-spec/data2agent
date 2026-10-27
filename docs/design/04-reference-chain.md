# 04 · 参考数据链与回归场景

> 状态:SQLite/SQL Server 参考数据链与推送回归链已实现(r5,2026-10-27,平台端移出后改为中间机 + 参考接收端)·
> 测试资产:`tests/fixtures/e10/` + `tests/integration/mssql/docker-compose.yml`
> 上层基线:[路线图](../roadmap.md)

## 1. 定位

本模块描述 E10-like **测试**参考数据链,用于自动测试、字典生成、本地冒烟和真实 API 验收。
它不是产品运行模式,不随 wheel / 便携包发布。

跨机对账、批次回执与 generation 屏障已有自动化回归链(对端为 `tests/support/` 下的参考接收端)。正式生产试点
仍要求现场 HTTPS/TLS、凭据治理、SQLite 容量压测、备份恢复及断网重放演练。

## 2. 当前拓扑

```
mssql-sim     SQL Server 容器(tests/integration/mssql),fixtures seed 灌数
   ▼ 抽取(connect,走 mssql_readonly)
   ├─ sink: local    SQLite 本地落地(raw_*),开发 / 对照实现
   ├─ sink: webhook  tests/support/webhook_receiver.py(或 scripts/dev_webhook_receiver.py 真实 HTTP)
   └─ sink: http     tests/support/reference_ingest.py(ingest v3 参考接收端,含 E6b 对账)
```

本机快速版:

```bash
python -m tests.fixtures.e10.seed --db /tmp/e10.sqlite
# 配置 connect.yaml 指向该库后:
python -m data2agent.middle.extract sync --config connect.yaml
```

SQL Server 集成:

```bash
docker compose -f tests/integration/mssql/docker-compose.yml up \
  --build --abort-on-container-exit --exit-code-from runner
```

## 3. 已实现

- E10 参考表形 + 确定性 seed(`tests/fixtures/e10/`);
- 表字典生成(`docs/dict/digiwin_e10.md`);
- 测试抽取计划(`tests/helpers.py` 的 E10 表清单与水位)↔表形一致性测试;
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

## 4. 推送回归链

- webhook:`tests/middle/test_webhook_sink.py`(推送与直连逐表行数一致、重试幂等、快照替换、签名);
- ingest v3:`tests/contract/test_sink_ingest.py` 与 `tests/contract/test_ingest_protocol_compat.py`;
- AI Hub:`tests/contract/test_ai_hub_object_push_sink.py`(进程内 mock)。

接单评审等 Agent 场景随平台端移出本仓。

## 5. 相关

- fixtures 源码:`tests/fixtures/e10/`
- MSSQL 集成:`tests/integration/mssql/`
- 迁移核对:[2026-07-24-m7-showroom-migration-checklist.md](../superpowers/plans/2026-07-24-m7-showroom-migration-checklist.md)
