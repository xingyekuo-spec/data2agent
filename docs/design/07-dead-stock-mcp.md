# 呆滞库存 MCP 业务能力设计

> 日期：2026-07-23  
> 版本：v2.4  
> 状态：M3（R1、R3、R4、R6 与可消耗候选）已实现（本期不含权限能力）

## 1. 目标与边界

本项目将鼎捷 E10 中分散的库存、采购、生产、BOM 和 ECN 数据转换为稳定、可复现、可审计的 MCP 业务能力，供任意支持 MCP 的外部调用方使用。

项目止于 MCP 网关，不开发外部 Agent。

| 范围内 | 范围外 |
|--------|--------|
| MCP Server、对象和指标契约 | 外部 Agent、Hermes 应用及其部署 |
| E10 数据映射、字段标准化 | Prompt、意图识别和多轮对话 |
| 呆滞判定、金额和规则型归因计算 | Agent 工具编排和推理策略 |
| 证据链、置信度、计算版本和数据警示 | 自然语言报告和通知推送 |
| 参数校验、默认脱敏、限流和审计 | 用户、角色、组织及工厂级权限控制 |
| 契约测试、参考数据和 MCP 测试客户端 | ERP 写回和业务审批流 |
| 复用现有共享 Token 或网络访问边界 | 身份体系、单点登录、权限配置界面 |

## 2. 总体架构

```text
外部 MCP 调用方
        |
        v
data2agent MCP 网关
  query_objects / query_metrics
        |
        v
版本化业务对象与指标
        |
        v
数据平台预计算与发布快照
        |
        v
鼎捷 E10 ERP（只读数据源）
```

各层职责：

| 层 | 职责 |
|----|------|
| 外部调用方（项目外） | 选择和组合 MCP 能力，决定最终呈现方式 |
| MCP 网关 | 参数校验、脱敏、限流、证据固化、版本警示和审计 |
| 业务对象/指标层 | 提供业务语义、确定性公式、归因候选及证据 |
| 数据平台 | 同步 E10 数据，执行批量预计算，原子发布数据集快照 |
| 鼎捷 E10 | 业务事实来源，本项目只读访问 |

设计原则：

1. 对外返回业务对象和指标，不暴露 ERP 表名、GUID 或 SQL。
2. 可用公式或规则表达的逻辑在数据平台/对象层完成，不依赖调用方重复计算。
3. MCP 返回事实、计算结果、置信度和证据，不生成自然语言结论。
4. 数据过期、链路缺失和口径未校准必须结构化返回，不得静默降级。
5. 所有查询读取同一版本的 published 快照，结果可追溯、可复现。
6. 本期运行在受控网络和共享访问域内，不按用户、角色或工厂实施授权隔离。

## 3. 业务口径

### 3.1 呆滞库存判定

最后出库日期必须覆盖销售出库、生产领用和已确认纳入库存口径的其他出库类型，不使用包含入库异动的 `LAST_TX_DATE`。

```text
last_issue_date = max(
  last_sales_issue_date,
  last_production_issue_date,
  last_misc_issue_date
)

age_anchor_date = coalesce(last_issue_date, first_stock_in_date)
dead_stock_days = as_of_date - age_anchor_date
is_dead_stock = usable_inventory_qty > 0
                and dead_stock_days > threshold_days
```

从未出库物料使用首次入库日期作为账龄起点。首次入库日期也不可得时，返回 `determination_status=unknown`，不得直接判为呆滞。

冻结、待检、报废等库存分别返回 `inventory_status`，是否计入呆滞金额由已校准的口径配置决定。

### 3.2 R2 采购超采

MOQ 强制超采与人工超采必须分开计算：

```text
demand_qty = 可追溯的有效需求量
net_received_qty = received_qty - returned_qty
actual_excess_qty = max(net_received_qty - demand_qty, 0)
moq_order_qty = ceil(demand_qty / moq) * moq
planned_moq_excess_qty = max(moq_order_qty - demand_qty, 0)
moq_forced_excess_qty = min(actual_excess_qty, planned_moq_excess_qty)
manual_excess_qty = max(actual_excess_qty - moq_forced_excess_qty, 0)
```

`moq` 缺失、为零或单位未对齐时不得计算。库存无法批次级关联采购单时，只能返回间接证据并降低置信度，不能声称某一采购单必然形成当前库存。

### 3.3 R5 生产损耗/超领

不能直接比较 `ISSUED_QTY` 与单台 `QTY_PER`：

