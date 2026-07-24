# ERP 元数据发现与抽取表管理优化实施计划

> 状态：进行中（M0–M4 已完成并提交；下一步 M5 中间机页面拆分）
> 制定日期：2026-07-23
> 最近修订：2026-07-24
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

### 3.5 管理页面职责拆分

中间机管理界面调整为：

| 页面 | 路径 | 职责 |
| --- | --- | --- |
| 状态 | `/` | 连接、调度、最近同步、水位和故障摘要 |
| 连接配置 | `/config` | ERP 地址/数据库/账号、平台地址、Token、调度和限流 |
| ERP 元数据 | `/metadata` | 扫描和浏览实际 ERP 表结构 |
| 抽取表管理 | `/tables` | 选择表并配置模式、业务键和水位 |
| 日志 | `/logs` | 中间机服务日志 |

连接配置页不再包含抽取表编辑器。

### 3.6 平台端使用独立配置模型

平台端不再复用中间机 `ConnectConfig`，新增只描述平台职责的 `PlatformConfig`。该模型只
保留 ingest 落地、apply、Console、MCP、认证和平台运行所需字段，不包含：

- ERP DSN、adapter 或数据库连接参数；
- `sources.*.tables` 抽取计划；
- 中间机调度、水位和限流配置；
- 平台直连 ERP 的 sync/reconcile 动作。

平台 ingest 请求仍必须携带 `source`、`schema`、`table`、同步模式和协议批次标识，用于
数据隔离与落地；删除的是平台配置中的 ERP 连接和抽取能力，不是 ingest 数据来源标识。

不得通过“继续读取 `ConnectConfig`，但忽略 `sources`”实现切分。系统尚未上线，应直接
删除错误的配置字段和运行入口，避免平台配置继续暗示可以直连 ERP。

### 3.7 元数据发现使用能力协议

新增 `MetadataDiscoverer` 协议，元数据 API 和扫描任务只依赖该协议，不直接判断 adapter
名称或拼接特定数据库系统表 SQL。首期实现：

- `MssqlMetadataDiscoverer`：生产使用；
- `SqliteMetadataDiscoverer` 或测试替身：用于协议测试和本地测试。

协议至少提供：

- schema 枚举；
- 表和视图分页/搜索；
- 表字段、主键、唯一索引和外键摘要；
- 候选业务键校验；
- 候选水位字段校验。

数据库连接由具体 discoverer 或 adapter 内部管理。协议不得接收、返回或向管理层暴露
原始连接对象。adapter 不支持元数据发现时返回明确的
`metadata_discovery_unsupported`，不得在 `metadata.py` 中堆叠 adapter 分支。

### 3.8 中间里程碑的运行语义与发布边界

删除 CLI 的 `sync --full` 不等于删除表级 `mode: full_refresh`。表级模式是正式配置能力，
在整个改造期间都必须保持可运行。

在 M4 原子快照完成前，现有 `full_refresh` 仍是“整表读取并 upsert”的旧落地语义，不能
保证删除源端已不存在的行。因此：

- `sync --full` 和 `full_sync()` 的删除统一放到 M4，与新快照协议一次完成；
- M0 至 M3 必须保留表级 `full_refresh` 的现有行为及回归测试；
- M0 至 M3 只是同一功能分支中的中间提交，不得构建正式包或部署到工厂；
- 只有 M4 完成删除行、失败回滚、零行表和幂等重放验收后，才允许进入页面联调；
- v0.5 只发布 M0 至 M7 全部完成后的单一干净切换版本。

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

### M0：旧配置与随包清单清理

删除或修改：

- 删除 `erp-configs/digiwin_e10.yaml` 和空目录；
- 删除 `deploy/setup-middle.ps1`、`deploy/setup-platform.ps1`；
- 删除 `docs/superpowers/plans/2026-07-23-explicit-table-config.md`；
- 修改 `data2agent/admin_common/setup_yaml.py`；
- 修改 `data2agent/connect/config.py`；
- 修改 `data2agent/connect/__main__.py`；
- 修改依赖旧抽取工具的测试和文档。

任务：

- 删除 ERP profile 常量、定位、加载、复制和完整性检查；
- 删除 `whitelist_from_bindings`、`extra_whitelist` 的兼容解析和提示；
- 删除 `migrate-config` CLI 与 `migrate_config_to_tables()`；
- 删除生产代码中的 `whitelist_from_pack()`、`watermarks_from_pack()`，测试改用显式
  表配置或 `tests` 内部 fixture；
