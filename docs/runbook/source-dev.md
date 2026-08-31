# 开发者本地接入指南

适用场景:开发者在本地搭建 E10-like 参考链,或开发新的 ERP 适配器。

> **部署形态说明**:本指南的 `sink: local`(不写 `sink` 即默认)是**内部开发/参考链/
> 测试专用**,不是交付形态。生产部署是跨机推送:
> 对接 **data2agent 平台** 用 `sink: { type: http, ... }`(本文 §6);
> 对接 **AI Hub** 必须用 `sink: { type: ai_hub, ... }`(本文 §6.1),
> 禁止把 http URL 改成 AI Hub。现场本仓平台安装走
> [便携包](portable.md);AI Hub 路径见 [push-validation](push-validation.md)。

## 1. 前置条件

| # | 确认项 |
| --- | --- |
| 1 | Python 3.11+ 已安装 |
| 2 | `pip install -e ".[dev,mcp,console,ingest,connect,middle_admin,excel]"` 已执行 |
| 3 | 参考 seed 已生成:`python -m tests.fixtures.e10.seed --db /tmp/e10.sqlite`（测试资产；非产品运行模式） |
| 4 | (可选) Docker 已安装,用于 SQL Server 集成测试 |

## 2. connect.yaml 最小配置

复制仓库根 `connect.example.yaml` 为 `connect.yaml`。生产新安装默认 `tables: {}`。
本地参考链可显式列出基线表:

```yaml
templates: templates
landing: landing/factory.sqlite

sources:
  digiwin_e10:
    adapter: sqlite_readonly
    path: /tmp/e10.sqlite
    # 生产: adapter: mssql_readonly + dsn_env: D2A_E10_DSN

    tables:
      CUSTOMER:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      CURRENCY:
        mode: full_refresh
      ITEM:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      QUOTATION:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      SALES_ORDER:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      SALES_ORDER_D:
        mode: incremental
        watermark: LAST_MODIFIED_DATE

    windows: []
    rate: { batch_size: 5000, rows_per_second: 2000 }
    lookback: 3d
    sync_every: 30m
    apply_after_sync: true
```

## 3. CLI 命令

```bash
# 验证配置可加载
python -c "from data2agent.middle.extract.config import load_config; load_config('connect.yaml'); print('OK')"

# 单次抽取(策略来自 tables;无 --full)
python -m data2agent.middle.extract sync --config connect.yaml

# 常驻调度;--once 立即跑一轮后退出
python -m data2agent.middle.extract serve --config connect.yaml --once

# 查看同步状态
python -m data2agent.middle.extract status
```

`serve` 常驻后按 `sync_every` 周期调度。修改 `connect.yaml` 后需重启服务才能生效。

## 4. 抽取表与元数据

- **唯一事实来源**:`tables`。未声明的表不会被抽取。
- **`mode: incremental`**:必须 `watermark`；可选 `key_columns`（覆盖 DB PK，支持复合键）。
- **`mode: full_refresh`**:快照 staging → 原子发布；禁止 `watermark`；源端删除行会从 raw 消失。
- **中间机 UI**:`/config` 只管连接；`/metadata` 扫描选表；`/tables` 确认并保存。
- 本地也可用 middle_admin 对 sqlite 配置做页面冒烟（见 `scripts/smoke_admin_ui.py`）。

## 5. 本地元数据扫描

```bash
# 单元/API 测试（不依赖真实 SQL Server；在仓库根目录执行）
.venv/bin/python -m pytest tests/middle/test_metadata_discoverer.py tests/middle/test_middle_metadata_api.py -q

# 真实 SQL Server：在仓库根目录启动 compose（会 seed 并跑 tests/integration/mssql）
docker compose -f tests/integration/mssql/docker-compose.yml up \
  --build --abort-on-container-exit --exit-code-from runner

# 若已有外部 MSSQL 并设置了 D2A_IT_MSSQL_DSN / D2A_IT_MSSQL_SA_DSN，也可在仓库根目录：
.venv/bin/python -m pytest tests/integration/mssql/ -q
```

