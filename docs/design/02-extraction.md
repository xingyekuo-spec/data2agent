# 02 · 抽取框架(详设)

> 状态:详设 v0.1(2026-07-10),指导第②阶段编码 · 实现目录:`data2agent/connect/`(依赖组 `connect` 已预留:pyodbc / apscheduler / psycopg)

## 1. 目标与非目标

**目标**:把源系统数据只读地、增量地、可审计地搬到本地落地库,再按元模型 binding 映射成业务对象层,供 MCP 网关消费。设计基线是"装进客户内网、碰生产 ERP 库",因此安全机制全部是代码强制。

**非目标**(明确不做,避免范围蔓延):

- CDC / binlog / 实时流 —— 制造业经营场景 T+1 到小时级足够,轮询即可;
- 写回源系统 —— 网关"做"档位是另一条通路(docs 03),抽取永远只读;
- 跨源 join / 主数据对齐 —— 落地层各源独立,对齐属商业版;
- 调度 UI / 监控面板 —— 开源提供 CLI 与结构化日志,面板归商业版。

## 2. 分层与数据流

```
SourceAdapter(mssql / sqlite / api_poll / excel)      ── 只读、白名单、限流、审计
   │ read_increment(table, since, lookback) → 行批次
   ▼
Landing 原样落地(raw_{source}__{table},按源主键 upsert)
   │                          ├─ increment:水位状态机
   │                          ├─ reconcile:分段对账(L1/L2)
   ▼                          └─ quarantine:隔离区
mapping_apply:binding → 对象视图 obj_{object}(复用 data2agent/mapping.py)
   ▼
MCP 网关改读对象视图(现直读展厅库,属过渡形态)
```

关键取舍:**ELT 而非 ETL** —— 源数据原样落地、不做任何转换,映射在落地库内以视图/物化表完成。理由:① 源侧只跑最简单的 SELECT,把对生产库的压力和停留时间压到最低;② 映射错了改 binding 重建视图即可,不必重抽;③ 落地层原样数据是对账和审计的证据。

## 3. 适配器层(adapters/)

```python
class SourceAdapter(Protocol):
    def tables(self) -> list[TableInfo]                  # 仅白名单内
    def read_increment(self, table, since, watermark_col) -> Iterator[RowBatch]  # since 由增量引擎算好(已含回看)
    def segment_stats(self, table, segment) -> SegmentStat   # L1 对账:COUNT + MAX(水位)
```

| 适配器 | 场景 | 说明 |
| --- | --- | --- |
| `mssql_readonly` | 鼎捷 E10 / 易飞(底层均为 SQL Server) | pyodbc;首个生产适配器 |
| `sqlite_readonly` | 开发 / 展厅 / 测试 | 与 mssql 行为等价,mode=ro |
| `api_poll` | 有 API 的源(SRM 等) | 仅定义接口,按需实现 |
| `excel_import` | 报价历史 Excel(MVP) | 一次性导入进落地层,同一下游 |

**安全强制(适配器层内实现,不可绕过)**:

1. **只读**:只允许 SELECT;连接串要求只读账号,MSSQL 加 `ApplicationIntent=ReadOnly`;任何非 SELECT 语句直接抛异常;
2. **白名单**:默认由模板 binding 的 `tables` 自动推导(元模型再次作为唯一事实来源),配置可追加 `extra_whitelist`;白名单外的表名一律拒绝;
3. **限流**:`batch_size`(默认 5000)+ `rows_per_second` 节流 + 语句超时;
4. **审计**:发往源库的每一条 SQL 记入 `d2a_audit_log`(语句、行数、耗时、批次号)。

## 4. 落地层(landing.py)

- 表名:`raw_{source}__{table}`(如 `raw_digiwin_e10__SALES_ORDER`);
- 列:源列原样(类型收敛到 int/real/text/blob 四类,值不转换)+ 元数据列:

| 元数据列 | 含义 |
| --- | --- |
| `_d2a_batch_id` | 写入批次(关联审计与隔离区) |
| `_d2a_extracted_at` | 抽取时间 |
| `_d2a_row_hash` | 行内容 md5(规范化序列化后),对账 L2 依据 |
| `_d2a_deleted_at` | 软删标记(对账发现源侧消失时打标,永不物理删) |

- 写入语义:按源主键 **upsert**(E10 为 `Id`),幂等 —— 回看窗口和对账重抽天然安全;
- 落地库:MVP 支持 SQLite(零依赖,开发/展厅)与 PostgreSQL(生产推荐,psycopg);统一走参数化 SQL 的薄封装,不引 ORM。

## 5. 增量协议(increment.py)

状态表 `d2a_sync_state(source, table, watermark_col, high_water, last_run_at, last_batch_id)`。

单表一轮增量:

```
since = high_water - lookback          # 回看窗口,默认 3 天,吸收迟到更新与时钟偏差
按 (watermark, pk) keyset 分页拉取 → 逐批落地(upsert)→ 批次提交
全部批次成功后:high_water = max(watermark of committed rows)
```