- 删除通用配置接口对 `sources.*.tables` 的编辑能力；抽取表只能由后续专用 API 保存；
- 清理测试和文档中的旧 profile、迁移命令和模板推导入口；
- 保留表级 `mode: full_refresh`、CLI `sync --full` 和 `full_sync()`，直到 M4 一次完成
  语义替换和删除。

验收：

- `rg` 无运行时 ERP profile、旧配置字段、迁移命令和模板推导工具；
- 新安装不再从随包文件生成候选表；
- 显式表配置仍可分别执行 incremental 和旧语义 full refresh；
- 针对表级 `full_refresh` 增加回归测试，确认 M0 清理未令其失效；
- 删除旧 PowerShell 安装脚本后，EXE + 浏览器成为唯一安装入口；
- Python 和现有核心测试通过；
- 本里程碑不得独立发布。

### M0-P：平台配置与运行边界切分

新增或修改：

- 新增平台专用配置模型及加载入口；
- 修改 `data2agent/admin_common/setup_yaml.py`；
- 修改 `data2agent/console/app.py`；
- 修改 `data2agent/console/contracts.py`；
- 修改 `data2agent/ingest/app.py` 的平台配置接入，但保留 ingest 请求中的来源标识；
- 修改平台便携包生成、启动和完整性检查；
- 修改 `console-ui/openapi.json`、生成类型和相关前端能力展示；
- 修改平台 Console、契约、部署和安装测试。

任务：

- 定义 `PlatformConfig` 及严格校验，只接受平台职责字段；
- 将 Console 和平台启动入口从 `ConnectConfig` 切换到 `PlatformConfig`；
- 删除平台配置中的 ERP adapter、`D2A_E10_DSN_PLACEHOLDER`、硬编码 ERP 表和
  `sources`；
- 删除平台端 `/api/actions/sync`、`/api/actions/reconcile` 和
  `actions_sync_reconcile`；
- 删除 Console 直接调用 `run_sync_cycle()`、`run_reconcile_cycle()` 的路径；
- 平台状态和观测改为读取 ingest/raw/apply 结果，不再读取 ERP source 配置；
- 更新平台配置生成器、EXE 首次安装、OpenAPI、前端类型、测试和文档；
- 保持 ingest 请求中的 `source/schema/table` 以及快照/批次标识，避免将配置切分误作
  数据来源标识删除。

验收：

- 平台配置解析模型中不存在 `sources` 和 ERP 连接字段；
- 平台包中没有 ERP DSN、抽取表计划和直连 ERP 动作；
- 平台配置若出现已删除字段会明确拒绝，而不是静默忽略；
- 中间机是唯一可以发起 ERP 抽取的角色；
- Console 不再暴露 sync/reconcile 接口或能力标志；
- ingest 仍可按来源和表正确隔离数据；
- Python、OpenAPI、前端类型、平台启动和便携包检查通过；
- 本里程碑不得独立发布。

### M1：配置契约与连接结果拆分

修改：

- `data2agent/connect/config.py`
- `data2agent/middle_admin/app.py`
- `data2agent/admin_common/config_edit.py`
- `data2agent/admin_common/setup_yaml.py`
- `deploy/build_portable.ps1`
- `scripts/check_portable_package.py`
- `connect.example.yaml`
- `tests/test_home_setup.py`

任务：

- 允许 `tables: {}`；
- 增加 `schema`、`key_columns`、`schema_fingerprint`、`validated_at`；
- 新安装不包含任何候选表；
- `build_middle_connect_yaml()` 生成空 `tables`；
- 删除便携包中的 `app/erp-configs` 目录创建、复制和完整性检查；
- 删除示例配置中关于随包 ERP profile 的说明；
- 删除依赖默认 ERP 表清单的测试，改为验证新安装表清单为空；
- 新增纯连接测试；
- 删除旧 `POST /api/test-connection`；
- 定义配置修订号和原子更新。

验收：

- 空表清单可以保存并启动；
- 连接成功但表未配置时状态明确；
- 安装包中不存在 `erp-configs/digiwin_e10.yaml`；
- 平台配置不包含 ERP 表和 ERP 连接占位字段；
- 配置冲突返回 `409`。

### M2：SQL Server 元数据发现后端

新增或修改：

