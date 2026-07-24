# 02 · 抽取框架(详设)

> 状态:设计修订 r0.8(2026-07-24)· 实现目录:`data2agent/connect/` + `data2agent/ingest/` · 当前:E1–E5 + E6a 推送 sink + v0.3 原子发布、映射 Preview、字段血缘已实现;v0.5 ERP 元数据发现、配置业务键、复合键增量、`full_refresh` 快照原子替换与中间机 `/metadata` `/tables` 已落地;v0.4 批次回执/E6b/TLS 门槛待建
> 上层基线:[路线图](../roadmap.md)

## 1. 目标与非目标

**目标**:把源系统数据只读地、增量地、可审计地搬到本地落地库,再按元模型 binding 映射成业务对象层,供 MCP 网关消费。设计基线是"装进客户内网、碰生产 ERP 库",因此安全机制全部是代码强制。

**非目标**(明确不做,避免范围蔓延):

- CDC / binlog / 实时流 —— 制造业经营场景 T+1 到小时级足够,轮询即可;
- 写回源系统 —— 网关"做"档位是另一条通路(docs 03),抽取永远只读;
- 跨源 join / 主数据对齐 —— 落地层各源独立,对齐为后续能力;
- 调度编排 UI —— 调度只经 connect.yaml + CLI 配置;运行监控与复核由
  运维控制台提供(docs 05)。

## 2. 分层与数据流

```
SourceAdapter(mssql / sqlite / api_poll / excel)      ── 只读、白名单、限流、审计
   │ read_increment(table, since, lookback) → 行批次
   │ （full_refresh: 快照 staging → 原子发布，删除行从 raw 消失）
   ▼
Landing 原样落地(raw_{source}__{table})
   │                          ├─ increment:水位 + 配置/复合运行键 keyset
   │                          ├─ reconcile:分段对账(L1/L2)
   ▼                          └─ quarantine:隔离区
mapping_apply:binding → 候选物化表 objv_<opaque>_* → 数据集原子发布(见 §7.2)
   ▼
MCP / Console / 指标在同一读事务内解析 PublishedDatasetSnapshot(不回退遗留 obj_*)
```

关键取舍:**ELT 而非 ETL** —— 源数据原样落地、不做任何转换,映射在落地库内以物化表完成。理由:① 源侧只跑最简单的 SELECT,把对生产库的压力和停留时间压到最低;② 映射错了改 binding 后重建候选并原子发布即可,不必重抽;③ 落地层原样数据是对账和审计的证据。

> 上图是**逻辑管道**(与部署位置无关)。现场的**物理部署**会把它拆到"中间服务器 + 数据平台"两台机器上 —— 见 §12 部署拓扑。

运行键优先级:配置 `key_columns`(可覆盖数据库 PK,支持复合键) → 否则数据库主键。无键且未配置 `key_columns` 时增量失败;`full_refresh` 可无主键,经快照协议整表替换。
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
| `sqlite_readonly` | 开发 / 参考库 / 测试 | 与 mssql 行为等价,mode=ro |
| `api_poll`(规划) | 有 API 的源(SRM 等) | 尚未实现;按需拉动,待首个真实 API 源出现再定义接口 |
| `excel_import`(非 SourceAdapter,CLI 导入流程,见 `connect/excel_import.py`) | 报价历史 Excel/CSV(MVP,已实现) | 三步工作流:`excel-suggest` 启发式建议列映射(YAML)→ 人工确认一次 → `excel-import` 快照落地(raw 表结构由 binding 契约决定,文件缺列落 NULL、缺业务键跳过并逐行报告、重导入按业务键幂等)→ 标准 apply 物化,校验 / 隔离 / 熔断原样生效;值翻译写 binding 的 (map ...) |

**安全强制(适配器层内实现,不可绕过)**:

1. **只读**:只允许 SELECT;连接串要求只读账号,MSSQL 加 `ApplicationIntent=ReadOnly`;任何非 SELECT 语句直接抛异常;
2. **表白名单**:由 `connect.yaml` 每源的 `tables` 字段显式声明抽取表及策略(`mode` / `watermark`);未声明表名一律拒绝;
3. **限流**:`batch_size`(默认 5000)+ `rows_per_second` 节流 + 语句超时;
4. **审计**:发往源库的每一条 SQL 记入 `d2a_audit_log`(语句、行数、耗时、批次号)。

