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
