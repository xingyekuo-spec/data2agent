# 06 · 中间机独立抽取表配置

> 状态:**已实现**(v0.5 / M0–M5)· 最近修订:2026-07-24
>
> 本文描述当前生产行为。历史「待实施」方案细节已收敛到代码与
> [ERP 元数据实施计划](../superpowers/plans/2026-07-23-erp-metadata-extraction-management.md)。

## 1. 目标

1. `sources.<source>.tables` 是抽取范围与每表同步策略的唯一配置来源。
2. 已删除 `whitelist_from_bindings`、`extra_whitelist` 以及 `migrate-config` CLI。
3. 适配器白名单 = `set(tables.keys())`；未声明表一律拒绝。
4. 新安装默认 `tables: {}`，同步不访问任何 ERP 业务表。
5. 表清单不随包携带；现场经元数据扫描确认后写入 `connect.yaml`。

## 2. 页面职责

| 页面 | 职责 |
| --- | --- |
| `/config` | ERP/平台连接、调度窗口、限流；**不含**抽取表编辑器 |
| `/metadata` | 只读扫描 ERP 表结构；「加入抽取计划」写入本机草稿 |
| `/tables` | 确认模式、业务键、水位；校验差异后原子保存整份 `tables` |
| `/status` | 连接 / 配置 / 运行三层；空表时提示去元数据选表 |

推荐首次流程：

```text
配置连接并测连 → /metadata 扫描选表 → /tables 确认键与水位 → 保存 → 重启 connector
```

## 3. 配置模型

```yaml
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN
    tables: {}   # 新安装默认；选表后写入如下
    # CUSTOMER:
    #   mode: incremental
    #   schema: dbo
    #   key_columns: [CUSTOMER_CODE]   # 可选，覆盖 DB PK
    #   watermark: LAST_MODIFIED_DATE
    # CURRENCY:
    #   mode: full_refresh             # 快照原子替换，禁止 watermark
```

约束：

- `incremental` 必须有 `watermark`；`full_refresh` 禁止 `watermark`。
- `key_columns` 可选；未配置时使用数据库主键；无键且未配置则增量失败。
- 旧字段 `whitelist_from_bindings` / `extra_whitelist` 加载即拒绝。
- 保存抽取计划必须现场元数据校验（`live=True`），不可由客户端关闭。

## 4. API

- `GET /api/extraction-tables` — 当前计划与 revision
- `POST /api/extraction-tables/validate` — 预览校验（可 `live:false` 仅结构）
- `PUT /api/extraction-tables` — 原子替换；强制现场校验 + revision 乐观锁
- `POST /api/metadata/scans` 等 — 只读元数据发现（见 middle_admin）

## 5. 同步语义

- 增量：配置/复合运行键 + 水位 keyset；水位仅来自 `tables`。
- 全量：`full_refresh` 经 staging → 原子发布；源端删除行从 raw 消失。
- CLI `sync` **不再**接受 `--full`；运行模式只来自逐表配置。
- HTTP 推送前校验平台 `ingest_protocol_version`，不一致 fail-fast。

## 6. 安全与运维

- 页面与 API 不回显密码、DSN、Token 明文。
- 元数据扫描只读、脱敏、可超时；不把完整元数据持久化到产品包。
- 字典文档（如 `docs/dict/digiwin_e10.md`）仅供参考，不参与运行时抽取配置。

## 7. 相关文档

- [02 抽取框架](02-extraction.md)
- [便携包部署](../runbook/portable.md)
- [推送链路验收](../runbook/push-validation.md)
- [实施计划](../superpowers/plans/2026-07-23-erp-metadata-extraction-management.md)