## 4. 落地层(landing.py)

- 表名:`raw_{source}__{table}`(如 `raw_digiwin_e10__SALES_ORDER`);
- 列:源列原样(类型收敛到 int/real/text/blob 四类,值不转换)+ 元数据列:

| 元数据列 | 含义 |
| --- | --- |
| `_d2a_batch_id` | 写入批次(关联审计与隔离区) |
| `_d2a_extracted_at` | 抽取时间 |
| `_d2a_row_hash` | 行内容 md5(规范化序列化后);落地时写入,当前对账未消费(预留:E6b 跨机对账时中间只推 hash、平台比对后仅回差异行) |
| `_d2a_deleted_at` | 软删标记(对账发现源侧消失时打标,永不物理删) |

- 写入语义:按源主键 **upsert**(E10 为 `Id`),幂等 —— 回看窗口和对账重抽天然安全;
- 落地库:当前实现为 **SQLite**(零依赖、单文件、零运维),覆盖开发、参考链和首个工厂试点候选路径。能否用于正式试点取决于 v0.4 对容量、并发、WAL checkpoint、备份与恢复的量化基线,不能仅凭“单写多读”直接宣告生产适用。当前访问模式是 connector/ingest/apply 写入 + MCP/控制台读取;必须验证实际多进程写争用。`LandingStore` 初始化开启 `journal_mode=WAL` + `busy_timeout`。
- 并发上界(需换库的信号):平台托管**多源 / 多工厂并发写同一落地库**、落地库需**跨机远程访问**、或 Agent 读并发大到单文件读锁成瓶颈 —— 任一出现再切 PostgreSQL(装 `.[postgres]` extra 引入 psycopg;当前 `connect`/`ingest` 不含它)。因落地库不是数据主体(ERP 才是),切库是一次性倒库 / `backfill` 重抽 + 一遍 `apply` 重建对象层,非持续迁移负担。
- 迁移面控制:方言用法(`INSERT ... ON CONFLICT`、`PRAGMA`、`file:...?mode=ro`)集中在 `landing.py` 与各读取方的 `_connect`,统一参数化 SQL 薄封装、不引 ORM,把将来 PG 切片的改造面圈在可控范围。

## 5. 增量协议(increment.py)

状态表 `d2a_sync_state(source, table_name, watermark_col, high_water, last_run_at, last_batch_id)`。

单表一轮增量:

```
since = high_water - lookback          # 回看窗口,默认 3 天,吸收迟到更新与时钟偏差
按 (watermark, pk) keyset 分页拉取 → 逐批落地(upsert)→ 批次提交
全部批次成功后:high_water = max(watermark of committed rows)
```

规则:

- 水位**只在落地事务提交后前进**,且只前进不后退;任何批次失败,水位停在原地,下轮重来(upsert 幂等);
- 无可靠水位字段的表(小维表如 CURRENCY):走全量快照替换 —— 由 `connect.yaml` 每表的 `mode: full_refresh` 显式声明;增量表须同时配置 `watermark` 字段名,并建议现场确认 `key_columns`;
- `full_refresh` 经 staging 表写入后原子发布到 raw,源端已删除行不会残留;失败则 abort staging,保留上一完整快照;
- 水位字段语义(是"修改时间"还是"审核时间")属现场核对项,以 `connect.yaml` 每表配置的 `watermark` 为准。
## 6. 分段对账(reconcile.py)

水位增量抓不住两类漂移:源侧物理删除、不更新水位的原地改动。对账按水位分段(默认自然月)两档:

- **L1(廉价,每日跑)**:源侧 `SELECT COUNT(*), MAX(wm)` per 段 vs 落地侧同口径。不一致 → 该段标记待修复;
- **L2(修复,对不一致段或 `--deep` 全段)**:重抽该段并 upsert —— 原地改动被覆盖自然修正;再按**源侧主键全集与落地侧活跃主键集合 diff**,源侧消失的主键打 `_d2a_deleted_at` 软删。不逐行比 `_d2a_row_hash`:整段既已重抽 upsert,hash diff 结果等价却更复杂,本地 upsert 便宜不值得。

