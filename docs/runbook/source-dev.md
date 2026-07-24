# 开发者本地接入指南

适用场景:开发者在本地搭建 E10-like 参考链,或开发新的 ERP 适配器。

## 1. 前置条件

| # | 确认项 |
| --- | --- |
| 1 | Python 3.11+ 已安装 |
| 2 | `pip install -e ".[dev,mcp,console,ingest,connect,middle_admin,excel]"` 已执行 |
| 3 | 参考 seed 已生成:`python -m data2agent.showroom.seed`（测试资产；非产品运行模式） |
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
    path: showroom/e10.sqlite
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
python -c "from data2agent.connect.config import load_config; load_config('connect.yaml'); print('OK')"

# 单次抽取(策略来自 tables;无 --full)
python -m data2agent.connect sync --config connect.yaml

# 常驻调度;--once 立即跑一轮后退出
python -m data2agent.connect serve --config connect.yaml --once

# 查看同步状态
python -m data2agent.connect status
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
# 单元/API 测试（不依赖真实 SQL Server）
.venv/bin/python -m pytest tests/test_metadata_discoverer.py tests/test_middle_metadata_api.py -q

# 真实 SQL Server（需 compose）
cd tests/integration/mssql
# 见该目录 docker-compose.yml 头注释设置 D2A_IT_MSSQL_DSN
.venv/bin/python -m pytest tests/integration/mssql/ -q
```

门控环境变量未设置时，MSSQL 集成测试自动 skip。

## 6. HTTP 推送模式

```yaml
sources:
  digiwin_e10:
    sink: { type: http, url: "http://127.0.0.1:8850", token_env: D2A_INGEST_TOKEN }
    apply_after_sync: false
```

同步前中间机会校验平台 `ingest_protocol_version`（当前为 `"2"`），不一致立即失败。

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