规则:

- 水位**只在落地事务提交后前进**,且只前进不后退;任何批次失败,水位停在原地,下轮重来(upsert 幂等);
- 无可靠水位字段的表(小维表如 CURRENCY):全量刷新策略,配置 `strategy: full_refresh`;
- 水位字段语义(是"修改时间"还是"审核时间")属现场核对项,binding `watermark` 为准。

## 6. 分段对账(reconcile.py)

水位增量抓不住两类漂移:源侧物理删除、不更新水位的原地改动。对账按水位分段(默认自然月)两档:

- **L1(廉价,每日跑)**:源侧 `SELECT COUNT(*), MAX(wm)` per 段 vs 落地侧同口径。不一致 → 该段标记待修复;
- **L2(修复,对不一致段)**:重抽该段(upsert 幂等),按 `_d2a_row_hash` 与源主键集合 diff:源侧消失的行打 `_d2a_deleted_at`。

刻意不用 MSSQL `CHECKSUM_AGG` 之类源侧哈希:各源实现不一致,无法与落地侧比对;L1 用 COUNT+MAX 这种任何源都便宜且语义一致的指标。

## 7. 隔离区(quarantine.py)

位置在**映射阶段**(落地是原样的,基本不会失败;映射才有类型/口径约束):

- 触发:类型转换失败、业务键缺失/重复、`map` 遇到未声明的源码值、ref 解析失败;
- 动作:整行(原样 JSON + 原因 + 批次号)进 `d2a_quarantine`,批次继续 —— 单行坏数据不阻塞管道;
- **熔断**:单批次隔离率超过阈值(默认 5%)→ 中止本批并告警,防止系统性口径错误(比如源表结构变了)被静默吞掉;
- 处理:`d2a quarantine list / retry`(修 binding 后重放),复核界面归商业版。

## 8. 调度与运行(scheduler.py + __main__.py)

- apscheduler 按源调度;**错峰窗口**(如 `windows: ["22:00-06:30"]`)硬约束:窗口外不发起,运行中越界则在批次边界优雅暂停、下窗口续跑(水位机制天然支持断点);
- CLI(`python -m data2agent.connect`):`sync --source X [--once]` / `backfill --table T --from --to` / `reconcile [--deep]` / `status` / `quarantine list|retry`;
- 每轮汇总进 `d2a_sync_run`(起止、行数、隔离数、对账结果),结构化日志输出。

## 9. 配置(connect.yaml)

```yaml
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN            # 凭据只从环境变量读,绝不落配置文件/仓库
    whitelist_from_bindings: true
    extra_whitelist: []
    windows: ["22:00-06:30"]
    rate: { batch_size: 5000, rows_per_second: 2000 }
    lookback: 3d
    tables:
      CURRENCY: { strategy: full_refresh }
landing:
  dsn_env: D2A_LANDING_DSN          # 或 sqlite: landing/factory.sqlite
```

## 10. 安全承诺 → 机制对照(README 承诺的逐条落地)

| 承诺 | 机制 | 层 |
| --- | --- | --- |
| 只读账号 | 仅 SELECT + ReadOnly intent,非 SELECT 抛异常 | 适配器 |
| 白名单表 | binding 推导 + 白名单外拒绝 | 适配器 |
| 限时 | 错峰窗口硬约束,越界批次边界暂停 | 调度 |
| 限流 | batch_size + rows_per_second + 语句超时 | 适配器 |
| 全部可审计 | d2a_audit_log 逐条 SQL + d2a_sync_run 逐轮汇总 | 全层 |

## 11. 实施切片(每片带测试,可独立合入)

| 切片 | 内容 | 验证方式 |
| --- | --- | --- |
| E1 | 适配器接口 + sqlite/mssql 适配器 + 落地层(全量) | 展厅 SQLite;容器化 MSSQL 灌入同一份 seed 数据跑集成测试 |
| E2 | 水位增量 + 回看 + keyset 分页 | seed 数据改动若干行,断言只拉增量、水位正确前进 |
| E3 | 分段对账 L1/L2 + 软删 | 源侧删行/改行不动水位,断言对账修复 |
| E4 | 映射应用(binding→对象视图)+ 隔离区 + 熔断 | 注入坏行断言隔离;MCP 网关切换到对象视图 |
| E5 | 调度 + 窗口 + 限流 + CLI + 审计完备 | 窗口/限流行为测试;E10 真实环境预演清单 |

## 附录 · 现场核对清单(进厂当天用)

按对象逐 binding 确认并置 verified:① 表名与我们参考表形的差异;② 每个 field_map 字段的真实字段名;③ 水位字段语义(修改时间?审核时间?时区?);④ 状态码全集(map 是否遗漏取值);⑤ 业务键唯一性实测;⑥ 只读账号 + 白名单授权是否就位;⑦ 允许的抽取窗口与限流上限(和 IT 书面确认)。
