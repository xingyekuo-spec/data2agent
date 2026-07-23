"""鼎捷 E10-like 参考表形。

表名 / 字段名按 E10 惯例构造:UPPER_SNAKE 英文表名、Id 代理主键、
*_ID 外键关联基础档、DOC_NO 业务单号、CREATE_DATE / LAST_MODIFIED_DATE
审计字段(增量水位)。本模块是 E10-like 参考库与 docs/dict 表字典的单一来源,
真实客户环境仍以现场数据字典核对为准。

与真实 E10 的已知简化(表字典中逐条标注):
- 币别保留 CURRENCY 基础档 + *_ID 外键,体现 E10 全 ID 关联的取数特点;
  付款条件 / 贸易条款 / 单位 / 品号大类等其余基础档简化为编码列;
- 报价单简化为单头(主要品号),真实 E10 报价单含单身;
- 订单业务状态由 APPROVE_DATE / CLOSE_STATE / INVALID_STATE 推导,
  E10 无现成中文状态列。
"""

from __future__ import annotations

Column = tuple[str, str, str]  # (字段名, SQLite 类型, 中文说明)

_AUDIT: list[Column] = [
    ("CREATE_DATE", "TEXT", "建立日期"),
    ("CREATE_BY", "TEXT", "建立人员"),
    ("LAST_MODIFIED_DATE", "TEXT", "最后修改日期(增量抽取水位字段)"),
    ("LAST_MODIFIED_BY", "TEXT", "最后修改人员"),
    ("Owner_Org_ROid", "TEXT", "所属组织(E10 多组织字段,参考库固定单组织)"),
]


def _table(cols: list[Column]) -> list[Column]:
    return [("Id", "INTEGER PRIMARY KEY", "代理主键")] + cols + _AUDIT