已知盲区:原地改动若**不动水位**,L1 的 COUNT+MAX 察觉不到、不会标记该段,L2 无从触发 —— 只能靠 `--deep` 全段重抽兜底(用不用 hash 都一样)。

刻意不用 MSSQL `CHECKSUM_AGG` 之类源侧哈希:各源实现不一致,无法与落地侧比对;L1 用 COUNT+MAX 这种任何源都便宜且语义一致的指标。

## 7. 隔离区(映射阶段,实现于 mapping_apply.py + landing.py)

位置在**映射阶段**(落地是原样的,基本不会失败;映射才有类型/口径约束):

- 触发:类型转换失败、业务键缺失/重复、`map` 遇到未声明的源码值;
- **ref 解析失败暂不隔离**:多对象有同步先后,子对象先于锚对象落地时外键短暂悬空是正常态,且"外键悬空"与"外键本身为空"在解码后无法区分 —— 强行隔离会大量误伤甚至误触熔断;兜底靠上游对账与下游指标口径警示;
- 动作:整行(原样 JSON + 原因 + 批次号)进 `d2a_quarantine`,批次继续 —— 单行坏数据不阻塞管道;
- **熔断**:单批次隔离率超过阈值(默认 5%)→ 中止本批并告警,防止系统性口径错误(比如源表结构变了)被静默吞掉;
- 处理:`quarantine list / retry` CLI,或运维控制台(docs 05)的复核与一键重试。

### 7.1 v0.3 映射 Preview 与字段血缘(已实现)

Preview 与正式 apply 共用 `mapping_transform` 纯转换核心(field → map → enum → 类型 → derived → 业务键),但必须满足:

- 输入为选定 raw 样本(有界 offset/limit/batch)和/或一次性临时 binding 草稿;
- 在 `LandingStore.open_readonly()` 读事务内冻结锚表主键样本,current/draft 双跑同一指纹;
- 输出仅包含预览结果、结构化隔离原因、枚举未覆盖值、业务键问题、derived 覆盖率与字段 diff;
- 不修改 published 物理表、水位、正式隔离区、Run、模板或当前数据集版本;唯一允许写入是 access audit;
- 返回 `template_version`、current/candidate `binding_hash`、raw batch 与 sample fingerprint,保证预览可复现;
- 强制 Bearer;敏感属性与 current∪draft 敏感 raw 列并集服务端遮罩;未分类 raw 列与 `derived_unmatched` 源值不回传明文。

字段血缘由正式 apply 产生,至少记录:

```text
dataset_version / object / object_key / property
source / source_table / source_pk / source_column / source_value
transform(map/join/derived) / result_value / extract_batch_id / map_batch_id
```

敏感源值遵循出口脱敏规则,不能因血缘接口绕过 `sensitive`。字典/字段语义是否正确仍需
现场核对;血缘只证明系统实际读取和转换了什么。实现必须复用同一转换评估模型,不得复刻第二套转换器。

### 7.2 v0.3 对象层原子发布(已实现 · M2)

发布模型是“不可变物理版本表 + 原子元数据指针”,不是重命名共享 `obj_*`:

```text
为全部目标对象构建不可变 objv_<opaque>_* 候选表
  → 完整校验与熔断判断(冻结 object_manifest + template_snapshot)
  → 任一关键对象失败则全体不发布,当前 published 元数据/物理表保持不变
  → SQLite 短事务内只切换 published 元数据指针(不在临界区建表/删表/改名)
  → 保留 current + immediate previous;rollback 仅允许 previous_dataset_version
```

元数据至少包含 `dataset_version / object_version / template_version / binding_hash / source / built_at / published_at / status / previous_dataset_version`。MCP、指标和 Console 对象读取在同一读事务内解析同一 `PublishedDatasetSnapshot`。遗留 `obj_*` 不是 published 事实源,升级后不得伪造版本;无 published 元数据时版本化读取 fail-closed。raw 已更新但对象尚未发布时,控制台和 MCP metadata 必须显示 `stale`/旧版本,不能伪装成最新。

## 8. 调度与运行(scheduler.py + __main__.py)

