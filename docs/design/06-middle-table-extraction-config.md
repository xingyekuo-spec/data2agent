# 06 · 中间机独立抽取表配置实施方案

> 状态:待实施 · 目标版本:v0.4 · 制定日期:2026-07-23
>
> 本文是实施方案,不是当前功能说明。完成本文全部验收项前,现行行为仍以
> [02 抽取框架](02-extraction.md)和实际代码为准。

## 1. 背景

当前中间机通过 `connect.yaml` 配置 ERP 连接、调度、限流和平台接收地址,但实际抽取表范围
仍由随程序发布的模板 binding 推导:

- `whitelist_from_bindings: true` 表示从模板的 `binding.tables` 生成白名单;
- `extra_whitelist` 只能追加表;
- 增量水位仍由模板的 `binding.watermark` 决定;
- HTTP 推送模式启动时仍加载模板包。

因此现场要调整 ERP 抽取表或水位字段时,仅修改中间机配置并不能完整生效,常常还需要更新
模板或程序包。中间服务器位于公司内网且更新不便,抽取计划应当能够在中间机本地独立维护。

## 2. 目标与非目标

### 2.1 目标

1. 使用 `sources.<source>.tables` 作为抽取范围和每表同步策略的唯一配置来源。
2. 删除 `whitelist_from_bindings` 和 `extra_whitelist` 两个配置字段。
3. 适配器继续强制执行白名单,但白名单改为由 `tables.keys()` 直接生成。
4. HTTP 推送模式不再依赖模板即可启动、同步和推送。
5. 中间机管理页面支持新增、修改、删除和验证 ERP 表配置。
6. 为现有 `connect.yaml` 提供可审计、可回滚的一次性迁移工具。
7. 让部署运行、命令行、管理页面、验收报告和文档使用同一配置语义。

### 2.2 非目标

- 不删除适配器的只读限制、表白名单检查、限流或 SQL 审计。
- 不把对象字段映射移到中间机;binding 仍由平台维护并负责 raw 到业务对象的映射。
- 不因删除某个 `tables` 配置而自动删除平台已有 raw 数据。
- 不在本改造中实现 schema drift 自动修复、E6b 跨机对账或批次 commit receipt。
- 不在本改造中增加 ERP 写回能力。

## 3. 核心设计决定

### 3.1 单一事实来源

抽取和映射采用两个清晰分离的事实来源:

| 职责 | 唯一事实来源 | 所在机器 |
| --- | --- | --- |
| 允许读取哪些 ERP 表 | `connect.yaml` 的 `tables` | 中间机 |
| 每张表使用增量还是全量刷新 | `connect.yaml` 的 `tables` | 中间机 |
| 增量表使用哪个水位字段 | `connect.yaml` 的 `tables` | 中间机 |
| raw 字段如何映射为业务对象 | 模板 binding | 数据平台 |
| 对象校验、隔离、发布和回滚 | 模板与平台发布元数据 | 数据平台 |

以后平台修改字段映射不要求中间机更新。只有确实需要增加、停止或改变 ERP 表抽取策略时,
才修改中间机 `connect.yaml` 并重启 connector。

### 3.2 白名单保护保留

“删除 `whitelist_from_bindings`”只表示删除配置开关,不表示取消安全边界。适配器的白名单必须
继续存在:

```text
adapter whitelist = set(source_config.tables.keys())
```

任何不在 `tables` 中的表名仍由适配器拒绝。代码、CLI、管理页面或对账流程都不得绕过该集合。

### 3.3 不保留第三种追加入口

`extra_whitelist` 同时删除。若保留它,抽取范围仍有两个来源,配置人员无法只看 `tables` 判断
中间机会访问哪些 ERP 表。

### 3.4 模板依赖边界

- `sink.type: http`:中间机抽取、推送和本地状态维护不加载模板。
- `sink.type: local` 且 `apply_after_sync: true`:同步范围仍来自 `tables`,同步结束后才加载模板执行映射。
- 平台 ingest、apply、Console 和 MCP 继续使用模板完成业务对象相关工作。
- 从模板生成初始表配置只发生在首次安装或显式迁移时,不是运行时回退逻辑。

## 4. 目标配置格式

```yaml
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN

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
    rate:
      batch_size: 5000
      rows_per_second: 2000
    lookback: 3d
    sync_every: 30m
    sink:
      type: http
      url: http://平台IP:8850
      token_env: D2A_INGEST_TOKEN
```

### 4.1 字段语义

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `tables` | 是 | 非空映射;键为源库物理表名 |
| `mode` | 是 | `incremental` 或 `full_refresh` |
| `watermark` | 条件必填 | `incremental` 必填;值为源表物理水位列名 |

