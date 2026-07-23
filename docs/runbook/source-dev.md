# 开发者本地接入指南

适用场景:开发者在本地搭建 E10-like 参考链,或开发新的 ERP 适配器。

## 1. 前置条件

| # | 确认项 |
| --- | --- |
| 1 | Python 3.10+ 已安装 |
| 2 | `pip install -e ".[dev,mcp]"` 已执行 |
| 3 | 参考 seed 已生成:`python -m data2agent.showroom.seed` |
| 4 | (可选) Docker 已安装,用于 SQL Server 集成测试 |

## 2. connect.yaml 最小配置

复制仓库根 `connect.example.yaml` 为 `connect.yaml`,按需修改。以下为本地 SQLite 参考库的最小配置:

```yaml
templates: templates
landing: landing/factory.sqlite

sources:
  digiwin_e10:
    adapter: sqlite_readonly
    path: showroom/e10.sqlite

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
# 验证配置(测试连接、验证 PK 和 watermark 列)
python -m data2agent.connect test --config connect.yaml

# 单次抽取(增量)
python -m data2agent.connect sync --config connect.yaml

# 全量重抽
python -m data2agent.connect sync --config connect.yaml --full

# 映射:raw_* → 物化对象层
python -m data2agent.connect apply --config connect.yaml

# 常驻调度(窗口内定时抽取 + 自动 apply)
python -m data2agent.connect serve --config connect.yaml

# 验证配置后立即运行一轮(调试用)
python -m data2agent.connect serve --config connect.yaml --once

# 查看同步状态
python -m data2agent.connect status --config connect.yaml
```

`--config` 默认值为 `connect.yaml`(当前目录)。多源或多环境时通过 `--config` 指定不同配置文件。

## 4. 抽取表管理

`tables` 字段是**抽取范围的唯一事实来源**:

- **`mode: incremental`**:增量抽取,必须同时配置 `watermark` 字段名。每次抽取拉取 `WHERE watermark_col > last_watermark` 的行。
- **`mode: full_refresh`**:全量刷新,每次抽取替换整表数据。适用于无可靠水位字段的小维表(如 CURRENCY)。
- 未在 `tables` 中声明的表不会被抽取。
- 表清单独立于平台模板维护 —— 中间机只管"抽什么",平台模板只管"怎么映射"。

## 5. HTTP 推送模式

中间服务器推送到数据平台的模式(Pattern A):

```yaml
sources:
  digiwin_e10:
    # ... 同上的 adapter / tables 配置 ...
    sink: { type: http, url: "https://平台域名", token_env: D2A_INGEST_TOKEN }
    apply_after_sync: false   # HTTP 推送模式下忽略,由平台侧负责 apply
```

HTTP 推送模式下:
- 中间机不执行 `apply`,只负责抽取 → 推送 raw 数据;
- 平台侧接收后在本地执行物化映射;
- 中间机不需要 `templates` 目录(模板在平台侧维护),但 `templates` 字段在 `connect.yaml` 顶层仍建议填写以支持本地 `validate` 命令。

## 6. 配置迁移

如果从旧版 `whitelist_from_bindings` / `extra_whitelist` 配置迁移:

```bash
python -m data2agent.connect migrate-config --config connect.yaml
```

该命令读取旧字段,结合模板 binding 推断表清单,生成新的 `tables` 段落并移除旧字段。

## 7. 常见问题

**Q: 新增抽取表后是否需要重启?**
不需要。`serve` 常驻模式在下个窗口自动感知 `connect.yaml` 变更。手动模式直接运行 `sync` 即可。

**Q: 删除 tables 中的表会导致数据丢失吗?**
不会。已落地的 raw 数据保留在 landing 库中,只是后续不再抽取该表。

**Q: 如何确认 watermark 字段是否正确?**
运行 `connect test` 会逐表验证 watermark 列是否存在。字段语义(修改时间 vs 审核时间)属现场核对项,参考字典中的字段名仅为参考形状。

**Q: 真实 SQL Server 环境如何配置?**
将 `adapter` 改为 `mssql_readonly`,删除 `path`,改用 `dsn_env` 指向环境变量:

```yaml
adapter: mssql_readonly
dsn_env: D2A_E10_DSN
```

ODBC 连接串通过环境变量注入,绝不写入配置文件。