- apscheduler 按源调度;**错峰窗口**(如 `windows: ["22:00-06:30"]`)硬约束:窗口外不发起,运行中越界则在批次边界优雅暂停、下窗口续跑(水位机制天然支持断点);
- CLI(`python -m data2agent.connect`):`sync --config <connect.yaml> [--source <name>]`(抽取范围/策略仅来自 `tables`,不再接受 `--full`)/ `apply` / `backfill --config ... --table --from --to` / `reconcile --config ... [--deep]` / `serve --config ... [--once]`(常驻调度)/ `status` / `quarantine list|retry` / `excel-suggest` / `excel-import`;已删除 `migrate-config` 与 CLI 全量覆盖入口;
- 每轮汇总进 `d2a_sync_run`(起止、行数、隔离数、对账结果),结构化日志输出。

## 9. 配置(connect.yaml)

```yaml
templates: templates
landing: landing/factory.sqlite     # 当前为 SQLite;PostgreSQL 属后续切片(见 §4 换库信号)
sources:
  digiwin_e10:
    adapter: mssql_readonly         # 开发 / 参考库:sqlite_readonly + path
    dsn_env: D2A_E10_DSN            # 凭据只从环境变量读,绝不落配置文件/仓库

    # 抽取表与同步策略。新安装默认 tables: {};现场经中间机 /metadata 扫描选表后写入。
    # key_columns 可选,覆盖数据库 PK;incremental 必须 watermark;full_refresh 禁止 watermark。
    tables:
      CUSTOMER:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
        key_columns: [CUSTOMER_CODE]   # 可选:配置业务键
      CURRENCY:
        mode: full_refresh             # 快照原子替换
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
        key_columns: [DOC_NO, SEQ]     # 可选:复合键
    windows: ["22:00-06:30"]
    rate: { batch_size: 5000, rows_per_second: 2000 }
    lookback: 3d
    sync_every: 30m                 # 窗口内的同步节奏
    reconcile_at: "05:30"           # 每日 L1 对账
    apply_after_sync: true          # 同步后自动物化对象层(sink=http 时忽略)
```

每表策略在 `tables` 字段中显式声明(`mode: incremental` + `watermark` 或 `mode: full_refresh`),无需额外配置项。示例见仓库根 `connect.example.yaml`。

## 10. 安全机制与试点门槛

### 10.1 当前已实现

| 承诺 | 机制 | 层 |
| --- | --- | --- |
| 只读账号 | 仅 SELECT + ReadOnly intent,非 SELECT 抛异常 | 适配器 |
| 白名单表 | tables 字段显式声明 + 未声明拒绝 | 适配器 |
| 限时 | 错峰窗口硬约束,越界批次边界暂停 | 调度 |
| 限流 | batch_size + rows_per_second + 语句超时 | 适配器 |
| 全部可审计 | d2a_audit_log 逐条 SQL + d2a_sync_run 逐轮汇总 | 全层 |

### 10.2 v0.4 正式试点前必须补齐

| 门槛 | 目标机制 | 当前状态 |
| --- | --- | --- |
| 跨机加密传输 | 平台反向代理终止 TLS 或等价方案;中间 URL 强制 `https://` | ingest 自身仅 HTTP;受控内网验证可用,正式试点未完成 |
| 端到端提交 | schema fingerprint + 行数/摘要 + commit receipt;保存 receipt 后推进水位 | 待建 |
| 跨机对账 | 中间算源侧统计,平台比对并返回差异段,重抽 + 主键 diff 软删 | E6b 待建 |
| raw 数据保护 | 最小目录权限、数据盘/备份加密、保留与清理策略、敏感列裁剪决策 | 待形成试点基线 |
| 凭据治理 | ingest/console/MCP 凭据分离、轮换、吊销、主体审计 | 待建 |
| SQLite 适用性 | 容量/并发/延迟/checkpoint/备份恢复压测与换库阈值 | 待验证 |

因此“只读、白名单、窗口、限流、SQL 审计已实现”不等于整条跨机链已经生产就绪。

## 11. 实施切片(每片带测试,可独立合入)

