# 01 · 元模型与模板包

> 状态:已实现,本文为契约定义(2026-07-10)· 实现:`data2agent/metamodel/`、`data2agent/mapping.py` · 消费者:映射引擎、MCP 网关、对账工具(规划)、白名单推导(规划)

## 1. 设计目标

元模型回答一个问题:**"什么是一个合法的对象模板 / 指标定义"**。它是全系统的单一事实来源 —— 任何组件需要知道"对象有哪些字段、字段从哪个源表来、什么算敏感、什么动作允许 Agent 做"时,只能问元模型,不允许各自维护副本。

刻意保持"薄":元模型只定义结构与校验,不含任何取数、转换逻辑。

## 2. 对象模板(ObjectTemplate)

| 字段 | 说明 | 设计理由 |
| --- | --- | --- |
| `object` / `display_name` / `description` | 对象标识与展示 | 目录自描述,Agent 无需外部文档 |
| `domain` | 销售 / 产品 / 供应 / 生产 / 资源 / 辅助 | 18 对象的分域导航 |
| `source_of_truth` | 权威来源的人话描述 | 多源冲突时的裁决依据 |
| `keys` | 业务键(必须是已声明属性) | 增量 upsert 与跨源对齐的锚点 |
| `properties` | 见 §2.1 | |
| `states` | 业务状态枚举 | 状态推导规则放 binding notes,不放元模型 |
| `relations` | 对象间引用(target 必须已定义) | 跨对象校验兜底 |
| `actions` | 动作 + 治理档位 `tier: 看/说/做` | 网关硬校验的依据,见 docs 03 |
| `bindings` | 源系统映射,见 §3 | |
| `knowledge_refs` | 行业知识包引用(仅名字) | 知识包本体为后续能力,模板只留挂点 |
| `custom_field_slots` | 默认 20 | 客户个性化字段进槽位,**模板永不分叉** |

### 2.1 属性(Property)

类型集:`string / text / int / decimal / money / date / datetime / bool / ref / enum`。约束:`ref` 必须声明目标对象;`enum` 必须声明取值。

`sensitive: true` 是**出网前置脱敏的依据**:被标记的属性,在任何离开数据层的通道(MCP 响应、导出、日志)默认脱敏。当前不提供解敏开关(解敏属"做"档治理,后续按权限模型提供)。

## 3. 源系统映射(SourceBinding)

```yaml
- source: digiwin_e10          # 数据源名,同时是适配器路由键
  tables: [SALES_ORDER, CURRENCY]   # tables[0] 为锚表:对象一行 = 锚表一行
  status: draft                # draft=参考表形推测;verified=现场数据字典核对固化
  key_map:   { order_no: SALES_ORDER.DOC_NO }
  field_map: { ... }           # 属性 → 映射表达式,文法见 §3.1
  watermark: SALES_ORDER.LAST_MODIFIED_DATE   # 增量水位字段
  notes: 状态推导规则等人读信息
```

一个对象可挂多个 binding(如 SalesOrder 同时有易飞与 E10);运行期由 `source` 选择。

### 3.1 映射表达式文法

`key_map` / `field_map` 的值遵循如下文法(解析器:`data2agent/mapping.py::parse_field_expr`):

```
表.字段                                    直取锚表字段
表.字段 (join 锚表.外键)                    维表解码:目标表以 Id 为主键,外键必须在锚表上
表.字段 (map 源值→对象值 / 源值→对象值)     编码翻译:筛选时自动反向映射,源码值不出网
```

join 与 map 可组合,顺序固定为先 join 后 map。**文法故意窄**:等值 join(仅 Id 主键)、等值 map,不支持表达式计算 —— 避免 YAML 里长出一门临时查询语言。文法扩展须同步:解析器、`build_select`、本节、binding 一致性测试(`tests/test_showroom.py::test_e10_bindings_match_schema`)。

### 3.1.1 派生决策表(derived)

跨列推导(如订单业务状态)不进表达式文法,用 binding 的 `derived` 声明式决策表:

```yaml
derived:
  state:
    rules:  # 有序,首个匹配生效;when 内多条件 AND,null = IS NULL
      - { when: { INVALID_STATE: "Y" }, value: 已作废 }
      - { when: { APPROVE_DATE: null }, value: 草稿 }
      - { when: { CLOSE_STATE: "C" }, value: 已结案 }
    default: null   # 可选;无匹配且无 default → 隔离区(契约不完整不静默)
```

约束(元模型在模板校验时强制):目标必须是已声明属性;枚举属性的全部派生值
必须在 enum_values 内;决策表只支持 等值 / 判空 —— 这是决策表,不是表达式语言。
执行在映射应用阶段(`mapping_apply._apply_derived`),条件列由 binding 一致性
测试对照源表形校验。

### 3.2 draft → verified 流程

1. 新 binding 一律 `draft`,按参考表形 / 公开资料构造;
2. 现场核对:对照客户数据字典逐字段确认表名、字段、状态码、水位字段语义(核对清单见 docs 02 附录);
3. 核对完成置 `verified`;`validate` CLI 列出全部 draft binding 作为待办清单。

## 4. 指标定义(MetricDef)

`metric`(snake_case,全局唯一)、`display_name`、`status: certified/draft/deprecated`、`formula`(人话口径)、`grain`、`dimensions`、`caveats`、`freshness_sla`。

要点:**formula 是口径声明,不是可执行表达式**。可执行实现位于消费方(当前:MCP 网关的指标 SQL 注册表,见 docs 03 §4),实现与定义分离 —— 口径校准(draft→certified)是后续能力,当前保证"未校准的口径必须带警示出现"。

## 5. 校验体系(三层)

| 层 | 执行点 | 内容 |
| --- | --- | --- |
| 单文件 | pydantic 模型 | 类型、必填、keys⊆properties、ref/enum 约束 |
| 跨对象 | `TemplatePack.cross_validate` | relation/ref 目标存在、指标 id 唯一 |
| binding↔表形 | pytest(展厅) | e10 binding 引用的每个 表.字段 必须存在于模拟表形 |

第三层是防漂移的关键:模板与表形任何一侧单独改动,CI 立即失败。现场版本的同类校验(binding↔客户真实字典)是对账工具的一部分,见 docs 02 §7。

## 6. 演进规则

- 模板 5 → 18:由场景拉动(接单评审链下一批大概率是询单、产能、库存),**不为凑数提前填**;
- 破坏性变更(改字段名 / 类型 / 文法)需同版本升级 `pack.yaml: version` 并给出迁移说明;
- 新 ERP 支持 = 新 `source` 的 binding + 表字典进 `docs/dict/`,不改元模型。
