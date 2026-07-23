# ERP 元数据发现与抽取表管理优化实施计划

> 状态：待实施  
> 制定日期：2026-07-23  
> 建议目标版本：v0.5  
> 前置设计：[中间机独立抽取表配置](../../design/06-middle-table-extraction-config.md)  
> 本文只定义实施方案，不代表相关功能已经完成。

## 1. 目标

本改造解决中间机目前将“数据库连接”和“所有配置表均可同步”混为一个结果，以及默认
ERP 表清单、数据库物理主键和实际水位字段与现场数据库不匹配的问题。

对应五项优化目标：

1. 分离数据库连接测试与抽取表可用性校验。
2. 删除随包 ERP 表清单，抽取表只来自当前 ERP 元数据和用户确认。
3. 支持现场确认数据库主键、唯一索引或业务唯一键，并写入中间机配置。
4. 为增量同步和全量同步建立不同的数据协议与校验规则。
5. 新增 ERP 元数据页面，并把抽取表管理从连接配置页拆成独立页面。

## 2. 非目标

- 不在中间机维护 raw 到业务对象的字段映射；该职责仍属于平台模板。
- 不修改 ERP 数据、索引、主键或其他数据库结构。
- 不自动启用扫描到的表，也不自动把“候选水位字段”认定为可靠水位。
- 不在浏览器或配置文件中暴露 DSN、ERP 密码和 Token。
- 不允许元数据页面执行任意 SQL。
- 本阶段不自动推断跨表业务关系或生成业务对象模板。

## 3. 核心设计决定

### 3.1 配置状态允许“已连接但尚未选择表”

`sources.<source>.tables` 允许为空映射。空表清单表示：

- ERP 连接信息已经保存；
- 可以执行连接测试和元数据扫描；
- connector 保持空闲，不访问任何业务表；
- 状态页显示“已连接，尚未配置抽取表”，不将其标记为运行失败。

这取代当前“`tables` 必须非空”的校验。

### 3.2 删除 ERP 随包清单

彻底删除 `erp-configs/digiwin_e10.yaml` 及其加载、复制、安装和测试代码。中间机不再携带
任何 ERP 表名候选清单，表目录只能来自当前连接数据库的实时元数据。

新安装生成：

```yaml
tables: {}
```

只有用户在“抽取表管理”页面确认并保存的表才进入运行白名单。`docs/dict` 可以继续作为
项目知识文档，但不得被运行时、首次安装或管理页面读取。

### 3.3 唯一键采用“数据库发现 + 现场显式覆盖”

目标配置格式：

```yaml
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN
    tables:
      ITEM:
        schema: dbo
        mode: incremental
        key_columns: [ITEM_ID]
        watermark: UPDATE_TIME
        schema_fingerprint: sha256:...
        validated_at: "2026-07-23T10:00:00Z"

      ITEM_WAREHOUSE:
        schema: dbo
        mode: full_refresh
        key_columns: [ITEM_ID, WAREHOUSE_ID]

      BOM_D:
        schema: dbo
        mode: full_refresh
```

字段语义：

| 字段 | `incremental` | `full_refresh` | 说明 |
| --- | --- | --- | --- |
| `schema` | 可选，默认 `dbo` | 可选，默认 `dbo` | SQL Server schema |
| `key_columns` | 必填 | 可选 | 数据库主键、唯一索引或现场确认的业务唯一键 |
| `watermark` | 必填 | 禁止 | 真实的增量水位列 |
| `schema_fingerprint` | 可选，页面保存时写入 | 可选，页面保存时写入 | 已确认字段结构的摘要，不包含业务数据 |
| `validated_at` | 可选，页面保存时写入 | 可选，页面保存时写入 | 最近一次现场校验时间 |

元数据页面的键建议优先级：

1. 优先建议数据库 `PRIMARY KEY`；
2. 其次建议唯一约束或唯一索引；
3. 普通字段组合不得仅凭名称自动认定为业务唯一键；
4. 用户确认后统一写入 `key_columns`；
5. 运行时只使用 `connect.yaml`，不重新猜测运行键。

保存业务键前执行只读校验：

- 字段存在；
- 字段组合无 NULL；
- 字段组合不存在重复值；
- 校验查询受独立超时限制；
- 校验结果只把结构摘要和时间写入 `connect.yaml`，不保存业务数据值。