| 切片 | 内容 | 验证方式 |
| --- | --- | --- |
| E1 | 适配器接口 + sqlite/mssql 适配器 + 落地层(全量) | 参考库 SQLite;容器化 MSSQL 灌入同一份 seed 数据跑集成测试 |
| E2 | 水位增量 + 回看 + keyset 分页 | seed 数据改动若干行,断言只拉增量、水位正确前进 |
| E3 | 分段对账 L1/L2 + 软删 | 源侧删行/改行不动水位,断言对账修复 |
| E4 | 映射应用(binding→对象视图)+ 隔离区 + 熔断 | 注入坏行断言隔离;MCP 网关切换到对象视图 |
| E5 | 调度 + 窗口 + 限流 + CLI + 审计完备 | 窗口/限流行为测试;E10 真实环境预演清单 |
| E6a | 部署(§12):推送 sink（Sink 抽象 + ingest 接收端） | ✅ 中间/平台双进程集成通过(参考链);推送落地与直连逐行一致 |
| E6b | 部署(§12):对账劈开(中间源侧统计 ↔ 平台落地侧比对) | 待建 |

产品版本映射:

| 产品版本 | 本文交付 |
| --- | --- |
| v0.2 可观察 | 运行/批次/水位/raw/object/隔离状态通过管理 API 提供给 Vue Console |
| v0.3 可验证 | §7.1 preview/血缘 + §7.2 数据集版本与原子发布 |
| v0.4 可试点 | §10.2 + §12.3 的提交回执、E6b、TLS 与 SQLite 基线 |

## 12. 部署拓扑(现场:薄中间 + 数据平台)

> 现场约束(客户 IT 定):① 生产 ERP(鼎捷 E10 / SQL Server)只对**中间服务器**授只读账号;
> ② 中间服务器是薄的安全管控层,**可做数据转发,不做数据落地/存储**;
> ③ 数据平台(测试服务器)也在公司内网,但**有外网访问**(需访问云 LLM 等);
> ④ Agent 经数据平台的 MCP 网关取数。

### 12.1 拓扑:内网流式 agent + 出站推送

采用数据平台产品接入 on-prem 源的**主流标准做法**(Azure 自托管集成运行时 SHIR、
Informatica Secure Agent、Fivetran Hybrid 皆同形):

```
生产 ERP(内网,只读)
   │ 本地只读:仅 SELECT / 白名单 / 错峰 / 限流 / 逐条 SQL 审计
   ▼
中间服务器(薄,无状态流式转发)      ← 持有 ERP 只读凭据;数据流过不落盘
   │ 出站推送 raw 批次(当前 HTTP sink;正式试点经反向代理使用 HTTPS/TLS)
   ▼
数据平台(内网,有外网)            ← 落地 raw_* → 构建 objv_* 并原子发布 → MCP 网关(出口脱敏)
   ▼
Agent(经 MCP 网关,拿到脱敏数据)
```

**安全依据**:数据平台是链路里唯一有外网的机器,即唯一对外暴露点。
本拓扑把"读 ERP + ERP 凭据"留在**不上外网**的中间服务器 —— 平台即便从外网被打穿,
也**没有通向 ERP 的活 SQL 通道、拿不到 ERP 凭据**,爆炸半径被隔在 ERP 之外。
raw 只在平台持久存一份,中间仅瞬态过境(无状态,不落盘)。

### 12.2 与 §2 逻辑管道的对应(物理拆分)

§2 逻辑管道(适配器→落地→映射→网关)不变,物理上拆到两台机器:

| 逻辑环节 | 跑在哪 | 说明 |
| --- | --- | --- |
| 适配器读 ERP | 中间服务器 | 只有它能碰 ERP |
| 落地 raw_* | 数据平台 | 中间推送过来后落地 |
| 对账 · 源侧统计 | 中间服务器 | COUNT+MAX / 段重抽,需读 ERP |
| 对账 · 落地侧统计 + 打软删 | 数据平台 | 与中间侧比对 |
| apply 映射 / 隔离 / 熔断 / MCP 网关 / 脱敏 | 数据平台 | 见 §7 / docs 03 |

### 12.3 拆机可靠性能力(E6a 已实现;提交协议与 E6b 待建)

现有 connect 是"落地再映射"引擎(适配器+增量+对账+落地全在一处)。
本拓扑把抽取放在中间、落地放在平台,需要推送 sink、端到端提交协议和跨机对账三项能力;
其余抽取、映射与隔离逻辑继续复用。

