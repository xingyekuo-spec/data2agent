# 02 · 抽取框架(详设)

> 状态:详设 v0.3(2026-07-11)· 实现目录:`data2agent/connect/` + `data2agent/ingest/` · 抽取框架 E1–E5 已实现;部署 E6a(推送 sink)已实现,E6b(对账劈开)待建

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

> 上图是**逻辑管道**(与部署位置无关)。现场的**物理部署**会把它拆到"中间服务器 + 数据平台"两台机器上 —— 见 §12 部署拓扑。

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
| `api_poll`(规划) | 有 API 的源(SRM 等) | 尚未实现;按需拉动,待首个真实 API 源出现再定义接口 |
| `excel_import`(非 SourceAdapter,CLI 导入流程,见 `connect/excel_import.py`) | 报价历史 Excel/CSV(MVP,已实现) | 三步工作流:`excel-suggest` 启发式建议列映射(YAML)→ 人工确认一次 → `excel-import` 快照落地(raw 表结构由 binding 契约决定,文件缺列落 NULL、缺业务键跳过并逐行报告、重导入按业务键幂等)→ 标准 apply 物化,校验 / 隔离 / 熔断原样生效;值翻译写 binding 的 (map ...) |

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
| `_d2a_row_hash` | 行内容 md5(规范化序列化后);落地时写入,当前对账未消费(预留:E6b 跨机对账时中间只推 hash、平台比对后仅回差异行) |
| `_d2a_deleted_at` | 软删标记(对账发现源侧消失时打标,永不物理删) |

- 写入语义:按源主键 **upsert**(E10 为 `Id`),幂等 —— 回看窗口和对账重抽天然安全;
- 落地库:当前实现为 **SQLite**(零依赖、单文件、零运维),覆盖开发 / 展厅 / 首个工厂现场验证。访问模式是单写者(connector 进程)+ 多只读者(MCP 网关 / 运维控制台),正是 SQLite 的舒适区;`LandingStore` 初始化即开 `journal_mode=WAL` + `busy_timeout`,写批次不阻塞只读连接。
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
- 无可靠水位字段的表(小维表如 CURRENCY):走全量刷新 —— 由 binding 有无 `watermark` 声明**自动推导**,无独立配置项(元模型仍是唯一事实来源;不可靠水位应从 binding 移除 `watermark`,而非加配置覆盖);
- 水位字段语义(是"修改时间"还是"审核时间")属现场核对项,binding `watermark` 为准。

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

## 8. 调度与运行(scheduler.py + __main__.py)

- apscheduler 按源调度;**错峰窗口**(如 `windows: ["22:00-06:30"]`)硬约束:窗口外不发起,运行中越界则在批次边界优雅暂停、下窗口续跑(水位机制天然支持断点);
- CLI(`python -m data2agent.connect`):`sync`(`--source` / `--full` / `--lookback-days` + 源连接参数)/ `apply` / `backfill --table --from --to` / `reconcile [--deep]` / `serve [--once]`(常驻调度,`--once` 立即各跑一轮后退出,验证配置用)/ `status` / `quarantine list|retry` / `excel-suggest` / `excel-import`;
- 每轮汇总进 `d2a_sync_run`(起止、行数、隔离数、对账结果),结构化日志输出。

## 9. 配置(connect.yaml)

```yaml
templates: templates
landing: landing/factory.sqlite     # 当前为 SQLite;PostgreSQL 属后续切片(见 §4 换库信号)
sources:
  digiwin_e10:
    adapter: mssql_readonly         # 开发 / 展厅:sqlite_readonly + path
    dsn_env: D2A_E10_DSN            # 凭据只从环境变量读,绝不落配置文件/仓库
    whitelist_from_bindings: true
    extra_whitelist: []
    windows: ["22:00-06:30"]
    rate: { batch_size: 5000, rows_per_second: 2000 }
    lookback: 3d
    sync_every: 30m                 # 窗口内的同步节奏
    reconcile_at: "05:30"           # 每日 L1 对账
    apply_after_sync: true          # 同步后自动物化对象层
```