- 新增 `data2agent/connect/metadata.py`
- 新增元数据领域模型、`MetadataDiscoverer` 协议和 discoverer 工厂/能力解析；
- 修改 `data2agent/connect/adapters/mssql.py`
- 修改 `data2agent/connect/adapters/sqlite.py` 或新增测试替身；
- 修改 `data2agent/middle_admin/app.py`

任务：

- 定义与数据库类型无关的 schema、表、字段、键、索引和校验结果模型；
- 实现 `MetadataDiscoverer` 协议，管理 API 和扫描任务只依赖该协议；
- 实现 `MssqlMetadataDiscoverer`，SQL Server 系统表查询只存在于该实现中；
- 实现 SQLite 测试实现或协议测试替身；
- adapter 不支持发现能力时返回 `metadata_discovery_unsupported`；
- 实现表、视图、字段、PK、唯一索引和外键查询；
- 实现进程内扫描任务、TTL 缓存、数量上限和超时；
- 实现业务键唯一性与 NULL 校验；
- 实现水位候选和字段类型校验；
- discoverer 自行管理连接，不向 API、缓存或调用方暴露原始连接对象；
- 所有异常输出脱敏。

验收：

- 可在没有任何 `tables` 配置时扫描；
- 元数据 API 测试可替换 discoverer，不依赖真实 SQL Server；
- `metadata.py` 不按 adapter 名称分支，也不包含 SQL Server 系统表 SQL；
- 元数据权限不足与连接失败可区分；
- 不支持发现能力的 adapter 返回稳定错误码；
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

- `data2agent/connect/sync.py`
- `data2agent/connect/__main__.py`
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
- 增加严格的 `ingest_protocol_version` 校验；
- 将表级 `mode: full_refresh` 从旧的全量读取/upsert 切换到快照生命周期；
- 删除 CLI `sync --full` 和 `full_sync()`，禁止运行时覆盖逐表同步模式；
- 删除仅服务旧全量 upsert 行为的测试和分支。

验收：

- 中途失败仍读取上一完整 raw 表；
- 源表删除行后，下一次全量完成后平台不再保留该行；
- 同一 snapshot 重放结果不重复；
- 零行表可正确发布为空表；
- 无主键表可使用 `full_refresh`；
- CLI 不再接受 `sync --full`，运行模式只能来自逐表配置；
- 全仓不存在生产 `full_sync()` 调用；
- M0 至 M3 的旧 full refresh 回归测试替换为快照语义测试；
- M4 未全部通过前不得进行正式页面联调或制作发布包。

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

### M6：文档与全链路验收

修改：

- `deploy/build_portable.ps1`
- `scripts/check_portable_package.py`
- `scripts/smoke_admin_ui.py`
- 示例配置、Release 检查和 CI
- 第 12 节列出的项目文档

任务：

- 再次审计并确认旧配置字段、旧 API、旧 ingest 协议和相关测试均已删除；
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
- CI 和安装包检查全部通过。

### M7：展厅、Mock、演示资产生产化清理与发布

删除或迁移：

- 将 `data2agent/showroom/seed.py`、`e10_schema.py` 和 `seed_mssql.py` 中仍被测试需要的
  seed/schema 能力移到 `tests/fixtures/e10`；
- 将仍有价值的 `review_demo.py` 断言改写成正常测试；没有产品价值的演示渲染逻辑直接
  删除；
- 删除产品包中的 `data2agent/showroom`；
- 删除 `deploy/showroom-connect.yaml`、`deploy/demo.tape` 和根目录演示
  `docker-compose.yml`；
- 删除 `deploy/render_hero_svg.py` 等只服务展厅的脚本；
- 删除前端 Mock 运行模式、MSW handlers、场景切换器和相关依赖；
- 删除 README、设计文档和开发文档中的展厅/Mock 运行说明；
- 保留真正的 SQL Server 集成测试 compose，但改为读取 `tests/fixtures`。

任务：

- 删除前先用 `rg` 生成并保存迁移核对清单，至少覆盖 Python import、`python -m`
  入口、Compose command、配置文件路径、前端环境变量和文档命令；
- 用明确的测试 fixture 替换生产包内的 showroom import；
- 逐项更新直接依赖 seed 的 Console、MCP、lineage、mapping、publish、increment、
  reconcile、dead-stock 和 dataset 测试；
- 更新 `tests/integration/mssql/docker-compose.yml`，改用测试范围的 MSSQL seed
  入口，不再执行 `python -m data2agent.showroom.seed_mssql`；