**E6a · 推送 sink(✅ 已实现,仅表示传输与幂等落地可运行)**
- 落地出口抽象为 Sink(`connect/sink.py`):`LocalSink`(写本地库,同机/开发默认)、
  `HttpPushSink`(POST 给平台;中间服务器用,stdlib urllib 零额外依赖、值推送前归一化、
  失败指数退避重试);`incremental_sync` 默认 `LocalSink(landing)`,行为向后兼容;
- 平台接收端 `data2agent.ingest`(FastAPI):`POST /ingest/batch` 只负责幂等落地;
  中间机在一张表的全部批次成功后再 `POST /ingest/table-complete`。完成事件包含表结构、
  行数与批次数，零行表也必须发送，平台据此创建空 Raw 表并保存表级新鲜度证据;
- connect.yaml 加 `sink: {type: http, url, token_env}`;**中间用 http sink 时本地只留
  水位/审计/运行状态、不落 raw**(水位是元数据非业务数据),且不在中间 apply(映射在平台侧);
- 验证:中间/平台双进程集成测试 —— 推送落地与直连 sync 逐表逐行一致、中间零 raw 表、
  表级完成事件与零行表完成证据
  (`tests/test_sink_ingest.py`);现场验证见 [runbook/push-validation](../runbook/push-validation.md)
  (主路径为便携包 + 平台 Vue Console);安装见 [portable](../runbook/portable.md),
  链路验收见 [push-validation](../runbook/push-validation.md);
- **约束**:推送模式下 `reconcile_at` 必须留空(config 校验强制)—— 中间只有水位无 raw,
  本地对账会误判整库不一致;跨机对账须待 E6b(中间驱动)。

当前中间机在 HTTP 请求成功返回后推进本地水位;平台没有持久化、可查询的 commit receipt,
也没有 schema fingerprint/内容摘要协商。因此 E6a 解决了“能推、重推幂等”,没有单独证明
平台备份回退、响应丢失或 schema 漂移后的端到端完整性。

**v0.4 · 端到端批次提交协议(待建)**:

- 请求携带不可重复 batch ID、应用/模板版本、schema fingerprint、行数和内容摘要;
- 平台事务提交后持久化批次记录并返回带 commit ID 的 receipt;
- 中间机验证并保存 receipt 后才推进对应表水位;
- 相同 batch ID + 相同摘要重推返回原 receipt;相同 ID + 不同摘要拒绝;
- schema 不兼容明确拒绝,不静默丢列/改类型;
- 控制台可查 pending/committed/retrying/failed/mismatch 并在授权后重放批次。

**E6b · 对账劈开(v0.4 待建)**:纯出站推送下平台不能回调中间,故对账**中间驱动** ——
中间算源侧段统计 POST `/ingest/reconcile` → 平台比对落地侧、响应回不一致段 →
中间重抽这些段推送(行 + 当前主键全集)→ 平台按主键 diff 打软删。

复用不变:游标增量 + 回看 + 软删 + 隔离 + 熔断 + 逐条审计 + 错峰窗口。

### 12.4 只读约束下的同步机制

ERP 只授只读 → **CDC / 日志捕获被封死**(SQL Server CDC / Change Tracking 需提权开启,
只读账号做不到)。故增量采用**游标(水位)增量**,其删除 / 静默改盲区
由 §6 分段对账 + 软删补齐 —— 这正是廉价标准 agent 默认没有、data2agent 内建的部分。

## 附录 · v0.4 通过后现场核对清单

按对象逐 binding 确认并置 verified:① 表名与我们参考表形的差异;② 每个 field_map 字段的真实字段名;③ 水位字段语义(修改时间?审核时间?时区?);④ 状态码全集(map 是否遗漏取值);⑤ 业务键唯一性实测;⑥ 只读账号 + 白名单授权是否就位;⑦ 允许的抽取窗口与限流上限(和 IT 书面确认)。

部署拓扑(§12)现场确认项:⑧ 中间服务器可跑无状态流式 agent(本方案前提);⑨ 中间→数据平台的出站已放行;⑩ 数据平台的外网出站收窄为白名单(仅 LLM 端点等);⑪ ERP 只授只读(游标增量 + 对账为准;如另开 CDC/Change Tracking 权限则重估);⑫ TLS 证书与反向代理验收;⑬ 批次 receipt/摘要校验通过;⑭ E6b 与 deep 周期已经配置;⑮ SQLite 容量、备份和恢复基线通过。
