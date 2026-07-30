"""E10-like 参考库测试:数据自洽 + binding 与表形一致。"""

import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data2agent.shared.metamodel.loader import load_pack
from tests.fixtures.e10.schema import TABLES, dict_markdown
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[2]
ASOF = date(2026, 7, 10)


@pytest.fixture(scope="module")
def data() -> dict[str, list[dict]]:
    return build(seed=42, asof=ASOF)


def test_build_deterministic(data):
    assert build(seed=42, asof=ASOF) == data


def test_all_tables_populated(data):
    assert set(data) == set(TABLES)
    for table, rows in data.items():
        assert rows, f"{table} 不应为空"
        cols = {c for c, _, _ in TABLES[table][1]}
        for row in rows:
            assert set(row) <= cols, f"{table} 存在表形之外的字段"


def test_foreign_keys_resolve(data):
    ids = {t: {r["Id"] for r in rows} for t, rows in data.items()}
    fk = [
        ("CUSTOMER", "CURRENCY_ID", "CURRENCY"),
        ("QUOTATION", "CUSTOMER_ID", "CUSTOMER"),
        ("QUOTATION", "ITEM_ID", "ITEM"),
        ("SALES_ORDER", "CUSTOMER_ID", "CUSTOMER"),
        ("SALES_ORDER", "CURRENCY_ID", "CURRENCY"),
        ("SALES_ORDER", "QUOTATION_ID", "QUOTATION"),
        ("SALES_ORDER_D", "SALES_ORDER_ID", "SALES_ORDER"),
        ("SALES_ORDER_D", "ITEM_ID", "ITEM"),
    ]
    for table, col, target in fk:
        for row in data[table]:
            if row.get(col) is not None:
                assert row[col] in ids[target], f"{table}.{col}={row[col]} 无法解析到 {target}"


def test_order_total_equals_lines(data):
    line_sum: dict[int, float] = {}
    for ln in data["SALES_ORDER_D"]:
        line_sum[ln["SALES_ORDER_ID"]] = round(line_sum.get(ln["SALES_ORDER_ID"], 0) + ln["AMOUNT"], 2)
        assert ln["AMOUNT"] == round(ln["QUANTITY"] * ln["UNIT_PRICE"], 2)
    for o in data["SALES_ORDER"]:
        assert o["TOTAL_AMOUNT"] == line_sum[o["Id"]], f"{o['DOC_NO']} 总额与单身合计不符"


def test_quotation_timeline_and_states(data):
    for q in data["QUOTATION"]:
        if q["RESULT_STATE"] == "D":
            assert q["SUBMIT_DATE"] is None
        else:
            assert q["SUBMIT_DATE"] >= q["INQUIRY_DATE"], "报出时间不得早于询单接收时间"
        assert q["RESULT_STATE"] in {"D", "P", "W", "L"}


def test_order_state_consistent_with_shipment(data):
    lines_of: dict[int, list[dict]] = {}
    for ln in data["SALES_ORDER_D"]:
        lines_of.setdefault(ln["SALES_ORDER_ID"], []).append(ln)
    for o in data["SALES_ORDER"]:
        for ln in lines_of[o["Id"]]:
            if o["CLOSE_STATE"] in ("C", "F"):
                assert ln["SHIPPED_QUANTITY"] == ln["QUANTITY"]
            elif o["CLOSE_STATE"] == "N":
                assert ln["SHIPPED_QUANTITY"] == 0
            else:
                assert 0 < ln["SHIPPED_QUANTITY"] < ln["QUANTITY"]
        if o["QUOTATION_ID"] is not None:
            quote = next(q for q in data["QUOTATION"] if q["Id"] == o["QUOTATION_ID"])
            assert quote["RESULT_STATE"] == "W", "订单只应溯源到成交报价单"
            assert quote["CUSTOMER_ID"] == o["CUSTOMER_ID"]


def test_write_db_roundtrip(data, tmp_path):
    db = tmp_path / "e10.sqlite"
    write_db(db, data)
    con = sqlite3.connect(db)
    try:
        for table, rows in data.items():
            (n,) = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            assert n == len(rows)
        # SQL 侧复核:单头总额 = 单身合计
        bad = con.execute("""
            SELECT h.DOC_NO FROM SALES_ORDER h
            JOIN (SELECT SALES_ORDER_ID, ROUND(SUM(AMOUNT), 2) s
                  FROM SALES_ORDER_D GROUP BY SALES_ORDER_ID) d ON d.SALES_ORDER_ID = h.Id
            WHERE ROUND(h.TOTAL_AMOUNT, 2) != d.s
        """).fetchall()
        assert bad == []
    finally:
        con.close()


def test_e10_bindings_match_schema():
    """元模型消费校验:digiwin_e10 binding 引用的表 / 字段必须存在于模拟表形。"""
    columns = {t: {c for c, _, _ in cols} for t, (_, cols) in TABLES.items()}
    token = re.compile(r"([A-Z][A-Z_0-9]*)\.([A-Za-z_][A-Za-z_0-9]*)")
    pack = load_pack(ROOT / "templates")
    checked = 0
    for obj in pack.objects:
        for b in obj.bindings:
            if b.source != "digiwin_e10":
                continue
            checked += 1
            for t in b.source_tables:
                assert t in TABLES, f"{obj.object}: binding 表 {t} 不在模拟表形中"
            if b.materializer:
                # 内部结果表由 materializer 生成，不属于 ERP 参考表形。
                continue
            refs = list(b.key_map.values()) + list(b.field_map.values()) + ([b.watermark] if b.watermark else [])
            for ref in refs:
                matches = token.findall(ref)
                assert matches, f"{obj.object}: 映射值 '{ref}' 未包含 表.字段 引用"
                for table, col in matches:
                    assert table in columns, f"{obj.object}: '{ref}' 引用了未知表 {table}"
                    assert col in columns[table], f"{obj.object}: '{ref}' 引用了 {table} 不存在的字段 {col}"
            anchor = b.tables[0]
            for prop, spec in b.derived.items():
                for rule in spec.rules:
                    for col in rule.when:
                        assert col in columns[anchor], \
                            f"{obj.object}.{prop}: 派生条件引用了锚表 {anchor} 不存在的列 {col}"
    assert checked == 15, "十五个对象都应有 digiwin_e10 binding"


def test_dict_markdown_covers_all_tables():
    md = dict_markdown()
    for table in TABLES:
        assert f"## {table}" in md