- 更新 `scripts/smoke_admin_ui.py` 和 `console-ui/scripts/e2e-acceptance.mjs`，改用
  测试 fixture 或真实 API 准备数据；
- 删除 `data2agent/mcp_server/__main__.py` 等生产错误信息中的 showroom 启动提示；
- 删除 `VITE_CONSOLE_MODE=mock`、Mock 水印和场景切换逻辑；
- 删除只验证 Mock 场景的前端测试，保留真实 API 契约和组件状态测试；
- 确认 wheel、平台便携包和中间机便携包均不包含展厅或 Mock 代码；
- 运行完整 Python、前端、SQL Server、Docker、便携包和 Release 检查；
- 构建同一版本的平台包与中间机包并完成最终发布。

验收：

- `data2agent` 正式 wheel 不包含 `showroom`；
- 除本实施计划的历史说明外，全仓搜索不存在 `data2agent.showroom`、
  `showroom-connect.yaml`、`seed_mssql` 或 `VITE_CONSOLE_MODE`；
- Vue 生产与开发均只使用真实 API，不存在 Mock 模式；
- 仓库根目录不存在展厅启动入口；
- 测试 fixture 只位于 `tests` 范围；
- SQL Server 集成 compose 可独立完成建库、seed、元数据发现和同步测试；
- CI 与 Release 全部通过；
- 两个便携包来自同一提交、同一版本和同一 ingest 协议。

## 10. 测试计划

新增测试建议：

- `tests/fixtures/e10/`：从产品 `showroom` 迁移的测试 schema 和 seed；
- `tests/test_metadata_discoverer.py`：发现协议、工厂、能力缺失和连接边界；
- `tests/test_erp_metadata.py`：元数据模型、候选键和水位；
- `tests/test_middle_metadata_api.py`：扫描 API、权限、脱敏和缓存；
- `tests/test_middle_extraction_tables.py`：独立表计划 API、修订冲突和原子保存；
- `tests/test_composite_increment.py`：复合键增量边界；
- `tests/test_full_refresh_snapshot.py`：全量 staging、发布、失败和重放；
- `tests/test_platform_config.py`：平台专用配置模型及旧字段拒绝；
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

showroom 迁移必须额外覆盖当前直接引用它的测试与入口，包括：

- `tests/test_connect.py`、`tests/test_reconcile.py`、`tests/test_sink_ingest.py`；
- `tests/test_console*.py`、`tests/test_mcp*.py`；
- `tests/test_lineage*.py`、`tests/test_mapping*.py`；
- `tests/test_publish*.py`、`tests/test_dead_stock*.py`；
- `tests/test_build_dataset.py`、`tests/test_entry_dataset_publish.py`；
- `tests/integration/mssql/docker-compose.yml`；
- `scripts/smoke_admin_ui.py`、`console-ui/scripts/e2e-acceptance.mjs`。

实施 M7 时以实际 `rg` 结果为准；上述列表是最低覆盖范围，不是允许忽略新增引用的固定
白名单。

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
| 删除 `docs/superpowers/plans/2026-07-23-explicit-table-config.md` | 已完成且与干净切换冲突的旧实施计划 |

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
- 平台不存在 ERP 直连配置及 sync/reconcile 动作；
- `PlatformConfig` 不接受或静默忽略 ERP `sources` 配置；
- 元数据 API 只依赖 `MetadataDiscoverer`，SQL Server 查询封装在对应实现内；
- M0 至 M3 未产生任何对外发布包，正式版本只包含 M4 快照语义；
- 正式包不包含 showroom、Mock、演示配置或旧 PowerShell 安装入口；
- Python、前端/页面、SQL Server 集成、Docker/便携包和 Release 检查全部通过。

## 14. 推荐执行顺序

严格按 `M0 → M0-P → M1 → M2 → M3 → M4 → M5 → M6 → M7` 实施。

M0 先移除会与新设计竞争的旧事实来源；M0-P 完成平台和中间机职责切分；M2 依赖 M1
的空配置和连接结果契约；M3 依赖 M2 产生的键确认信息；M4 依赖 M3 的运行键模型，并在
同一里程碑删除 CLI 全量覆盖入口；M5 在 API 契约冻结后开发；M6 完成功能验收，M7
完成产品包清理和正式发布。不得先制作页面再反推同步协议，也不得发布 M0 至 M3 的中间
状态，否则会再次出现“界面可配置、运行时不支持”或“全量读取但不是完整快照”的状态。