```text
net_issued_qty = issued_qty - returned_qty
standard_required_qty = output_basis_qty * qty_per / denominator
allowed_issue_qty = standard_required_qty * (1 + allowed_loss_rate)
                    + fixed_loss_qty
excess_issue_qty = max(net_issued_qty - allowed_issue_qty, 0)
```

已结案工单使用合格产出量作为 `output_basis_qty`；未结案工单需计入在制需求并返回 `calculation_status=provisional`。单位换算、替代料领用、补料、退料和报废单据必须纳入口径。

### 3.4 其他归因规则

| 编号 | 根因 | 最低判定要求 | 默认置信度上限 |
|------|------|--------------|----------------|
| R1 | 客户订单取消/减量 | 有效需求取消/减量发生在备料或采购后，并存在可追溯数量关系 | HIGH |
| R2 | MOQ 强制超采 | MOQ、需求、净收货和单位均有效 | MEDIUM |
| R2M | 人工超采 | 净收货量超过 MOQ 向上取整量 | MEDIUM |
| R3 | ECN 变更未消化 | 旧料被替换、新料已生效，且处置方式不是“用完为止” | HIGH |
| R4 | 特殊使用条件 | 结构化限制或维护过的知识项命中 | LOW |
| R5 | 生产损耗/超领 | 标准需求、净领料、允许损耗和工单状态完整 | MEDIUM |
| R6 | 疑似重复建料 | 规格归一化键或已审批物料映射命中 | LOW |

R6 只返回“重复建料候选”。本项目不调用 LLM 做语义判定；未经主数据确认，不提升为确定根因。

所有规则独立评估，允许多标签。每个标签必须自带 `evidence`、`related_department` 和 `related_employee`；顶层人员字段不得代替各标签自己的关联主体。

## 4. MCP 契约

### 4.1 暴露方式

本场景复用现有 MCP 网关，不新增一组平行的顶层工具：

| MCP Tool | 用途 | 本场景要求 |
|----------|------|------------|
| `query_objects` | 查询明细对象和证据 | 无参返回目录；有参执行白名单筛选、排序和 limit；M1 增加游标分页 |
| `query_metrics` | 查询汇总指标 | 无参返回口径；有参按允许维度聚合 |
| `propose_action` | 固化“说”档建议卡 | 现有网关能力，不属于本场景 MVP 验收依赖 |

对外使用 `item_code`、`plant_id`、`warehouse_code`、单据编号等业务标识。E10 GUID 和表关联只存在于 binding 实现中。

### 4.2 对象目录

| 对象 | 粒度 | 主要筛选项 | 核心字段 |
|------|------|------------|----------|
| `DeadStockItem` | 工厂+仓库+物料 | 工厂、仓库、品号、物料类型、数据日期 | 库存量、成本、金额、阈值、账龄起点、呆滞天数、判定状态 |
| `DeadStockAttribution` | 工厂+物料+批次+标签 | 工厂、品号、根因、置信度、批次 | 根因、规则版本、置信度、证据、关联部门/经办人 |
| `MaterialOrderEvidence` | 工厂+物料+单据行 | 工厂、品号、单据类型、日期范围 | 销售、采购、工单来源关系和数量时间线 |
| `PurchaseOverbuyEvidence` | 工厂+采购行 | 工厂、品号、采购单、供应商、日期范围 | 需求、净收货、MOQ、强制超采、人工超采、单位 |
| `ProductionLossEvidence` | 工厂+工单+物料 | 工厂、品号、工单、日期范围 | 产量基数、标准需求、净领料、允许损耗、超额领料 |
| `SpecialConditionEvidence` | 工厂+物料+BOM 主件 | 工厂、品号、主件 | BOM 备注限制、识别方式、用量、低置信警示 |
| `DuplicateMaterialCandidate` | 呆滞品号+候选品号 | 品号、候选品号 | 规范化规格、匹配方式、低置信警示 |
| `MaterialBomUsage` | 工厂+物料+BOM 行 | 工厂、品号、有效日期 | 当前/历史机型、用量、有效状态、在产需求 |
| `EcnChangeEvidence` | 工厂+ECN 变更行 | 工厂、旧/新品号、日期范围 | 旧料、新料、生效日、处置方式、原因、关联主体 |
| `MaterialSubstituteCandidate` | 来源工厂+物料+候选用途 | 工厂、品号、目标工厂 | 替代依据、有效性、库存状态、在产需求、预计消耗量、限制条件 |