### 3.4 增量和全量使用不同落地语义

#### 增量同步

- 必须有稳定的 `key_columns`；
- 必须有已确认的 `watermark`；
- 按 `(watermark, key_columns)` 确定性分页；
- 平台继续按唯一键幂等 upsert；
- 水位只在整张表全部批次确认后推进；
- 复合键必须得到增量分页、续传、回看和重试支持。

#### 全量同步

- 每次读取源表全部当前数据；
- 不要求稳定主键；
- 数据先进入本轮 snapshot staging；
- 全部批次和行数确认后，在平台事务内原子替换当前 raw 表；
- 失败或中断时保留上一份完整 raw 表，不暴露半张表；
- 完成事件重试必须幂等；
- 旧快照按保留策略清理。

这使 `full_refresh` 成为真正的当前状态快照，源库中已经删除的记录不会长期残留在平台。

### 3.5 三个独立管理页面

中间机管理界面调整为：

| 页面 | 路径 | 职责 |
| --- | --- | --- |
| 状态 | `/` | 连接、调度、最近同步、水位和故障摘要 |
| 连接配置 | `/config` | ERP 地址/数据库/账号、平台地址、Token、调度和限流 |
| ERP 元数据 | `/metadata` | 扫描和浏览实际 ERP 表结构 |
| 抽取表管理 | `/tables` | 选择表并配置模式、业务键和水位 |
| 日志 | `/logs` | 中间机服务日志 |

连接配置页不再包含抽取表编辑器。

## 4. 目标用户流程

```text
保存 ERP 连接
  → 测试数据库连接
  → 成功后刷新 ERP 元数据
  → 在元数据页查看表、字段、PK、唯一索引和水位候选
  → 将需要的表加入抽取计划
  → 在抽取表页确认模式、业务键和水位
  → 校验并原子保存 tables
  → connector 下一轮使用新计划
```

连接测试成功不再要求任何抽取表存在。

## 5. 管理 API 设计

### 5.1 连接测试

`POST /api/connection/test`

只验证：

- DSN 可连接；
- 当前数据库可访问；
- 基本元数据查询权限可用；
- 返回耗时、数据库名称和脱敏后的服务版本。

不读取 `tables`，也不以抽取表错误否定连接成功。

响应状态分为：

- `connected`：数据库和元数据权限正常；
- `connected_limited`：数据库已连接，但元数据权限不足；
- `failed`：ODBC、认证、网络、超时或数据库选择失败。

### 5.2 元数据扫描

新增：

- `POST /api/metadata/scans`：启动扫描，返回 `scan_id`；
- `GET /api/metadata/scans/{scan_id}`：读取进度和结果摘要；
- `GET /api/metadata/tables`：分页、搜索和筛选表/视图；
- `GET /api/metadata/tables/{schema}/{table}`：读取字段、键和索引；
- `POST /api/metadata/key-check`：校验候选业务键；
- `POST /api/metadata/watermark-check`：校验候选水位字段类型和可用性。

扫描至少读取：

- schema、表或视图名称；
- 字段名、顺序、SQL 类型、NULL 属性；
- PRIMARY KEY；
- UNIQUE constraint / unique index；
- 外键摘要；
- 估算行数（可获取时）；
- 候选水位列。

候选水位只根据字段类型和常见命名生成建议，例如 `UPDATE_TIME`、`MODIFIED_AT`；
页面必须显示“待确认”，不得直接保存为增量配置。

### 5.3 抽取表管理

新增：

- `GET /api/extraction-tables`：读取当前抽取计划和配置修订号；
- `POST /api/extraction-tables/validate`：验证待保存计划；
- `PUT /api/extraction-tables`：按修订号原子替换整个 `tables` 子树。

`PUT` 必须使用乐观并发控制。页面读取到的配置修订号与磁盘当前版本不一致时返回 `409`，
要求刷新后重新提交，防止两个浏览器相互覆盖。

验证结果逐表返回：

- `ready`；
- `table_missing`；
- `key_missing`；
- `key_not_unique`；
- `watermark_missing`；
- `watermark_invalid`；
- `metadata_stale`；
- `permission_denied`。

## 6. 元数据处理与保存边界

不新增 SQLite 元数据库，也不把完整 ERP 元数据写入 `connect.yaml`。

一次扫描会读取：