每表策略无需配置:有 `binding.watermark` 声明 → 水位增量;没有 → full_refresh(自动推导,元模型仍是唯一事实来源)。示例见仓库根 `connect.example.yaml`。

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
| E6a | 部署(§12):推送 sink（Sink 抽象 + ingest 接收端） | ✅ 中间/平台双进程集成通过(展厅);推送落地与直连逐行一致 |
| E6b | 部署(§12):对账劈开(中间源侧统计 ↔ 平台落地侧比对) | 待建 |

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
   │ 出站推送 raw 批次(HTTPS/TLS)
   ▼
数据平台(内网,有外网)            ← 落地 raw_* → apply obj_* → MCP 网关(出口脱敏)
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

### 12.3 需要的改动(E6a 已实现,E6b 待建)

现有 connect 是"落地再映射"引擎(适配器+增量+对账+落地全在一处)。
本拓扑把抽取放在中间、落地放在平台,需两项改动,其余全部复用。

**E6a · 推送 sink(✅ 已实现)**
- 落地出口抽象为 Sink(`connect/sink.py`):`LocalSink`(写本地库,同机/开发默认)、
  `HttpPushSink`(POST 给平台;中间服务器用,stdlib urllib 零额外依赖、值推送前归一化、
  失败指数退避重试);`incremental_sync` 默认 `LocalSink(landing)`,行为向后兼容;
- 平台接收端 `data2agent.ingest`(FastAPI,`POST /ingest/batch` → 复用 landing 幂等落地,
  可选 Bearer Token;`python -m data2agent.ingest`);
- connect.yaml 加 `sink: {type: http, url, token_env}`;**中间用 http sink 时本地只留
  水位/审计/运行状态、不落 raw**(水位是元数据非业务数据),且不在中间 apply(映射在平台侧);
- 验证:中间/平台双进程集成测试 —— 推送落地与直连 sync 逐表逐行一致、中间零 raw 表
  (`tests/test_sink_ingest.py`);现场验证步骤见 [runbook/push-validation](../runbook/push-validation.md),
  Windows 两台部署细节见 [runbook/windows-deploy](../runbook/windows-deploy.md);
- **约束**:推送模式下 `reconcile_at` 必须留空(config 校验强制)—— 中间只有水位无 raw,
  本地对账会误判整库不一致;跨机对账须待 E6b(中间驱动)。

**E6b · 对账劈开(待建)**:纯出站推送下平台不能回调中间,故对账**中间驱动** ——
中间算源侧段统计 POST `/ingest/reconcile` → 平台比对落地侧、响应回不一致段 →
中间重抽这些段推送(行 + 当前主键全集)→ 平台按主键 diff 打软删。

复用不变:游标增量 + 回看 + 软删 + 隔离 + 熔断 + 逐条审计 + 错峰窗口。

### 12.4 只读约束下的同步机制

ERP 只授只读 → **CDC / 日志捕获被封死**(SQL Server CDC / Change Tracking 需提权开启,
只读账号做不到)。故增量采用**游标(水位)增量**,其删除 / 静默改盲区
由 §6 分段对账 + 软删补齐 —— 这正是廉价标准 agent 默认没有、data2agent 内建的部分。

## 附录 · 现场核对清单(进厂当天用)

按对象逐 binding 确认并置 verified:① 表名与我们参考表形的差异;② 每个 field_map 字段的真实字段名;③ 水位字段语义(修改时间?审核时间?时区?);④ 状态码全集(map 是否遗漏取值);⑤ 业务键唯一性实测;⑥ 只读账号 + 白名单授权是否就位;⑦ 允许的抽取窗口与限流上限(和 IT 书面确认)。

部署拓扑(§12)现场确认项:⑧ 中间服务器可跑无状态流式 agent(本方案前提);⑨ 中间→数据平台的出站已放行;⑩ 数据平台的外网出站收窄为白名单(仅 LLM 端点等);⑪ ERP 只授只读(游标增量 + 对账为准;如另开 CDC/Change Tracking 权限则重估)。