库存到单据若只能按物料和时间窗口间接关联，对象必须返回：

- `trace_type`: `direct` 或 `indirect`
- `trace_coverage`: 本次查询可覆盖的数据范围
- `caveats`: 断链、缺失状态和其他解释限制

### 4.3 指标目录

| 指标 | 公式概要 | 允许分组 |
|------|----------|----------|
| `dead_stock_quantity` | 呆滞库存数量合计 | 工厂、仓库、物料类型、账龄段 |
| `dead_stock_amount` | `inventory_qty * unit_cost` | 工厂、仓库、物料类型、账龄段、根因 |
| `dead_stock_item_count` | 呆滞品号去重数 | 工厂、仓库、物料类型、根因 |
| `new_dead_stock_amount` | 本批次新进入呆滞状态的金额 | 日期、工厂、物料类型 |
| `attribution_coverage_rate` | 至少一个有效标签的品号数/呆滞品号数 | 工厂、置信度等级 |
| `attribution_distribution` | 各根因命中的品号数和金额 | 工厂、根因、置信度等级 |
| `substitute_consumable_quantity` | 当前 BOM 与未结案工单约束下的预计可消耗量 | 来源工厂、目标工厂、物料 |

每个指标声明 `formula`、`grain`、`allowed_group_by`、`status` 和 `calculation_version`。多标签指标存在重复计数，各根因金额不能直接相加作为总额。

### 4.4 通用响应

```json
{
  "object": "DeadStockAttribution",
  "display_name": "呆滞库存归因",
  "rows": [],
  "meta": {
    "query_id": "q_...",
    "result_digest": "sha256:...",
    "dataset_version": "2026-07-23T02:00:00+08:00",
    "calculation_version": "dead-stock-v1",
    "as_of_time": "2026-07-23T02:00:00+08:00",
    "masked_fields": [],
    "warnings": [],
    "caveats": []
  }
}
```

`DeadStockAttribution.rows[*]` 的标签结构：

```json
{
  "root_cause": "R2",
  "confidence": 0.7,
  "confidence_level": "MEDIUM",
  "rule_version": "r2-v1",
  "trace_type": "indirect",
  "evidence": {
    "po_no": "PO-2025-1120",
    "demand_qty": 2000,
    "net_received_qty": 5000,
    "moq": 5000,
    "moq_forced_excess_qty": 3000,
    "manual_excess_qty": 0
  },
  "related_department": {"id": "...", "name": "采购部"},
  "related_employee": {"id": "...", "name": "***"},
  "warnings": ["当前库存与采购单为物料级间接关联"]
}
```

### 4.5 查询限制和错误语义

| 条件 | 行为 |
|------|------|
| 传入 `plant_id` | 作为普通业务维度执行等值筛选，不做授权判断 |
| 未传 `plant_id` | 查询当前实例已发布范围内的数据，仍受单次结果上限约束 |
| published 数据集不存在 | fail closed，不回退未版本化表 |
| 数据超过新鲜度阈值 | 返回数据并附 `stale_dataset` 警示；超过硬上限则拒绝 |
| 口径为 `draft` | 返回结果并贯穿 draft 警示 |
| 必要字段或单位缺失 | 对应计算状态为 `unknown`，不得用零代替 |
| 单次结果过大 | 使用游标分页；不得截断后假装完整 |
| 查询超时 | 返回结构化 `query_timeout`，不生成部分结论 |

## 5. 数据实现

### 5.1 来源链路

真实表名和外键必须在现场数据字典核对后进入 verified binding。设计阶段的主要候选来源：

| 领域 | 候选来源 |
|------|----------|
| 库存与成本 | `INV_COST_BAL`、`INV_SUMMARY`、`INV_UNIT_COST`、仓库/批号状态表 |
| 销售需求与出库 | `SALES_ORDER_DOC*`、`SALES_DELIVERY*`、`SALES_ISSUE*`、`SALES_RETURN*` |
| 采购与需求来源 | `PURCHASE_ORDER*`、`PO_REQ_SOURCE`、`PURCHASE_GOODS*`、`PURCHASE_RETURN*`、`SUPPLIER_PURCHASE` |
| 生产需求与领退料 | `MO`、`MO_D`、`MO_DEMAND`、`MO_ISSUED_SETS`、`MO_CHANGE*`、`MO_RECEIPT*` |
| BOM 与替代 | `BOM_D`、`BOM_PRODUCT`、`ITEM_MAPPING*` |
| ECN | `ECN`、`ECN_D`、`ECN_SD`、`ECN_TASK` |
| 主数据与组织 | `ITEM_FEATURE`、`ITEM_PLANT`、部门/员工维度 |