- schema、表或视图名称；
- 字段名、顺序、SQL 类型和 NULL 属性；
- PRIMARY KEY、唯一约束、唯一索引和外键摘要；
- 可获取时的估算行数；
- 候选水位字段；
- 本次扫描时间和结构指纹。

完整扫描结果只保存在中间机进程内存中：

- 按 `scan_id` 隔离；
- 设置数量上限和短期 TTL；
- 进程重启后自动丢弃；
- 页面没有可用缓存时重新读取 ERP；
- 新扫描失败时页面显示失败，不伪装为最新数据。

`connect.yaml` 只保存用户最终确认的抽取规则：

- schema 和表名；
- 同步模式；
- `key_columns`；
- `watermark`；
- 已确认结构的 `schema_fingerprint`；
- `validated_at`。

因此配置文件仍是抽取计划的唯一事实来源，但不会膨胀为整套 ERP 数据字典。

## 7. 同步与 ingest 协议调整

### 7.1 表信息解析

`TableInfo` 需要区分：

- 数据库声明主键；
- 配置确认的运行键；
- 当前同步模式；
- schema。

适配器所有 SQL 标识符必须分段引用，例如 `[dbo].[ITEM]`，不能拼接未经校验的
`schema.table` 字符串。

### 7.2 增量复合键

当前增量引擎只支持单列主键。实施时扩展为按
`(watermark, key_1, key_2, ...)` 排序和续传，并覆盖：

- 同水位多行；
- 复合键边界；
- 回看重跑；
- 中断续传；
- NULL 键拒绝；
- 水位不后退。

### 7.3 全量快照协议

HTTP 推送协议增加快照生命周期：

1. `table-begin`：创建或恢复 `snapshot_id`；
2. `batch`：写入 snapshot staging，并用 `snapshot_id + batch_id` 去重；
3. `table-complete`：核对批次数和行数；
4. 平台事务内发布 staging 为当前 raw 表；
5. 返回不可变完成回执，中间机收到后才记录成功。

本地 `LocalSink` 与远端 `HttpPushSink` 使用同一生命周期语义。

旧 ingest 请求模型和旧批次协议直接删除，不提供双协议兼容。平台健康接口返回固定的
`ingest_protocol_version`，中间机启动同步前要求版本完全一致；不一致时 fail-fast。

## 8. 页面设计

### 8.1 连接配置页 `/config`

保留：

- ERP 服务器、端口、数据库、账号和密码；
- 平台 ingest 地址和 Token；
- 管理 Token；
- 同步周期、错峰窗口和限流。

新增：

- 独立“测试数据库连接”按钮；
- 最近一次连接结果；
- “连接成功后前往 ERP 元数据”入口。

删除：

- 当前页面中的抽取表表格；
- 表模式和水位编辑逻辑；
- 将表校验结果拼接为“连接失败”的现有表现。

### 8.2 ERP 元数据页 `/metadata`

页面结构：

- 顶部：连接状态、最近扫描时间、刷新元数据；
- 筛选：schema、表/视图、关键词、是否有 PK、是否已加入抽取；
- 主列表：表名、类型、估算行数、PK、唯一索引、抽取状态；
- 详情抽屉：字段、类型、NULL、键、外键和水位候选；
- 操作：“加入抽取计划”。

元数据页只负责发现，不直接保存整个 `tables` 配置。

### 8.3 抽取表管理页 `/tables`

主列表至少显示：

- schema / 表名；
- 元数据状态；
- 同步模式；
- 运行键及来源（数据库 PK / 唯一索引 / 业务键）；
- 水位字段；
- 最近校验时间和结果；
- 编辑、移除和重新校验。

编辑面板：

- `incremental`：必须选择键和水位；
- `full_refresh`：水位隐藏，键可选；
- 业务键需要显式执行唯一性校验；
- 保存前展示会新增、修改和删除的表；
- 整体保存后提示 connector 从下一轮开始使用新配置。

## 9. 实施里程碑

### M1：配置契约与连接结果拆分

修改：

- `data2agent/connect/config.py`
- `data2agent/middle_admin/app.py`
- `data2agent/admin_common/config_edit.py`
- `data2agent/admin_common/setup_yaml.py`
- 删除 `erp-configs/digiwin_e10.yaml`
- 删除所有 ERP profile 加载与便携包复制代码

任务：