#: 表名 -> (中文名, 字段列表)
TABLES: dict[str, tuple[str, list[Column]]] = {
    "CURRENCY": (
        "币别基础档",
        _table([
            ("CURRENCY_CODE", "TEXT", "币别代号(USD / EUR / JPY / CNY)"),
            ("CURRENCY_NAME", "TEXT", "币别名称"),
        ]),
    ),
    "CUSTOMER": (
        "客户主档",
        _table([
            ("CUSTOMER_CODE", "TEXT", "客户编号(业务键)"),
            ("CUSTOMER_NAME", "TEXT", "客户全称"),
            ("CUSTOMER_SHORT_NAME", "TEXT", "客户简称"),
            ("COUNTRY_REGION", "TEXT", "国家 / 区域"),
            ("CURRENCY_ID", "INTEGER", "结算币别(外键 → CURRENCY.Id)"),
            ("PAYMENT_TERM_DAYS", "INTEGER", "账期天数(真实 E10 为付款条件档外键,参考库简化为天数列)"),
            ("CONTACT_NAME", "TEXT", "联系人(敏感,出网前置脱敏)"),
            ("CONTACT_PHONE", "TEXT", "联系电话(敏感)"),
            ("CONTACT_EMAIL", "TEXT", "联系邮箱(敏感)"),
        ]),
    ),
    "ITEM": (
        "品号主档",
        _table([
            ("ITEM_CODE", "TEXT", "品号(业务键,编码规则表达变体)"),
            ("ITEM_NAME", "TEXT", "品名"),
            ("ITEM_SPECIFICATION", "TEXT", "规格描述(竿:长度/节数/调性;轮:齿比/轴承;饵:类型/颜色)"),
            ("CATEGORY_CODE", "TEXT", "大类编码(ROD 竿 / REEL 轮 / LURE 饵 / ACC 配件 / RAW 原料;真实 E10 为品号类别档外键,参考库简化)"),
            ("UNIT_CODE", "TEXT", "计量单位(PCS / SET / KG;真实 E10 为单位档外键,参考库简化)"),
            ("STANDARD_COST", "NUMERIC", "标准成本(CNY,敏感)"),
        ]),
    ),
    "INV_COST_BAL": (
        "存货成本余额",
        _table([
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("PLANT_ID", "TEXT", "工厂编码"),
            ("WAREHOUSE_CODE", "TEXT", "仓库编码"),
            ("INVENTORY_QTY", "NUMERIC", "即时库存数量"),
            ("INVENTORY_STATUS", "TEXT", "库存状态:usable / frozen / pending_inspection / scrapped"),
        ]),
    ),
    "INV_UNIT_COST": (
        "存货单位成本",
        _table([
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("UNIT_COST", "NUMERIC", "单位成本(CNY)"),
        ]),
    ),
    "INV_RECEIPT": (
        "存货入库记录",
        _table([
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("RECEIPT_DATE", "TEXT", "入库日期"),
            ("RECEIPT_QTY", "NUMERIC", "入库数量"),
        ]),
    ),
    "SALES_ISSUE": (
        "销售出库单",
        _table([
            ("DOC_NO", "TEXT", "销货出库单号"),
            ("DOC_DATE", "TEXT", "出库日期"),
        ]),
    ),
    "SALES_ISSUE_D": (
        "销售出库单明细",
        _table([
            ("SALES_ISSUE_ID", "INTEGER", "销货出库单(外键 → SALES_ISSUE.Id)"),
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("ISSUED_QTY", "NUMERIC", "出库数量"),
        ]),
    ),
    "MO_ISSUED_SETS": (
        "工单领料状况",
        _table([
            ("MO_ID", "INTEGER", "制造工单(外键 → MO.Id)"),
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("ISSUE_DATE", "TEXT", "领料日期"),
            ("ISSUED_QTY", "NUMERIC", "领料数量"),
            ("RETURNED_QTY", "NUMERIC", "退料数量"),
        ]),
    ),
    "QUOTATION": (
        "报价单(参考库简化为单头 + 主要品号,真实 E10 含单身)",
        _table([
            ("DOC_NO", "TEXT", "报价单号(业务键)"),
            ("DOC_DATE", "TEXT", "单据日期(报价日期)"),
            ("CUSTOMER_ID", "INTEGER", "客户(外键 → CUSTOMER.Id)"),
            ("ITEM_ID", "INTEGER", "主要品号(外键 → ITEM.Id)"),
            ("SPEC_SUMMARY", "TEXT", "规格摘要"),
            ("QUANTITY", "NUMERIC", "询单数量"),
            ("TARGET_PRICE", "NUMERIC", "客户目标单价(交易币别)"),
            ("QUOTE_PRICE", "NUMERIC", "报价单价(交易币别)"),
            ("CURRENCY_ID", "INTEGER", "交易币别(外键 → CURRENCY.Id)"),
            ("EXCHANGE_RATE", "NUMERIC", "汇率假设(交易币别 → CNY)"),
            ("INQUIRY_DATE", "TEXT", "询单接收时间(datetime;报价响应时长起点)"),
            ("SUBMIT_DATE", "TEXT", "报出时间(datetime,草稿为空;报价响应时长终点)"),
            ("RESULT_STATE", "TEXT", "结果状态:D 草稿 / P 已报出待定 / W 成交 / L 未成交"),
        ]),
    ),
    "SALES_ORDER": (
        "销售订单单头",
        _table([
            ("DOC_NO", "TEXT", "订单号(业务键)"),
            ("DOC_DATE", "TEXT", "单据日期(订单日期)"),
            ("CUSTOMER_ID", "INTEGER", "客户(外键 → CUSTOMER.Id)"),
            ("QUOTATION_ID", "INTEGER", "来源报价单(外键 → QUOTATION.Id,可空;接单评审链溯源)"),
            ("CURRENCY_ID", "INTEGER", "交易币别(外键 → CURRENCY.Id)"),
            ("EXCHANGE_RATE", "NUMERIC", "订单汇率(交易币别 → CNY)"),
            ("TRADE_TERM", "TEXT", "贸易条款(FOB / CIF / EXW;真实 E10 为贸易条件档外键,参考库简化)"),
            ("TOTAL_AMOUNT", "NUMERIC", "订单总额(交易币别,= 单身金额合计)"),
            ("PROMISED_SHIP_DATE", "TEXT", "承诺船期 / 交期"),
            ("APPROVE_DATE", "TEXT", "审核日期(可空 = 未审核)"),
            ("CLOSE_STATE", "TEXT", "出货结案状态:N 未出货 / P 部分出货 / F 完全出货 / C 已结案"),
            ("INVALID_STATE", "TEXT", "作废状态:N 正常 / Y 已作废"),
        ]),
    ),
    "SALES_ORDER_D": (
        "销售订单单身",
        _table([
            ("SALES_ORDER_ID", "INTEGER", "单头(外键 → SALES_ORDER.Id)"),
            ("SEQUENCE_NUMBER", "INTEGER", "行号"),
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("QUANTITY", "NUMERIC", "订购数量"),
            ("UNIT_PRICE", "NUMERIC", "单价(交易币别)"),
            ("AMOUNT", "NUMERIC", "金额(= 数量 × 单价)"),
            ("PLAN_DELIVERY_DATE", "TEXT", "预交日"),
            ("SHIPPED_QUANTITY", "NUMERIC", "已出货数量"),
        ]),
    ),
    "PURCHASE_ORDER": (
        "采购订单单头",
        _table([
            ("DOC_NO", "TEXT", "采购单号(业务键)"),
            ("DOC_DATE", "TEXT", "采购单日期"),
            ("SUPPLIER_ID", "TEXT", "供应商标识"),
            ("Owner_Dept", "TEXT", "关联采购部门"),
            ("Owner_Emp", "TEXT", "关联采购经办人"),
            ("APPROVE_STATUS", "TEXT", "审核状态"),
        ]),
    ),
    "PURCHASE_ORDER_D": (
        "采购订单单身",
        _table([
            ("PURCHASE_ORDER_ID", "INTEGER", "采购订单单头(外键 → PURCHASE_ORDER.Id)"),
            ("SEQUENCE_NUMBER", "INTEGER", "采购单行号"),
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("PURCHASE_QTY", "NUMERIC", "实际采购数量"),
            ("PRICE", "NUMERIC", "采购单价"),
            ("BUSINESS_QTY", "NUMERIC", "业务数量"),
        ]),
    ),
    "PURCHASE_ORDER_SD": (
        "采购订单子单身",
        _table([
            ("PURCHASE_ORDER_D_ID", "INTEGER", "采购订单单身(外键 → PURCHASE_ORDER_D.Id)"),
            ("PLANT_ID", "TEXT", "收货工厂"),
            ("WAREHOUSE_ID", "TEXT", "收货仓库"),
            ("PLAN_ARRIVAL_DATE", "TEXT", "预到货日期"),
        ]),
    ),
    "PURCHASE_ORDER_SD1": (
        "采购订单子单身1",
        _table([
            ("PURCHASE_ORDER_SD_ID", "INTEGER", "采购订单子单身(外键 → PURCHASE_ORDER_SD.Id)"),
            ("RECEIPT_CLOSE", "TEXT", "入库结束码"),
            ("RECEIPTED_QTY", "NUMERIC", "已入库数量"),
        ]),
    ),
    "PURCHASE_ORDER_SSD": (
        "采购订单孙单身",
        _table([
            ("PURCHASE_ORDER_SD1_ID", "INTEGER", "采购订单子单身1(外键 → PURCHASE_ORDER_SD1.Id)"),
            ("DEMAND_NO", "TEXT", "参考需求单号"),
            ("DEMAND_QTY", "NUMERIC", "原始需求量"),
            ("PURCHASED_QTY", "NUMERIC", "已采数量"),
            ("ARRIVED_QTY", "NUMERIC", "已到货数量"),
            ("RECEIPTED_QTY", "NUMERIC", "已入库数量"),
            ("LOCKED_FLAG", "TEXT", "供需锁定标记"),
        ]),
    ),
    "PURCHASE_ARRIVAL_D": (
        "采购到货单明细",
        _table([
            ("PURCHASE_ORDER_D_ID", "INTEGER", "采购订单单身(外键 → PURCHASE_ORDER_D.Id)"),
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("RECEIPTED_BUSINESS_QTY", "NUMERIC", "已收货业务数量"),
            ("RETURNED_BUSINESS_QTY", "NUMERIC", "已退货业务数量"),
            ("MO_ID", "INTEGER", "关联制造工单(外键 → MO.Id,可空)"),
        ]),
    ),
    "SUPPLIER_PURCHASE": (
        "供应商采购信息",
        _table([
            ("SUPPLIER_ID", "TEXT", "供应商标识"),
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("MOQ", "NUMERIC", "最小起订量"),
            ("LEAD_TIME", "INTEGER", "采购提前期(天)"),
            ("MIN_ORDER_QTY", "NUMERIC", "最小订购量"),
        ]),
    ),
    "MO": (
        "制造工单",
        _table([
            ("DOC_NO", "TEXT", "制造工单号(业务键)"),
            ("DOC_DATE", "TEXT", "开单日期"),
            ("ITEM_ID", "INTEGER", "产出品号(外键 → ITEM.Id)"),
            ("PLANT_ID", "TEXT", "生产工厂"),
            ("Owner_Dept", "TEXT", "关联生产部门"),
            ("Owner_Emp", "TEXT", "关联生产经办人"),
            ("PLAN_QTY", "NUMERIC", "预计产量"),
            ("COMPLETED_QTY", "NUMERIC", "完成产量"),
            ("STATUS", "TEXT", "工单状态:closed / open"),
        ]),
    ),
    "MO_D": (
        "制造工单单身",
        _table([
            ("MO_ID", "INTEGER", "制造工单(外键 → MO.Id)"),
            ("ITEM_ID", "INTEGER", "领用物料(外键 → ITEM.Id)"),
            ("QTY_PER", "NUMERIC", "标准单位用量"),
            ("REPLACE_ITEM", "TEXT", "替代料标记"),
        ]),
    ),
    "BOM_D": (
        "BOM 产出品信息档",
        _table([
            ("PARENT_ITEM_ID", "INTEGER", "主件品号(外键 → ITEM.Id)"),
            ("SUB_ITEM_FEATURE_ID", "INTEGER", "元件品号(外键 → ITEM.Id)"),
            ("QTY_PER", "NUMERIC", "组成用量"),
            ("DENOMINATOR", "NUMERIC", "底数"),
            ("FIXED_LOSS_RATE", "NUMERIC", "固定损耗率"),
            ("DYNAMIC_LOSS_RATE", "NUMERIC", "变动损耗率"),
            ("ISSUE_OVERRUN_RATE", "NUMERIC", "允许超领率"),
            ("REMARK", "TEXT", "结构化使用限制说明"),
        ]),
    ),
    "SALES_ORDER_DOC": (
        "销售订单",
        _table([
            ("DOC_NO", "TEXT", "销售订单号(业务键)"),
            ("DOC_DATE", "TEXT", "订单日期"),
            ("CUSTOMER_ID", "INTEGER", "客户(外键 → CUSTOMER.Id)"),
            ("Owner_Dept", "TEXT", "关联销售部门"),
            ("Owner_Emp", "TEXT", "关联销售经办人"),
            ("ApproveStatus", "TEXT", "订单状态:有效 / 已取消 / 已减量"),
        ]),
    ),
    "SALES_ORDER_DOC_D": (
        "销售订单明细",
        _table([
            ("SALES_ORDER_DOC_ID", "INTEGER", "销售订单(外键 → SALES_ORDER_DOC.Id)"),
            ("SEQUENCE_NUMBER", "INTEGER", "订单行号"),
            ("ITEM_ID", "INTEGER", "品号(外键 → ITEM.Id)"),
            ("QTY_PER", "NUMERIC", "需求数量"),
            ("BUSINESS_QTY", "NUMERIC", "有效业务数量"),
            ("PRICE", "NUMERIC", "单价"),
        ]),
    ),
    "SALES_ORDER_DOC_SD": (
        "销售订单发货计划",
        _table([
            ("SALES_ORDER_DOC_D_ID", "INTEGER", "销售订单明细(外键 → SALES_ORDER_DOC_D.Id)"),
            ("PLAN_QTY", "NUMERIC", "计划发货数量"),
            ("PLAN_SHIP_DATE", "TEXT", "计划出货日期"),
            ("SHIPPED_QTY", "NUMERIC", "实际出货数量"),
        ]),
    ),
    "PO_REQ_SOURCE": (
        "采购订单需求来源",
        _table([
            ("PURCHASE_ORDER_SD1_ID", "INTEGER", "采购订单子单身1(外键 → PURCHASE_ORDER_SD1.Id)"),
            ("DEMAND_NO", "TEXT", "来源需求单号"),
            ("DEMAND_QTY", "NUMERIC", "来源需求数量"),
            ("PURCHASED_QTY", "NUMERIC", "已采数量"),
            ("PURCHASE_SEQUENCE", "INTEGER", "采购序号"),
        ]),
    ),
    "ECN": (
        "工程变更信息",
        _table([
            ("DOC_NO", "TEXT", "工程变更单号(业务键)"),
            ("DOC_DATE", "TEXT", "变更日期"),
            ("Owner_Dept", "TEXT", "关联设计部门"),
            ("Owner_Emp", "TEXT", "关联设计经办人"),
            ("REASON_DESC", "TEXT", "变更原因说明"),
            ("CONTENT", "TEXT", "变更范围"),
            ("REASON_ID", "TEXT", "变更原因代码"),
        ]),
    ),
    "ECN_D": (
        "工程变更信息单身",
        _table([
            ("ECN_ID", "INTEGER", "工程变更单(外键 → ECN.Id)"),
            ("PARENT_ITEM_ID", "INTEGER", "新主件品号(外键 → ITEM.Id)"),
            ("ORIGINAL_PARENT_ITEM_ID", "INTEGER", "旧主件品号(外键 → ITEM.Id)"),
            ("CHANGE_TYPE", "TEXT", "变更类型"),
            ("VERSION_TIMES", "INTEGER", "版次"),
        ]),
    ),
    "ECN_SD": (
        "工程变更信息子单身",
        _table([
            ("ECN_D_ID", "INTEGER", "工程变更单身(外键 → ECN_D.Id)"),
            ("SUB_ITEM_FEATURE_ID", "INTEGER", "新元件品号(外键 → ITEM.Id)"),
            ("ORIGINAL_SUB_ITEM_FEATURE_ID", "INTEGER", "旧元件品号(外键 → ITEM.Id)"),
            ("CHANGE_TYPE", "TEXT", "变更类型"),
            ("HANDLE", "TEXT", "处置方式，如 replace / run-out"),
            ("QTY_PER", "NUMERIC", "新元件组成用量"),
            ("EFFECTIVE_DATE", "TEXT", "生效日期"),
            ("EXPIRY_DATE", "TEXT", "失效日期"),
            ("REMARK", "TEXT", "备注"),
        ]),
    ),
    "ECN_TASK": (
        "工程变更部门工作",
        _table([
            ("ECN_ID", "INTEGER", "工程变更单(外键 → ECN.Id)"),
            ("DEPARTMENT_ID", "TEXT", "关联部门"),
            ("PERSON_ID", "TEXT", "关联经办人"),
            ("DESCRIPTION", "TEXT", "工作说明"),
            ("START_DATE", "TEXT", "开始日期"),
            ("PLAN_DATE", "TEXT", "预计完成日"),
            ("ACTUAL_DATE", "TEXT", "实际完成日"),
        ]),
    ),
}