当前文档中的关联路径均为候选，不视为已验证事实。尤其需要核对 `PO_REQ_SOURCE`、采购多级子表和工单领退料的真实外键。

### 5.2 YAML 模板与抽取配置

对象目录必须落实为模板包中的 YAML，不能只存在于本文。计划新增：

| 文件 | 阶段 | 作用 |
|------|------|------|
| `templates/objects/dead_stock_item.yaml` | M1 | 定义 `DeadStockItem` 对象、业务键、属性、敏感字段和 binding |
| `templates/objects/dead_stock_attribution.yaml` | M2-M3 | 已实现：定义 `DeadStockAttribution` 及逐标签证据字段，M3 起包含 R4/R6 |
| `templates/objects/material_order_evidence.yaml` | M3a | 已实现：定义 R1 销售/采购来源证据对象 |
| `templates/objects/purchase_overbuy_evidence.yaml` | M2 | 已实现：定义 R2/R2M 计算输入和结果字段 |
| `templates/objects/production_loss_evidence.yaml` | M2 | 已实现：定义 R5 计算输入和结果字段 |
| `templates/objects/special_condition_evidence.yaml` | M3b | 已实现：定义 R4 特殊使用条件候选 |
| `templates/objects/duplicate_material_candidate.yaml` | M3b | 已实现：定义 R6 同规格重复料候选 |
| `templates/objects/material_bom_usage.yaml` | M3c | 已实现：定义 BOM 使用和未结案工单需求 |
| `templates/objects/ecn_change_evidence.yaml` | M3a | 已实现：定义 R3 ECN 变更证据 |
| `templates/objects/material_substitute_candidate.yaml` | M3c | 已实现：定义可消耗/转用候选及限制条件 |
| `templates/metrics/dead_stock.yaml` | M1-M3 | 已实现：定义本场景指标、维度、口径状态、新鲜度和 caveats |
| `templates/pack.yaml` | 各阶段 | 每次模板集合发生兼容性变化时更新模板包版本和迁移说明 |

模板与抽取配置职责不同：

| 配置 | 位置 | 职责 |
|------|------|------|
| 对象/指标模板 | 数据平台 `templates/` | 业务字段、类型、键、敏感性、关系、口径和 raw-to-object binding |
| ERP 抽取表配置 | 中间机 `connect.yaml` 的 `sources.digiwin_e10.tables` | 允许读取的物理表、全量/增量模式和水位字段 |

新增 binding 依赖的每张 E10 表都必须同步加入中间机显式 `tables` 清单；平台 Validation 对比两者，缺表时阻止发布或明确标记数据陈旧。不得继续依赖模板自动扩大中间机白名单。

当前 `field_map` 只支持直取、单键 join、枚举 map 和简单 `derived` 决策表，不能表达跨表聚合、MOQ 公式或生产损耗公式。因此：

1. 复杂计算由版本化的呆滞库存预计算任务实现，并用代码和测试锁定。
2. 预计算结果落入稳定的场景结果表，再由上述对象 YAML 做一对一字段 binding 和发布。
3. 不在 YAML 中嵌入 SQL、脚本或临时表达式语言。
4. 新 binding 初始状态一律为 `draft`；现场核对表、字段、状态码、单位和水位后才可改为 `verified`。
5. `templates/metrics/dead_stock.yaml` 只声明口径；可执行聚合在 MCP 指标实现注册表中实现，并以测试证明与声明一致。

### 5.3 预计算产物

每日任务按同一批次生成并原子发布：

1. `MaterialLastIssue`: 各出库类型最后日期、首次入库日期和账龄锚点。
2. `DeadStockItem`: 呆滞判定、数量、成本、金额及状态。
3. `DeadStockAttribution`: 多标签规则结果和逐标签证据。
4. `MaterialBomUsage`: 当前 BOM 使用关系和未结案工单潜在需求。
5. `MaterialSubstituteCandidate`: 基于 BOM 与未结案工单的可消耗候选。
6. 趋势快照: 新增、退出和持续呆滞的批次差异。

所有产物携带 `etl_batch_id`、`dataset_version`、`calculation_version`、`calculate_time` 和来源水位。发布必须是原子的，避免对象与指标读取不同批次。

`MaterialLastIssue` 至少包含：