- 允许 `tables: {}`；
- 增加 `schema`、`key_columns`、`schema_fingerprint`、`validated_at`；
- 新安装不包含任何候选表；
- 新增纯连接测试；
- 删除旧 `POST /api/test-connection`；
- 定义配置修订号和原子更新。

验收：

- 空表清单可以保存并启动；
- 连接成功但表未配置时状态明确；
- 安装包中不存在 `erp-configs/digiwin_e10.yaml`；
- 配置冲突返回 `409`。

### M2：SQL Server 元数据发现后端

新增或修改：

- 新增 `data2agent/connect/metadata.py`
- 修改 `data2agent/connect/adapters/mssql.py`
- 修改 `data2agent/middle_admin/app.py`

任务：

- 实现表、视图、字段、PK、唯一索引和外键查询；
- 实现进程内扫描任务、TTL 缓存、数量上限和超时；
- 实现业务键唯一性与 NULL 校验；
- 实现水位候选和字段类型校验；
- 所有异常输出脱敏。

验收：

- 可在没有任何 `tables` 配置时扫描；
- 元数据权限不足与连接失败可区分；
- 不保存 ERP 行值；
- 不创建元数据 SQLite 或其他持久化缓存文件；
- 进程重启后可以重新扫描并恢复页面能力。

### M3：运行键与增量复合键

修改：

- `data2agent/connect/adapters/base.py`
- `data2agent/connect/adapters/mssql.py`
- `data2agent/connect/adapters/sqlite.py`
- `data2agent/connect/increment.py`
- `data2agent/connect/scheduler.py`
- `data2agent/connect/landing.py`
- `data2agent/connect/sink.py`

任务：

- 配置业务键覆盖数据库主键；
- 增量同步支持复合键；
- 运行时验证键字段存在、非 NULL；
- 水位和复合键游标使用新的统一序列化格式；
- 审计记录键来源，不记录键值。

验收：

- 单键和复合键均通过新配置契约运行；
- 复合键同水位分页不丢行、不重行；
- 错误业务键在同步前失败；
- 中断重启后可从稳定边界续传。

### M4：真正的全量快照替换

修改：

- `data2agent/connect/increment.py`
- `data2agent/connect/sink.py`
- `data2agent/connect/landing.py`
- `data2agent/ingest/app.py`
- ingest 请求模型及相关部署检查

任务：

- 增加 snapshot begin/batch/complete 协议；
- 增加 staging raw 表；
- 支持无主键全量表；
- 实现批次去重、完成核对、原子发布与失败清理；
- 增加严格的 `ingest_protocol_version` 校验。

验收：

- 中途失败仍读取上一完整 raw 表；
- 源表删除行后，下一次全量完成后平台不再保留该行；
- 同一 snapshot 重放结果不重复；
- 零行表可正确发布为空表；
- 无主键表可使用 `full_refresh`。

### M5：中间机页面拆分

新增或修改：

- 修改 `data2agent/middle_admin/templates/layout.html`
- 修改 `data2agent/middle_admin/templates/config.html`
- 新增 `data2agent/middle_admin/templates/metadata.html`
- 新增 `data2agent/middle_admin/templates/tables.html`
- 修改 `data2agent/middle_admin/app.py`
- 修改便携包静态/模板完整性检查

任务：

- 新增 `/metadata` 和 `/tables`；
- 配置页删除表编辑器；
- 实现元数据扫描和详情；
- 实现抽取计划编辑、校验、差异确认和原子保存；
- 状态页按连接、配置、运行三个层次展示。

验收：

- 用户可以完整走通“连接→扫描→选表→确认→保存”；
- 页面刷新不会丢失未保存提示；
- 元数据扫描失败不影响已有 connector；
- 删除抽取表必须二次确认；
- 页面不显示密码、DSN 或 Token 明文。

### M6：旧实现清理、文档、全链路验收与发布

修改：

- `deploy/build_portable.ps1`
- `scripts/check_portable_package.py`
- `scripts/smoke_admin_ui.py`
- 示例配置、Release 检查和 CI
- 第 12 节列出的项目文档

任务：

- 删除旧配置字段、迁移命令、旧 API、旧 ingest 协议和相关测试；
- 首次打开新页面时引导扫描并选择表；
- 平台与中间机协议版本严格检查；
- 更新安装包模板和检查脚本；
- 执行真实 SQL Server 集成测试；
- 确认发布包不包含已删除的 ERP profile 和兼容代码。