def ddl() -> list[str]:
    """按 TABLES 生成建表语句。"""
    stmts = []
    for name, (_, cols) in TABLES.items():
        body = ",\n".join(f'    "{c}" {t}' for c, t, _ in cols)
        stmts.append(f'CREATE TABLE "{name}" (\n{body}\n);')
    return stmts


def dict_markdown() -> str:
    """生成 docs/dict 表字典 markdown。"""
    lines = [
        "# 鼎捷 E10-like 参考表字典",
        "",
        "> ⚠️ 本字典描述的是**E10-like 参考库**的表形,按 E10 命名惯例构造,",
        "> 用于映射引擎、MCP 与回归测试开发。真实客户环境的表名 / 字段名 / 状态码",
        "> **以现场数据字典核对为准**,核对后将对应 binding 置为 `verified`。",
        ">",
        "> 生成方式:`python -m data2agent.showroom.seed --dict-md docs/dict/digiwin_e10.md`",
        "> (来源:`data2agent/showroom/e10_schema.py`,请勿手改本文件)",
        "",
        "通用惯例:`Id` 代理主键;`*_ID` 外键指向对应表的 `Id`;",
        "`LAST_MODIFIED_DATE` 为增量抽取水位字段;`Owner_Org_ROid` 为 E10 多组织字段。",
        "",
    ]
    for name, (title, cols) in TABLES.items():
        lines += [f"## {name} —— {title}", "", "| 字段 | 类型 | 说明 |", "| --- | --- | --- |"]
        lines += [f"| {c} | {t} | {d} |" for c, t, d in cols]
        lines.append("")
    return "\n".join(lines)