不增加 `enabled` 字段。表存在于 `tables` 中即启用;删除对应节点即停止后续抽取。

### 4.2 校验规则

1. 每个数据源的 `tables` 必须非空。
2. 表名和水位列名必须符合受支持的 SQL 标识符格式。
3. 表名按大小写折叠后不得重复。
4. `incremental` 必须配置非空 `watermark`。
5. `full_refresh` 不允许配置 `watermark`,避免配置含义冲突。
6. 未知配置字段直接报错,不得静默忽略。
7. 连接测试必须验证表存在;增量表还必须验证水位列存在。
8. 增量表必须满足现有增量引擎对主键的要求;不满足时连接测试失败并给出表名。

### 4.3 删除表的行为

从 `tables` 删除一张表后:

- 下一轮起中间机不再查询该表;
- 适配器拒绝任何针对该表的直接抽取或回填请求;
- 已保存的本地水位、审计记录和平台 raw 数据不自动删除;
- 若平台启用的 binding 仍依赖该表,Validation 必须报告缺失或陈旧数据,由管理员决定恢复配置、
  停用 binding 或执行单独的数据清理。

## 5. 代码实施范围

### 5.1 配置模型

修改 `data2agent/connect/config.py`:

- 新增 `TableExtractConfig`;
- `SourceConfig` 新增必填 `tables: dict[str, TableExtractConfig]`;
- 删除 `whitelist_from_bindings` 和 `extra_whitelist`;
- 增加表名、模式、水位字段和未知字段校验;
- 提供从配置生成白名单和水位映射的公共方法或纯函数。

目标结果:

```python
whitelist = set(source_config.tables)
watermarks = {
    table: spec.watermark
    for table, spec in source_config.tables.items()
    if spec.mode == "incremental"
}
```

### 5.2 调度与运行时解耦

修改 `data2agent/connect/scheduler.py`:

- `build_adapter()` 只使用 `tables` 构建白名单;
- `run_sync_cycle()` 和 `run_reconcile_cycle()` 只使用配置水位;
- HTTP 推送模式不再为了抽取加载模板;
- local 模式只在执行 apply 时加载模板;
- 日志增加已配置表数量,但不记录凭据。

需要相应调整函数边界,避免把 `TemplatePack` 作为 HTTP 抽取路径的必需参数。

### 5.3 CLI 统一

修改 `data2agent/connect/__main__.py`:

- `sync`、`reconcile` 和 `backfill` 统一从 `--config` 加载连接与表策略;
- 使用 `--source` 选择数据源;
- `sync --full` 只临时忽略水位,仍不能访问 `tables` 之外的表;
- `backfill` 只接受配置为 `incremental` 的表;
- `apply` 继续使用模板,因为它属于映射阶段;
- 帮助文本删除“由 binding 推导白名单/水位”的描述。

现有 `whitelist_from_pack()` 和 `watermarks_from_pack()` 可暂时保留为首次配置、迁移和测试工具,
但不得继续参与生产抽取路径。待调用点完成收敛后再判断是否删除。

### 5.4 中间机管理 API 与页面

修改:

- `data2agent/middle_admin/app.py`;
- `data2agent/middle_admin/templates/config.html`;
- `data2agent/admin_common/config_edit.py`。

管理页面用表格编辑器替换“额外抽取表”文本框,至少支持:

- 展示 ERP 表名、同步模式和水位字段;
- 新增表;
- 修改模式和水位字段;
- 删除表;
- 整体保存 `tables`;
- 保存前进行结构校验;
- 连接测试逐表确认表、主键和水位字段。

配置合并器必须把 `sources.<source>.tables` 当作一个可原子替换的受控子树,以支持真正删除表;
不能仅合并叶子字段,否则被删除的旧表会残留在 YAML 中。其他不可编辑字段和凭据继续受保护。

### 5.5 首次安装与默认配置

修改:

- `data2agent/admin_common/setup_yaml.py`;
- `deploy/setup-middle.ps1`;
- `connect.example.yaml`;
- `deploy/showroom-connect.yaml`。

首次安装时将当前 E10 基线表写入 `tables`。Python 浏览器安装流程可从随包模板生成一次初始值;
PowerShell 安装脚本应生成等价的显式配置。生成完成后,运行时不再跟随模板自动改变抽取范围。

平台首次配置生成的配置也必须包含显式 `tables`,或者将平台非抽取配置拆成独立模型;本改造优先采用
前者,避免扩大配置模型重构范围。

### 5.6 Validation 与状态展示