门控环境变量未设置时，直接跑 pytest 会 skip 集成用例；compose 路径会注入 DSN 并实际执行。
## 6. HTTP 推送模式

```yaml
sources:
  digiwin_e10:
    # 本机参考链专用:回环地址仅在 deployment_mode 非 production 时合法。
    # 现场两台机器部署必须填平台机地址,例如 http://192.168.1.20:8850;
    # 生产模式下回环 sink 会被 readiness 判为违规并拒绝启动 connector。
    sink: { type: http, url: "http://127.0.0.1:8850", token_env: D2A_INGEST_TOKEN }
    apply_after_sync: false
```

同步前中间机确认自身发送协议落在平台 `supported_ingest_protocol_versions` 中
（当前发送 ingest v3,平台同时接受 v2/v3）；不在列表则立即失败。现场升级策略见 [portable.md](portable.md)。

## 6.1 AI Hub PUSH_AGENT(C1-B,本仓已落地)

对接 AI Hub 不得使用 §6 的 `HttpPushSink`。C1-B 适配器、加密 spool 与 mock 契约已在本仓;
跨仓联调与生产启用属 C1-C。最小配置形状:

```yaml
sources:
  digiwin_e10:
    apply_after_sync: false
    spool:
      policy: encrypted_temp_volume
      directory: /secure/aihub-spool   # 须为现场确认静态加密的专用目录
      encrypted_at_rest: true
    sink:
      type: ai_hub
      url: "https://ai-hub.example"
      source_application_id: e10-adapter
      oidc_token_url: "https://identity.example/application/o/token/"
      oidc_client_id: e10-adapter
      oidc_client_secret_env: D2A_AI_HUB_CLIENT_SECRET
    tables:
      ITEM:
        mode: full_refresh
        object_type: erp.item
        payload_contract_version: item.v1
        payload_schema_fingerprint: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        payload_columns: [ITEM_CODE, ITEM_NAME]
```

开发可用 mock server 契约测试(`tests/contract/test_ai_hub_object_push_sink.py`),
不连接真实 AI Hub。`ai_hub` **必须** `spool.policy=encrypted_temp_volume`(禁止
`strict_stream` / `temporary_file`);spool 按 `source_application_id` 摘要分子目录,
批次文件名为可移植摘要,读写删除均校验路径落在配置根内。崩溃恢复时先排空 pending,
再比较内容摘要:未变则跳过,已变则作为下一序号发送。
AI Hub 侧 `DATA_INGEST_PUSH_ENABLED` 默认关闭,且变更日志 purpose 唯一约束未切
contract 前写入 API 仍关闭;跨仓联调与按来源启用属 C1-C。
`ai_hub` **禁止**配置 `reconcile_at` / `reconcile_deep_at`,定时与手工对账都会被拒绝
(无远端对账协议,且不得对中间机 state_db 做本地 raw 对账)。
`deployment_mode: production` 在 C1-C 前拒绝 `ai_hub`;对象版本写入中间机 `state_db`
表 `d2a_aihub_object_version`,随状态库备份/恢复。

## 7. 常见问题

**Q: 新增抽取表后是否需要重启?**
需要。当前版本 `serve` 不会自动感知 `connect.yaml` 变更。

**Q: 删除 tables 中的表会导致数据丢失吗?**
不会。已落地的 raw 保留，只是后续不再抽取该表。

**Q: 如何确认 watermark / 业务键?**
在 `/tables` 保存前会做现场校验；也可调用
`POST /api/extraction-tables/validate`。字典中的字段名仅为参考形状。

**Q: 真实 SQL Server 如何配置?**

```yaml
adapter: mssql_readonly
dsn_env: D2A_E10_DSN
tables: {}   # 再用 UI 或手写填入
```

ODBC 连接串只通过环境变量注入。