验收：

- 新安装默认不访问任何 ERP 业务表；
- 两端版本不兼容时 fail-fast，不产生半份数据；
- 旧配置格式和旧 ingest 请求均被明确拒绝；
- Windows 便携包完成端到端验收；
- CI、Release 构建和安装包检查全部通过。

## 10. 测试计划

新增测试建议：

- `tests/test_erp_metadata.py`：元数据模型、候选键和水位；
- `tests/test_middle_metadata_api.py`：扫描 API、权限、脱敏和缓存；
- `tests/test_middle_extraction_tables.py`：独立表计划 API、修订冲突和原子保存；
- `tests/test_composite_increment.py`：复合键增量边界；
- `tests/test_full_refresh_snapshot.py`：全量 staging、发布、失败和重放；
- `tests/integration/mssql/test_metadata_discovery.py`：SQL Server 元数据；
- `tests/integration/mssql/test_business_keys.py`：配置键覆盖数据库 PK；
- `tests/integration/mssql/test_snapshot_replace.py`：无键全量替换；
- 中间机浏览器验收：配置页、元数据页、抽取表页和状态页。

现有测试需要重点更新：

- `tests/test_table_config.py`
- `tests/test_middle_admin.py`
- `tests/test_config_scheduler.py`
- `tests/test_sink_ingest.py`
- `tests/test_increment.py`
- `tests/test_home_setup.py`
- `tests/test_ui_launcher.py`

## 11. 发布策略

### 11.1 干净切换

系统尚未上线，不设计旧配置和旧协议兼容层：

- 删除旧配置解析、迁移命令和兼容错误提示；
- 删除旧连接测试接口；
- 删除旧 ingest 请求模型和双协议分支；
- 删除相关兼容测试、文档和部署脚本；
- 测试环境重新生成 `connect.yaml`；
- 平台包和中间机包必须来自同一 Release。

### 11.2 不设置灰度开关

不需要灰度开关。灰度开关只会让尚未上线的系统长期保留两套行为，增加测试和排障成本。

仍保留不可关闭的协议版本校验：

1. 平台健康接口返回 `ingest_protocol_version`；
2. 中间机启动同步前比较自身要求版本；
3. 版本不一致立即停止同步并显示明确错误；
4. 版本一致后直接使用新协议，不存在旧行为回退。

## 12. 实施时必须更新的文档

| 文档 | 更新内容 |
| --- | --- |
| `docs/design/02-extraction.md` | 业务键、复合键增量、全量快照原子替换 |
| `docs/design/06-middle-table-extraction-config.md` | 新配置模型、空表清单、删除随包清单与独立页面 |
| `docs/dict/digiwin_e10.md` | 元数据发现与现场确认规则 |
| `docs/dict/digiwin_e10_dead_stock_verified.md` | 明确该文档不参与运行时抽取配置 |
| `docs/runbook/portable.md` | 首次连接、扫描和选表流程 |
| `docs/runbook/push-validation.md` | 连接、元数据、抽取计划和快照验收 |
| `docs/runbook/source-dev.md` | 本地元数据扫描和 SQL Server 集成测试 |
| `docs/roadmap.md` | 里程碑状态和发布门槛 |
| `README.md` | 中间机部署流程简述 |

## 13. 发布门槛

必须同时满足：

- 数据库连接结果与表校验结果完全分离；
- 安装包不包含 ERP 表名候选清单；
- 元数据扫描只读、脱敏、可超时且不持久化完整元数据；
- 增量表必须有已验证键和水位；
- 全量表通过 staging 原子替换，不暴露半成品；
- 无主键全量表完成跨机推送；
- `/config`、`/metadata`、`/tables` 职责清晰；
- 旧配置、旧接口、旧协议及迁移代码已彻底删除；
- Python、前端/页面、SQL Server 集成、Docker/便携包和 Release 检查全部通过。

## 14. 推荐执行顺序

严格按 `M1 → M2 → M3 → M4 → M5 → M6` 实施。

M2 依赖 M1 的空配置和连接结果契约；M3 依赖 M2 产生的键确认信息；M4 依赖 M3 的
运行键模型；M5 在 API 契约冻结后开发；M6 负责旧实现清理和正式发布。不得先制作页面再反推
同步协议，否则会再次出现“界面可配置、运行时不支持”的状态。