修改 `data2agent/console/validation.py` 以及引用相关检查名称的测试/API 契约:

- “只读适配器与模板白名单”改为“只读适配器与显式表清单”;
- 检查 `tables` 非空且配置可加载;
- 对比平台启用 binding 依赖的表与中间机已配置表;
- 缺表时显示具体表名;
- 表已停止抽取但平台仍有历史 raw 时,不能把历史存在误判为当前正常。

## 6. 旧配置迁移方案

### 6.1 迁移入口

新增显式命令:

```powershell
data2agent.exe connect migrate-config --config C:\ProgramData\data2agent\config\connect.yaml
```

源码运行等价入口:

```bash
python -m data2agent.connect migrate-config --config connect.yaml
```

### 6.2 迁移步骤

1. 读取旧 YAML 原文,识别 `whitelist_from_bindings` 和 `extra_whitelist`。
2. 在同目录生成 `connect.yaml.bak-YYYYMMDD-HHMMSS`。
3. 按旧运行语义计算实际表集合:
   - `whitelist_from_bindings: true`:启用 binding 表集合加 `extra_whitelist`;
   - `whitelist_from_bindings: false`:只使用 `extra_whitelist`。
4. 对模板中有水位声明的表生成 `mode: incremental` 和对应 `watermark`。
5. 其余表生成 `mode: full_refresh`。
6. 删除两个旧字段并写入 `tables`。
7. 使用新配置模型校验,失败则恢复备份。
8. 输出转换后的表、模式和水位摘要,提醒管理员复核后重启服务。

### 6.3 兼容原则

- 新运行时模型不保留两个旧字段。
- 检测到旧字段或缺少 `tables` 时 fail-fast,并显示迁移命令。
- 不允许静默回退到模板推导,否则中间机仍无法独立控制抽取范围。
- 迁移工具是唯一允许读取旧字段的兼容代码,后续版本可在迁移窗口结束后删除。

## 7. 测试方案

### 7.1 配置单元测试

- 接受合法的增量表和全量表配置;
- 拒绝空 `tables`;
- 拒绝增量表缺少水位;
- 拒绝全量表携带水位;
- 拒绝非法或大小写重复的标识符;
- 拒绝旧字段和未知字段;
- 验证配置生成的白名单、水位映射完全正确。

### 7.2 调度与抽取测试

- 配置中只有两张表时只查询并推送这两张表;
- 配置外表由适配器拒绝;
- 增量表推进水位;
- 全量表不创建水位;
- 修改配置后重启,新增表开始抽取、删除表停止抽取;
- HTTP 推送模式在模板目录不存在时仍能运行;
- local + apply 模式仍能加载模板并发布数据集;
- sync、reconcile、backfill 使用同一表策略。

### 7.3 管理页面和安装测试

- 配置 API 返回结构化 `tables`;
- 页面能新增、修改和删除表;
- 删除操作在 YAML 中真正删除对应节点;
- 非受控字段和凭据不能通过配置 API 修改;
- 连接测试能发现不存在的表、水位列或主键;
- 浏览器首次安装和 PowerShell 安装生成等价的六张基线表。

### 7.4 迁移测试

- `whitelist_from_bindings: true/false` 两种旧配置均正确迁移;
- `extra_whitelist` 被保留为显式表;
- 有水位和无水位表分别得到正确模式;
- 迁移前创建备份;
- 校验失败恢复原配置;
- 重复执行迁移命令保持幂等或明确报告“无需迁移”。

### 7.5 回归测试

- 运行连接、推送、ingest、映射、Console Validation 和中间机管理相关测试;
- 运行完整 Python 测试集;
- 运行 Console 前端测试与生产构建;
- 使用本地 E10-like 参考库完成一次端到端同步、推送、apply 和对象读取。

## 8. 验收标准

全部满足后才可声明改造完成:

1. 代码和示例配置中不再存在运行时 `whitelist_from_bindings`、`extra_whitelist`。
2. 中间机 HTTP 推送模式没有模板目录也能正常启动和同步。
3. 中间机只查询 `tables` 明确列出的 ERP 表。
4. 管理页面可独立完成表配置增删改与连接验证。
5. 新增或删除表只需修改中间机配置并重启服务,无需升级程序包。
6. 增量和全量策略严格按 `tables` 执行。
7. 旧配置迁移有备份、失败恢复和清晰报告。
8. 平台 binding 缺少所需 raw 表时,Validation 明确失败并列出表名。
9. 所有相关自动化测试和端到端参考链通过。
10. 第 10 节列出的文档全部完成同步更新并通过全文检索复核。