- `last_sales_issue_date`
- `last_production_issue_date`
- `last_misc_issue_date`
- `first_stock_in_date`
- `last_issue_date`
- `age_anchor_date`
- `determination_status`

## 6. 运行边界与治理

1. 业务源和 published 快照只读；本场景不写 ERP。
2. 本期不建设用户身份、角色、组织、数据行级或工厂级权限模型。
3. `plant_id` 仅为查询和统计维度，不作为授权边界；能访问同一 MCP 实例的调用方可以查询该实例全部已发布工厂数据。
4. 访问入口复用现有共享 Token、内网隔离或外部反向代理，不在本场景新增认证与权限管理功能。
5. 员工姓名和联系方式继续使用现有默认脱敏能力，`meta.masked_fields` 明示；本期不提供按权限解敏。
6. 审计继续记录共享主体、会话、规范化查询、数据集版本、结果摘要和警示，不记录明文敏感结果。
7. 跨机访问仍应使用 HTTPS；匿名模式只用于本地参考数据。
8. `related_employee` 表示关联经办人，不等同于责任认定。

当前限制：本期不承诺不同调用方之间的数据隔离。部署方必须确保 MCP 实例只暴露给允许访问其全部 published 数据的受控调用方。用户级和工厂级授权列入后续版本，启用前需要补充凭据到权限范围的映射、服务端强制过滤和越权审计。

## 7. MVP 与验收

### 7.1 前置 POC

先按根因和物料类型分层选择 10-20 个已知案例，核对：

- 库存到采购、销售、工单和 ECN 的真实关联路径
- 销售、生产和其他出库类型是否完整
- 首次入库日期能否覆盖从未出库物料
- MOQ、数量和单位换算的数据质量
- 领料、退料、补料、报废和产出的计算口径

链路覆盖不足时必须降低对应规则置信度上限，并通过 `caveats` 对外声明。

### 7.2 分阶段交付

| 阶段 | 内容 | 完成标准 |
|------|------|----------|
| M0 口径与映射 | 建立 draft YAML、显式抽取表清单，现场核对字段、外键、状态码、单位和公式 | 模板校验通过，binding verified，口径评审通过 |
| M1 基础查询 | `DeadStockItem` YAML、场景指标 YAML 和对应可执行实现 | 对象、指标、分页和证据契约测试通过 |
| M2 核心归因 | R2/R2M、R5 及对应证据对象 | 已实现；待测试 ERP 人工样本核对公式与关联覆盖率 |
| M3 扩展归因 | R1、R3、R4、R6 候选和替代用途对象 | 已实现；R4/R6 为 LOW 候选，M3c 可消耗候选不等同于已确认替代 |
| M4 运行验收 | 定时预计算、原子发布、监控和恢复 | 连续运行两周，无跨批次或半发布结果 |

### 7.3 验收指标

正式验收使用不少于 50 个分层人工确认案例：

| 维度 | 标准 |
|------|------|
| 对象字段正确率 | 必填业务字段与人工核对一致率 100% |
| 核心公式正确率 | 呆滞天数、金额、R2 和 R5 测试样本 100% 一致 |
| 归因准确率 | HIGH/MEDIUM 标签与人工结论命中率不低于 80% |
| 归因覆盖率 | 无有效标签的呆滞品号不高于 30%，并允许按链路现状调整目标 |
| 证据完整性 | 每个标签均有规则版本、证据、追溯类型和警示 |
| 数据时效性 | ERP 日结后两小时内发布；响应明确给出数据时间点 |
| 默认脱敏 | 模板标记的敏感字段在 MCP 响应中全部脱敏 |
| 可复现性 | 同一数据集版本和规范化查询产生相同结果摘要 |
| 性能 | 常规查询 P95 目标在 POC 后按现场数据量确定 |

验收范围只覆盖 MCP 数据与契约质量，不评估外部 Agent 的回答、编排或报告效果。

## 8. 待业务确认

- 各物料类型的呆滞天数阈值
- “其他出库”纳入的单据类型和状态
- 从未出库物料的首次入库/期初库存取数规则
- 冻结、待检、报废库存的金额口径
- R2 有效需求量和净采购量的最终取数口径
- R5 未结工单在制需求、允许损耗和单位换算规则
- 低订单机型阈值及 R4 结构化知识维护方式
- 数据新鲜度软阈值和拒绝服务硬阈值
- 各规则置信度权重及校准流程

后续权限版本再确认：调用主体模型、角色与组织关系、工厂可见范围、敏感字段解敏规则和越权审计要求。
