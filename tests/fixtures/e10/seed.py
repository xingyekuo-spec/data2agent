"""E10-like 参考数据生成:python -m tests.fixtures.e10.seed [--db 路径] [--dict-md 路径]

按 E10 参考表形(e10_schema.TABLES)生成一份渔具外销业务参考数据库。
数据确定性生成(--seed 固定随机种子、--asof 固定时间锚点),且自洽:
- 订单总额 = 单身金额合计;
- 报出时间 >= 询单接收时间(报价响应时长指标的数据基础);
- 出货结案状态与已出货数量一致;
- 成交报价单可溯源到销售订单(SALES_ORDER.QUOTATION_ID)。
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from tests.fixtures.e10.schema import TABLES, ddl, dict_markdown

ORG = "ORG01"
USERS = ["E001", "E003", "E007", "E012"]

#: 币别 -> (名称, 对 CNY 基准汇率)
CURRENCIES = {"USD": ("美元", 7.10), "EUR": ("欧元", 7.75), "JPY": ("日元", 0.048), "CNY": ("人民币", 1.0)}

#: 区域 -> (结算币别, 公司后缀, 电话国码)
REGIONS = {
    "美国": ("USD", ["Inc.", "LLC"], 1),
    "欧洲": ("EUR", ["GmbH", "B.V."], 49),
    "日本": ("JPY", ["K.K."], 81),
    "澳洲": ("USD", ["Pty Ltd"], 61),
    "韩国": ("USD", ["Co., Ltd."], 82),
    "东南亚": ("USD", ["Sdn Bhd"], 60),
}
REGION_MIX = ["美国"] * 8 + ["欧洲"] * 6 + ["日本"] * 4 + ["澳洲"] * 2 + ["韩国"] * 2 + ["东南亚"] * 2

ADJ = ["Pacific", "Northern", "Blue Water", "Silver", "River", "Ocean",
       "Golden", "Salt", "Alpine", "Coastal", "Big Lake", "Sunrise"]
NOUN = ["Angler", "Tackle", "Outdoors", "Fishing Gear", "Sports", "Angling", "Rod & Reel", "Fisheries"]
FIRST = ["Mike", "Sarah", "Ken", "Anna", "Tom", "Yuki", "Liam", "Emma"]
LAST = ["Smith", "Tanaka", "Weber", "Kim", "Brown", "Sato", "Lee", "Miller"]

#: 渔具外销的季节性:春秋钓季前询单更密
MONTH_WEIGHT = {1: 1.4, 2: 1.4, 3: 1.2, 4: 1.0, 5: 0.8, 6: 0.8,
                7: 0.8, 8: 1.0, 9: 1.3, 10: 1.3, 11: 1.0, 12: 1.0}


def _d(v: date) -> str:
    return v.isoformat()


def _dt(v: datetime) -> str:
    return v.strftime("%Y-%m-%d %H:%M:%S")


def _worktime(rng: random.Random, day: date) -> datetime:
    return datetime.combine(day, time(rng.randint(8, 18), rng.randint(0, 59), rng.randint(0, 59)))


def _audit(rng: random.Random, created: datetime, modified: datetime | None = None) -> dict:
    modified = max(created, modified or created)
    return {
        "CREATE_DATE": _dt(created), "CREATE_BY": rng.choice(USERS),
        "LAST_MODIFIED_DATE": _dt(modified), "LAST_MODIFIED_BY": rng.choice(USERS),
        "Owner_Org_ROid": ORG,
    }


def _currencies(rng: random.Random) -> list[dict]:
    day = date(2023, 1, 5)
    return [
        {"Id": i, "CURRENCY_CODE": code, "CURRENCY_NAME": name, **_audit(rng, _worktime(rng, day))}
        for i, (code, (name, _)) in enumerate(CURRENCIES.items(), start=1)
    ]


def _customers(rng: random.Random, cur_id: dict[str, int]) -> list[dict]:
    names = rng.sample([f"{a} {n}" for a in ADJ for n in NOUN], len(REGION_MIX))
    regions = REGION_MIX[:]
    rng.shuffle(regions)
    rows = []
    for i, (base, region) in enumerate(zip(names, regions), start=1):
        currency, suffixes, cc = REGIONS[region]
        first, last = rng.choice(FIRST), rng.choice(LAST)
        slug = base.lower().replace(" & ", "").replace(" ", "")
        created = _worktime(rng, date(2023, 1, 1) + timedelta(days=rng.randint(0, 700)))
        rows.append({
            "Id": i,
            "CUSTOMER_CODE": f"C{i:03d}",
            "CUSTOMER_NAME": f"{base} {rng.choice(suffixes)}",
            "CUSTOMER_SHORT_NAME": base.split()[0],
            "COUNTRY_REGION": region,
            "CURRENCY_ID": cur_id[currency],
            "PAYMENT_TERM_DAYS": rng.choice([30, 30, 45, 45, 60, 90]),
            "CONTACT_NAME": f"{first} {last}",
            "CONTACT_PHONE": f"+{cc}-{rng.randint(200, 999)}-{rng.randint(1000, 9999)}",
            "CONTACT_EMAIL": f"{first.lower()}.{last.lower()}@{slug}.example.com",
            **_audit(rng, created, created + timedelta(days=rng.randint(0, 300))),
        })
    return rows


def _items(rng: random.Random) -> list[dict]:
    specs: list[tuple[str, str, str, str, float]] = []  # (大类, 品名, 规格, 单位, 成本)
    for _ in range(16):
        ln = rng.choice([1.8, 2.1, 2.4, 2.7, 3.0, 3.6])
        act = rng.choice(["UL", "L", "ML", "M", "MH", "H"])
        base = rng.choice(["碳素路亚竿", "海钓竿", "矶钓竿", "船钓竿"])
        specs.append(("ROD", f"{base} {ln}m {act}",
                      f"长度 {ln}m / {rng.choice([2, 3, 4, 5])}节 / 调性 {act}",
                      "PCS", round(rng.uniform(45, 220), 2)))
    for _ in range(12):
        size = rng.choice([1000, 2500, 3000, 4000])
        specs.append(("REEL", f"{rng.choice(['纺车轮', '水滴轮'])} {size}型",
                      f"齿比 {rng.choice([4.7, 5.2, 5.8, 6.3, 7.1])}:1 / 轴承 {rng.choice(['4+1', '6+1', '9+1', '11+1'])}BB",
                      "PCS", round(rng.uniform(60, 350), 2)))
    for _ in range(14):
        mm = rng.choice([55, 70, 90, 110])
        specs.append(("LURE", f"{rng.choice(['米诺', '铅笔', 'VIB', '亮片', '软虫'])} {mm}mm",
                      f"{mm}mm / {rng.choice([8, 10, 12, 16])} 色可选 / ABS",
                      "PCS", round(rng.uniform(2, 15), 2)))
    for name in ["不锈钢导环组", "渔轮座", "EVA 握把", "竿尖保护帽", "陶瓷导眼", "碳素竿稍", "尾堵", "金属标牌"]:
        specs.append(("ACC", name, "见图纸", rng.choice(["SET", "PCS"]), round(rng.uniform(1, 20), 2)))
    for name in ["东丽碳布 T300", "东丽碳布 T700", "环氧树脂", "不锈钢丝 0.8mm", "ABS 粒料", "EVA 原料", "玻纤布", "UV 涂料"]:
        specs.append(("RAW", name, "按采购规格书", "KG", round(rng.uniform(20, 120), 2)))

    prefix = {"ROD": "FR", "REEL": "FL", "LURE": "LB", "ACC": "AC", "RAW": "RM"}
    rows, seq = [], {}
    for i, (cat, name, spec, unit, cost) in enumerate(specs, start=1):
        seq[cat] = seq.get(cat, 0) + 1
        created = _worktime(rng, date(2023, 3, 1) + timedelta(days=rng.randint(0, 600)))
        rows.append({
            "Id": i, "ITEM_CODE": f"{prefix[cat]}-{seq[cat]:04d}", "ITEM_NAME": name,
            "ITEM_SPECIFICATION": spec, "CATEGORY_CODE": cat, "UNIT_CODE": unit,
            "STANDARD_COST": cost,
            **_audit(rng, created, created + timedelta(days=rng.randint(0, 200))),
        })
    return rows


def _month_starts(asof: date, months: int = 18) -> list[date]:
    y, m = asof.year, asof.month
    out = []
    for _ in range(months):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(date(y, m, 1))
    return sorted(out)


QTY = {"ROD": (100, 2000, 10), "REEL": (100, 1500, 10), "LURE": (1000, 20000, 500), "ACC": (500, 10000, 100)}


def _qty(rng: random.Random, cat: str) -> float:
    lo, hi, step = QTY[cat]
    return float(rng.randrange(lo, hi + 1, step))


def _quotations(rng: random.Random, asof: date, customers: list[dict], items: list[dict],
                cur_code: dict[int, str]) -> list[dict]:
    sellable = [i for i in items if i["CATEGORY_CODE"] != "RAW"]
    weights = [rng.uniform(0.5, 3.0) for _ in customers]  # 客户规模差异 → 订单集中度
    months = _month_starts(asof)
    m_weights = [MONTH_WEIGHT[m.month] for m in months]

    rows, seq = [], {}
    for i in range(1, 181):
        month = rng.choices(months, m_weights)[0]
        day = min(month + timedelta(days=rng.randint(0, 27)), asof - timedelta(days=3))
        inquiry = _worktime(rng, day)
        cust = rng.choices(customers, weights)[0]
        item = rng.choice(sellable)
        rate = round(CURRENCIES[cur_code[cust["CURRENCY_ID"]]][1] * rng.uniform(0.97, 1.03), 4)
        quote = round(item["STANDARD_COST"] * rng.uniform(1.18, 1.60) / rate, 2)

        if rng.random() < 0.05:
            submit, state = None, "D"
        else:
            submit = inquiry + timedelta(hours=min(rng.lognormvariate(2.5, 0.9), 120))
            if (asof - submit.date()).days > 40:
                state = rng.choices(["W", "L", "P"], [50, 42, 8])[0]
            else:
                state = rng.choices(["W", "L", "P"], [25, 20, 55])[0]

        doc_day = (submit or inquiry).date()
        key = doc_day.strftime("%y%m")
        seq[key] = seq.get(key, 0) + 1
        rows.append({
            "Id": i, "DOC_NO": f"QT{key}-{seq[key]:03d}", "DOC_DATE": _d(doc_day),
            "CUSTOMER_ID": cust["Id"], "ITEM_ID": item["Id"],
            "SPEC_SUMMARY": f"{item['ITEM_NAME']}:{item['ITEM_SPECIFICATION']}",
            "QUANTITY": _qty(rng, item["CATEGORY_CODE"]),
            "TARGET_PRICE": round(quote * rng.uniform(0.85, 1.02), 2),
            "QUOTE_PRICE": quote,
            "CURRENCY_ID": cust["CURRENCY_ID"], "EXCHANGE_RATE": rate,
            "INQUIRY_DATE": _dt(inquiry),
            "SUBMIT_DATE": _dt(submit) if submit else None,
            "RESULT_STATE": state,
            **_audit(rng, inquiry, submit),
        })
    return rows


def _orders(rng: random.Random, asof: date, quotations: list[dict], customers: list[dict],
            items: list[dict], cur_code: dict[int, str]) -> tuple[list[dict], list[dict]]:
    by_id = {c["Id"]: c for c in customers}
    sellable = [i for i in items if i["CATEGORY_CODE"] != "RAW"]
    item_by_id = {i["Id"]: i for i in items}

    sources: list[tuple[dict | None, dict]] = []  # (来源报价单或 None, 客户)
    for q in quotations:
        if q["RESULT_STATE"] == "W" and rng.random() < 0.9:
            sources.append((q, by_id[q["CUSTOMER_ID"]]))
    for _ in range(25):
        sources.append((None, rng.choice(customers)))

    headers, lines, seq = [], [], {}
    oid, lid = 0, 0
    for quote, cust in sources:
        if quote:
            doc_day = datetime.strptime(quote["SUBMIT_DATE"], "%Y-%m-%d %H:%M:%S").date() \
                + timedelta(days=rng.randint(3, 21))
            if doc_day > asof - timedelta(days=1):
                continue
            rate = quote["EXCHANGE_RATE"]
        else:
            doc_day = asof - timedelta(days=rng.randint(5, 540))
            rate = round(CURRENCIES[cur_code[cust["CURRENCY_ID"]]][1] * rng.uniform(0.97, 1.03), 4)

        oid += 1
        key = doc_day.strftime("%y%m")
        seq[key] = seq.get(key, 0) + 1
        promised = doc_day + timedelta(days=rng.randint(45, 120))

        invalid = rng.random() < 0.03
        approved = invalid or rng.random() < 0.96 or (asof - doc_day).days > 30
        if invalid or not approved:
            close = "N"
        elif promised < asof - timedelta(days=60):
            close = "C"
        elif promised < asof:
            close = "F"
        elif promised < asof + timedelta(days=30):
            close = "P"
        else:
            close = "N"

        # 单身:来源报价的品号打头,再补 0–3 行其他品号
        picked = [item_by_id[quote["ITEM_ID"]]] if quote else []
        extra = [i for i in sellable if i not in picked]
        picked += rng.sample(extra, rng.randint(0 if quote else 1, 3))
        total = 0.0
        for ln_no, item in enumerate(picked, start=1):
            lid += 1
            if quote and ln_no == 1:
                qty = max(QTY[item["CATEGORY_CODE"]][2],
                          round(quote["QUANTITY"] * rng.uniform(0.8, 1.2)))
                price = round(quote["QUOTE_PRICE"] * rng.uniform(0.97, 1.03), 2)
            else:
                qty = _qty(rng, item["CATEGORY_CODE"])
                price = round(item["STANDARD_COST"] * rng.uniform(1.15, 1.55) / rate, 2)
            shipped = {"C": qty, "F": qty}.get(close) or \
                (round(qty * rng.uniform(0.2, 0.8)) if close == "P" else 0)
            amount = round(qty * price, 2)
            total += amount
            created = _worktime(rng, doc_day)
            lines.append({
                "Id": lid, "SALES_ORDER_ID": oid, "SEQUENCE_NUMBER": ln_no,
                "ITEM_ID": item["Id"], "QUANTITY": float(qty), "UNIT_PRICE": price,
                "AMOUNT": amount,
                "PLAN_DELIVERY_DATE": _d(promised - timedelta(days=rng.randint(0, 10))),
                "SHIPPED_QUANTITY": float(shipped),
                **_audit(rng, created),
            })

        created = _worktime(rng, doc_day)
        modified = _worktime(rng, min(promised if close in ("C", "F") else doc_day
                                      + timedelta(days=rng.randint(0, 30)), asof))
        headers.append({
            "Id": oid, "DOC_NO": f"SO{key}-{seq[key]:03d}", "DOC_DATE": _d(doc_day),
            "CUSTOMER_ID": cust["Id"], "QUOTATION_ID": quote["Id"] if quote else None,
            "CURRENCY_ID": cust["CURRENCY_ID"], "EXCHANGE_RATE": rate,
            "TRADE_TERM": rng.choices(["FOB", "CIF", "EXW"], [60, 25, 15])[0],
            "TOTAL_AMOUNT": round(total, 2), "PROMISED_SHIP_DATE": _d(promised),
            "APPROVE_DATE": _d(doc_day + timedelta(days=rng.randint(0, 2))) if approved else None,
            "CLOSE_STATE": close, "INVALID_STATE": "Y" if invalid else "N",
            **_audit(rng, created, modified),
        })
    return headers, lines


def _stock_history(rng: random.Random, asof: date, items: list[dict]) -> dict[str, list[dict]]:
    """为呆滞库存 M1 构造库存、入库和出库/领料事实。"""
    balances: list[dict] = []
    warehouses: list[dict] = []
    costs: list[dict] = []
    receipts: list[dict] = []
    sales_headers: list[dict] = []
    sales_lines: list[dict] = []
    production_issues: list[dict] = []
    snapshot_at = datetime.combine(asof, time(23, 0, 0))
    sales_id = sales_line_id = production_id = 0

    for index, item in enumerate(items, start=1):
        receipt_day = asof - timedelta(days=180 + (index % 140))
        receipt_at = _worktime(rng, receipt_day)
        qty = float((index % 9 + 1) * (500 if item["CATEGORY_CODE"] in {"RAW", "LURE"} else 50))
        status = "frozen" if index % 17 == 0 else "usable"
        unit_cost = round(float(item["STANDARD_COST"]) * (0.96 + (index % 5) * 0.02), 2)

        balances.append({
            "Id": index, "ITEM_ID": item["Id"], "PLANT_ID": "P01",
            "WAREHOUSE_CODE": "W01", "INVENTORY_QTY": qty,
            "INVENTORY_STATUS": status,
            **_audit(rng, receipt_at, snapshot_at),
        })
        costs.append({
            "Id": index, "ITEM_ID": item["Id"], "UNIT_COST": unit_cost,
            **_audit(rng, receipt_at, snapshot_at),
        })
        receipts.append({
            "Id": index, "ITEM_ID": item["Id"], "RECEIPT_DATE": _d(receipt_day),
            "RECEIPT_QTY": qty * 2,
            **_audit(rng, receipt_at),
        })

        issue_day = None if index % 11 == 0 else asof - timedelta(
            days=135 if index % 5 == 0 else 20 + (index % 30),
        )
        if issue_day is not None:
            issue_at = _worktime(rng, issue_day)
            if item["CATEGORY_CODE"] in {"RAW", "ACC"}:
                production_id += 1
                production_issues.append({
                    "Id": production_id, "MO_ID": None, "ITEM_ID": item["Id"], "ISSUE_DATE": _d(issue_day),
                    "ISSUED_QTY": qty / 2, "RETURNED_QTY": 0,
                    **_audit(rng, issue_at),
                })
            else:
                sales_id += 1
                sales_line_id += 1
                sales_headers.append({
                    "Id": sales_id, "DOC_NO": f"SI{issue_day:%y%m}-{sales_id:03d}",
                    "DOC_DATE": _d(issue_day), **_audit(rng, issue_at),
                })
                sales_lines.append({
                    "Id": sales_line_id, "SALES_ISSUE_ID": sales_id, "ITEM_ID": item["Id"],
                    "ISSUED_QTY": qty / 2, **_audit(rng, issue_at),
                })
        warehouses.append({
            "Id": index, "ITEM_ID": item["Id"], "WAREHOUSE_ID": "W01",
            "INVENTORY_QTY": qty,
            "LAST_ISSUE_DATE": _d(issue_day) if issue_day is not None else None,
            "LAST_RECEIPT_DATE": _d(receipt_day), "SAFE_STOCK": qty * 0.1,
            **_audit(rng, receipt_at, snapshot_at),
        })

    return {
        "INV_COST_BAL": balances,
        "ITEM_WAREHOUSE": warehouses,
        "INV_UNIT_COST": costs,
        "INV_RECEIPT": receipts,
        "SALES_ISSUE": sales_headers,
        "SALES_ISSUE_D": sales_lines,
        "MO_ISSUED_SETS": production_issues,
    }


def _m2_history(rng: random.Random, asof: date, items: list[dict], stock: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """构造可命中 R2、R2M、R5 的采购与生产证据链。"""
    dead_items = [item for index, item in enumerate(items, start=1)
                  if index % 5 == 0 and index % 17 != 0]
    purchases = dead_items[:3]
    po_rows: list[dict] = []
    po_lines: list[dict] = []
    po_sd_rows: list[dict] = []
    po_sd1_rows: list[dict] = []
    po_ssd_rows: list[dict] = []
    arrival_rows: list[dict] = []
    supplier_rows: list[dict] = []
    base_day = asof - timedelta(days=160)

    # (需求, MOQ, 净收货): 第一条仅 R2,第二条仅 R2M,第三条同时命中。
    quantities = [(200.0, 500.0, 500.0), (100.0, 100.0, 400.0), (300.0, 500.0, 800.0)]
    for ident, (item, (demand, moq, received)) in enumerate(zip(purchases, quantities), start=1):
        created = _worktime(rng, base_day + timedelta(days=ident))
        po_rows.append({
            "Id": ident, "DOC_NO": f"PO{base_day:%y%m}-{ident:03d}", "DOC_DATE": _d(base_day),
            "SUPPLIER_ID": f"SUP-{ident:03d}", "Owner_Dept": "采购部",
            "Owner_Emp": f"BUYER-{ident:03d}", "APPROVE_STATUS": "approved",
            **_audit(rng, created),
        })
        po_lines.append({
            "Id": ident, "PURCHASE_ORDER_ID": ident, "SEQUENCE_NUMBER": 1,
            "ITEM_ID": item["Id"], "PURCHASE_QTY": received, "PRICE": item["STANDARD_COST"],
            "BUSINESS_QTY": received, **_audit(rng, created),
        })
        po_sd_rows.append({
            "Id": ident, "PURCHASE_ORDER_D_ID": ident, "PLANT_ID": "P01",
            "WAREHOUSE_ID": "W01", "PLAN_ARRIVAL_DATE": _d(base_day + timedelta(days=12)),
            **_audit(rng, created),
        })
        po_sd1_rows.append({
            "Id": ident, "PURCHASE_ORDER_SD_ID": ident, "RECEIPT_CLOSE": "Y",
            "RECEIPTED_QTY": received, **_audit(rng, created),
        })
        po_ssd_rows.append({
            "Id": ident, "PURCHASE_ORDER_SD1_ID": ident, "DEMAND_NO": f"DEM-{ident:03d}",
            "DEMAND_QTY": demand, "PURCHASED_QTY": received, "ARRIVED_QTY": received,
            "RECEIPTED_QTY": received, "LOCKED_FLAG": "Y", **_audit(rng, created),
        })
        arrival_rows.append({
            "Id": ident, "PURCHASE_ORDER_D_ID": ident, "ITEM_ID": item["Id"],
            "RECEIPTED_BUSINESS_QTY": received + 20, "RETURNED_BUSINESS_QTY": 20,
            "MO_ID": None, **_audit(rng, created),
        })
        supplier_rows.append({
            "Id": ident, "SUPPLIER_ID": f"SUP-{ident:03d}", "ITEM_ID": item["Id"],
            "MOQ": moq, "LEAD_TIME": 30, "MIN_ORDER_QTY": moq, **_audit(rng, created),
        })

    # 一张已结案工单命中 R5；另一张未结案工单保留 provisional 作为降级样本。
    production_items = dead_items[:2]
    mo_rows: list[dict] = []
    mo_lines: list[dict] = []
    bom_rows: list[dict] = []
    existing_issue_id = max((r["Id"] for r in stock["MO_ISSUED_SETS"]), default=0)
    production_issues: list[dict] = []
    finished_goods = [i for i in items if i["Id"] not in {x["Id"] for x in production_items}]
    for ident, item in enumerate(production_items, start=1):
        mo_id = ident
        parent = finished_goods[ident - 1]
        closed = ident == 1
        created = _worktime(rng, base_day + timedelta(days=20 + ident))
        output_qty = 100.0
        mo_rows.append({
            "Id": mo_id, "DOC_NO": f"MO{base_day:%y%m}-{ident:03d}", "DOC_DATE": _d(base_day),
            "ITEM_ID": parent["Id"], "PLANT_ID": "P01", "Owner_Dept": "生产部",
            "Owner_Emp": f"PROD-{ident:03d}", "PLAN_QTY": output_qty,
            "COMPLETED_QTY": output_qty if closed else 60.0,
            "STATUS": "closed" if closed else "open", **_audit(rng, created),
        })
        mo_lines.append({
            "Id": ident, "MO_ID": mo_id, "ITEM_ID": item["Id"], "QTY_PER": 2.0,
            "REPLACE_ITEM": "N", **_audit(rng, created),
        })
        bom_rows.append({
            "Id": ident, "PARENT_ITEM_ID": parent["Id"], "SUB_ITEM_FEATURE_ID": item["Id"],
            "QTY_PER": 2.0, "DENOMINATOR": 1.0, "FIXED_LOSS_RATE": 0.02,
            "DYNAMIC_LOSS_RATE": 0.01, "ISSUE_OVERRUN_RATE": 0.02,
            "REMARK": "仅适用高端定制机型" if ident == 1 else None,
            **_audit(rng, created),
        })
        existing_issue_id += 1
        production_issues.append({
            "Id": existing_issue_id, "MO_ID": mo_id, "ITEM_ID": item["Id"],
            "ISSUE_DATE": _d(asof - timedelta(days=135)),
            "ISSUED_QTY": 230.0 if closed else 220.0, "RETURNED_QTY": 10.0,
            **_audit(rng, created),
        })

    return {
        "PURCHASE_ORDER": po_rows, "PURCHASE_ORDER_D": po_lines,
        "PURCHASE_ORDER_SD": po_sd_rows, "PURCHASE_ORDER_SD1": po_sd1_rows,
        "PURCHASE_ORDER_SSD": po_ssd_rows, "PURCHASE_ARRIVAL_D": arrival_rows,
        "SUPPLIER_PURCHASE": supplier_rows, "MO": mo_rows, "MO_D": mo_lines,
        "BOM_D": bom_rows, "MO_ISSUED_SETS": stock["MO_ISSUED_SETS"] + production_issues,
    }


def _m3_history(rng: random.Random, asof: date, items: list[dict]) -> dict[str, list[dict]]:
    """构造可命中 R1 与 R3、并覆盖 run-out 排除规则的证据链。"""
    dead_items = [item for index, item in enumerate(items, start=1)
                  if index % 5 == 0 and index % 17 != 0]
    cancelled, ecn_old, run_out_old = dead_items[:3]
    new_items = [item for item in items if item["Id"] not in {cancelled["Id"], ecn_old["Id"], run_out_old["Id"]}]
    base_day = asof - timedelta(days=120)
    created = _worktime(rng, base_day)
    sales_headers = [{
        "Id": 1, "DOC_NO": "SO-CANCEL-001", "DOC_DATE": _d(base_day),
        "CUSTOMER_ID": 1, "Owner_Dept": "销售部", "Owner_Emp": "SALES-001",
        "ApproveStatus": "已取消", **_audit(rng, created, _worktime(rng, base_day + timedelta(days=20))),
    }]
    sales_lines = [{
        "Id": 1, "SALES_ORDER_DOC_ID": 1, "SEQUENCE_NUMBER": 1,
        "ITEM_ID": cancelled["Id"], "QTY_PER": 200.0, "BUSINESS_QTY": 200.0,
        "PRICE": cancelled["STANDARD_COST"] * 1.3, **_audit(rng, created),
    }]
    sales_plans = [{
        "Id": 1, "SALES_ORDER_DOC_D_ID": 1, "PLAN_QTY": 200.0,
        "PLAN_SHIP_DATE": _d(base_day + timedelta(days=15)), "SHIPPED_QTY": 0.0,
        **_audit(rng, created),
    }]
    req_source = [{
        "Id": 1, "PURCHASE_ORDER_SD1_ID": 1, "DEMAND_NO": "SO-CANCEL-001",
        "DEMAND_QTY": 200.0, "PURCHASED_QTY": 500.0, "PURCHASE_SEQUENCE": 1,
        **_audit(rng, created),
    }]

    ecn_rows, ecn_lines, ecn_sub_lines, ecn_tasks = [], [], [], []
    for ident, (old, new, handle) in enumerate(
        [(ecn_old, new_items[0], "replace"), (run_out_old, new_items[1], "run-out")], start=1,
    ):
        changed = base_day + timedelta(days=ident * 3)
        event_at = _worktime(rng, changed)
        ecn_rows.append({
            "Id": ident, "DOC_NO": f"ECN-{changed:%Y}-{ident:03d}", "DOC_DATE": _d(changed),
            "Owner_Dept": "设计部", "Owner_Emp": f"DESIGN-{ident:03d}",
            "REASON_DESC": "材料替代", "CONTENT": "版本升级", "REASON_ID": "MAT-REPLACE",
            **_audit(rng, event_at),
        })
        ecn_lines.append({
            "Id": ident, "ECN_ID": ident, "PARENT_ITEM_ID": new["Id"],
            "ORIGINAL_PARENT_ITEM_ID": old["Id"], "CHANGE_TYPE": "replace",
            "VERSION_TIMES": ident, **_audit(rng, event_at),
        })
        ecn_sub_lines.append({
            "Id": ident, "ECN_D_ID": ident, "SUB_ITEM_FEATURE_ID": new["Id"],
            "ORIGINAL_SUB_ITEM_FEATURE_ID": old["Id"], "CHANGE_TYPE": "replace",
            "HANDLE": handle, "QTY_PER": 1.0,
            "EFFECTIVE_DATE": _d(changed + timedelta(days=5)), "EXPIRY_DATE": None,
            "REMARK": None, **_audit(rng, event_at),
        })
        ecn_tasks.append({
            "Id": ident, "ECN_ID": ident, "DEPARTMENT_ID": "设计部", "PERSON_ID": f"TASK-{ident:03d}",
            "DESCRIPTION": "变更执行", "START_DATE": _d(changed),
            "PLAN_DATE": _d(changed + timedelta(days=7)), "ACTUAL_DATE": _d(changed + timedelta(days=6)),
            **_audit(rng, event_at),
        })
    return {
        "SALES_ORDER_DOC": sales_headers, "SALES_ORDER_DOC_D": sales_lines,
        "SALES_ORDER_DOC_SD": sales_plans, "PO_REQ_SOURCE": req_source,
        "ECN": ecn_rows, "ECN_D": ecn_lines, "ECN_SD": ecn_sub_lines, "ECN_TASK": ecn_tasks,
    }


def build(seed: int, asof: date) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    currencies = _currencies(rng)
    cur_id = {r["CURRENCY_CODE"]: r["Id"] for r in currencies}
    cur_code = {v: k for k, v in cur_id.items()}
    customers = _customers(rng, cur_id)
    items = _items(rng)
    dead_items = [item for index, item in enumerate(items, start=1)
                  if index % 5 == 0 and index % 17 != 0]
    if len(dead_items) >= 1 and len(items) >= 6:
        items[5]["ITEM_SPECIFICATION"] = dead_items[0]["ITEM_SPECIFICATION"]
    quotations = _quotations(rng, asof, customers, items, cur_code)
    orders, order_lines = _orders(rng, asof, quotations, customers, items, cur_code)
    stock_history = _stock_history(rng, asof, items)
    m2_history = _m2_history(rng, asof, items, stock_history)
    m3_history = _m3_history(rng, asof, items)
    return {
        "CURRENCY": currencies, "CUSTOMER": customers, "ITEM": items,
        "QUOTATION": quotations, "SALES_ORDER": orders, "SALES_ORDER_D": order_lines,
        **stock_history, **m2_history, **m3_history,
    }


def write_db(db_path: str | Path, data: dict[str, list[dict]]) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    try:
        for stmt in ddl():
            con.execute(stmt)
        for table, rows in data.items():
            cols = [c for c, _, _ in TABLES[table][1]]
            con.executemany(
                f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join(":" + c for c in cols)})',
                [{c: r.get(c) for c in cols} for r in rows],
            )
        con.commit()
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 E10-like 参考数据库")
    ap.add_argument("--db", default="tests/fixtures/e10/e10.sqlite", help="输出 SQLite 路径(已存在则重建)")
    ap.add_argument("--seed", type=int, default=42, help="随机种子(默认 42,确定性输出)")
    ap.add_argument("--asof", default="2026-07-10", help="数据窗口锚点日期(默认固定,保证可复现)")
    ap.add_argument("--dict-md", help="同时生成表字典 markdown 到该路径(如 docs/dict/digiwin_e10.md)")
    args = ap.parse_args()

    data = build(args.seed, date.fromisoformat(args.asof))
    write_db(args.db, data)
    print(f"E10-like 参考库已生成:{args.db}")
    for table, rows in data.items():
        print(f"  - {table:<14} {len(rows):>4} 行  ({TABLES[table][0]})")

    if args.dict_md:
        out = Path(args.dict_md)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dict_markdown(), encoding="utf-8")
        print(f"表字典已生成:{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