## 9. 实施顺序与提交边界

建议按以下顺序实施,每一步保持可测试:

| 顺序 | 任务 | 建议提交边界 |
| --- | --- | --- |
| 1 | 新配置模型、校验和旧配置迁移器 | `feat: add explicit extraction table config` |
| 2 | 调度、适配器构建和 CLI 改用 `tables` | `refactor: decouple extraction from bindings` |
| 3 | 中间机管理 API、表格编辑器和连接验证 | `feat: manage extraction tables on middle server` |
| 4 | 首次安装、PowerShell、示例和参考链配置 | `chore: generate explicit middle table plans` |
| 5 | Validation、测试和文档收口 | `test: validate explicit extraction table plans` |

实施时应保留工作区内与本改造无关的现有修改,不要混入上述提交。

## 10. 需要同步更新的文档

以下文档必须在代码实施过程中更新,不能只新增本文。

| 文档 | 必须更新的内容 | 完成判定 |
| --- | --- | --- |
| `README.md` | 中间机配置入口;说明 ERP 表在中间机 `tables` 中独立维护 | 不再暗示抽取表由模板自动决定 |
| `docs/roadmap.md` | 将本改造列为 v0.4 前置切片或对应里程碑任务;说明其与 M1/M3 的依赖 | 路线图能定位本方案并标明完成状态 |
| `docs/design/00-overview.md` | 架构职责边界:中间机维护抽取计划,平台维护映射模板 | 总览与本文的两类事实来源一致 |
| `docs/design/01-metamodel.md` | 删除 binding 是抽取白名单唯一来源的表述;保留其映射职责 | 不再把 binding 描述为中间机运行时抽取配置 |
| `docs/design/02-extraction.md` | 重写白名单、水位、配置、调度、安全机制、部署拓扑和现场核对相关段落 | 示例只使用 `tables`,不出现两个旧字段 |
| `docs/design/04-reference-chain.md` | 更新参考链连接配置和验证预期 | 参考链按显式六张表运行 |
| `docs/design/05-console.md` | 更新 middle_admin 配置能力,加入表格编辑与连接校验说明 | 管理界面能力与实现一致 |
| `docs/dict/digiwin_e10.md` | 标明物理表名和水位列应写入中间机 `tables`;区分参考表形与现场确认值 | 字典可直接用于现场配置核对 |
| `docs/runbook/portable.md` | 更新首次安装、旧配置迁移、表配置、重启和回滚步骤 | 现场人员不需要改源码或模板即可改表 |
| `docs/runbook/push-validation.md` | 增加表配置核对、表/水位字段连接测试和配置外表拒绝验收 | 验收记录能证明实际抽取范围 |
| `docs/runbook/source-dev.md` | 更新本地 connect.yaml 示例、CLI 命令和无模板 HTTP 推送验证方法 | 开发运行说明不再使用旧参数 |
| `connect.example.yaml` | 替换为完整 `tables` 示例并解释两种模式 | 文件可直接复制后修改使用 |
| `deploy/showroom-connect.yaml` | 写入参考链六张显式表及水位策略 | 参考链不依赖 binding 推导抽取范围 |
| `deploy/setup-middle.ps1` 内嵌说明 | 生成显式 `tables`;提示后续可在中间机管理页维护 | 新安装不再产生旧字段 |

### 10.1 文档全文复核

实施完成后至少执行以下语义检索并逐条处理:

```bash
rg -n "whitelist_from_bindings|extra_whitelist" README.md docs deploy connect.example.yaml
rg -n "白名单.*binding|binding.*白名单|模板.*推导.*表" README.md docs
```

允许旧字段只出现在本文的迁移说明和迁移测试说明中。其他当前状态文档、示例配置和安装脚本
不得继续出现旧配置。

## 11. 发布与回滚

### 11.1 发布前

1. 对目标中间机的 `connect.yaml` 和凭据文件分别备份。
2. 使用迁移命令生成新配置并人工核对表名、模式和水位字段。
3. 先运行连接测试,确认不会访问配置外表。
4. 使用 `serve --once` 或管理页面触发一次受控同步。
5. 在平台确认 raw 行数、最新批次和对象发布结果后再恢复常驻服务。

### 11.2 回滚

若新版本验证失败:

1. 停止 connector 服务;
2. 恢复旧程序版本;
3. 恢复迁移前 `connect.yaml.bak-*`;
4. 启动旧 connector;
5. 核对水位和最近成功批次。

配置迁移不删除 raw 数据和水位状态,但回滚前仍必须保留配置备份并核对当前程序版本支持的配置格式。
