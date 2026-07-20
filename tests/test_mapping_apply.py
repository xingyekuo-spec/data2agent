"""抽取框架 E4 测试:映射物化、解码校验、隔离区、熔断。"""

from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.mapping_apply import (
    MappingCircuitBreaker,
    apply_object,
    apply_objects,
)
from data2agent.connect.sync import whitelist_from_pack
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def landing(tmp_path, pack) -> LandingStore:
    """seed 源库 → 全量同步后的落地库(未 apply)。"""
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    return landing


def _quotation_tpl(pack):
    return next(o for o in pack.objects if o.object == "Quotation")


def test_apply_materializes_all_objects(landing, pack):
    report = apply_objects(landing, pack, SOURCE)
    by = {r.object: r for r in report.results}
    assert by["Customer"].mapped == 24 and by["Quotation"].mapped == 180
    assert by["SalesOrderLine"].mapped == 239, "复合业务键对象也应物化"
    assert all(r.quarantined == 0 for r in report.results)

    row = landing.con.execute('SELECT * FROM "obj_Customer" LIMIT 1').fetchone()
    assert row["currency"] in {"USD", "EUR", "JPY", "CNY"}, "币别应经 join 解码"
    results = {r[0] for r in landing.con.execute('SELECT DISTINCT result FROM "obj_Quotation"')}
    assert results <= {"成交", "未成交", "待定"}, "map 解码应在物化阶段完成"
    (fx,) = landing.con.execute(
        'SELECT fx_rate FROM "obj_SalesOrder" LIMIT 1').fetchone()
    assert isinstance(fx, float) and fx > 0


def test_soft_deleted_rows_excluded(landing, pack):
    landing.mark_deleted(SOURCE, "CUSTOMER", "Id", {1})
    apply_objects(landing, pack, SOURCE)
    (n,) = landing.con.execute('SELECT COUNT(*) FROM "obj_Customer"').fetchone()
    assert n == 23, "软删行不得进入对象层"


def test_null_business_key_quarantined(landing, pack):
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET DOC_NO = NULL WHERE Id = 5')
    landing.con.commit()
    result = apply_object(landing, _quotation_tpl(pack), SOURCE)
    assert result.mapped == 179 and result.quarantined == 1
    reason = landing.con.execute(
        "SELECT reason FROM d2a_quarantine WHERE resolved_at IS NULL").fetchone()
    assert "业务键缺失" in reason["reason"]
    assert landing.quarantine_count(SOURCE, "Quotation") == 1


def test_unknown_enum_code_quarantined(landing, pack):
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET RESULT_STATE = \'X\' WHERE Id = 7')
    landing.con.commit()
    result = apply_object(landing, _quotation_tpl(pack), SOURCE)
    assert result.quarantined == 1
    reason = landing.con.execute(
        "SELECT reason FROM d2a_quarantine WHERE resolved_at IS NULL").fetchone()
    assert "未在 map 中声明" in reason["reason"]


def test_duplicate_business_key_quarantined(landing, pack):
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" '
        "SET DOC_NO = (SELECT DOC_NO FROM "
        f'"{raw_table_name(SOURCE, "QUOTATION")}" WHERE Id = 1) WHERE Id = 2')
    landing.con.commit()
    result = apply_object(landing, _quotation_tpl(pack), SOURCE)
    assert result.quarantined == 1 and result.mapped == 179


def test_type_coercion_failure_quarantined(landing, pack):
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET QUOTE_PRICE = \'abc\' WHERE Id = 9')
    landing.con.commit()
    result = apply_object(landing, _quotation_tpl(pack), SOURCE)
    assert result.quarantined == 1


def test_circuit_breaker_preserves_old_table(landing, pack):
    apply_objects(landing, pack, SOURCE)  # 先建立健康的对象层
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET DOC_NO = NULL WHERE Id <= 15')
    landing.con.commit()  # 15/180 > 5%

    with pytest.raises(MappingCircuitBreaker, match="超过阈值") as exc_info:
        apply_object(landing, _quotation_tpl(pack), SOURCE)
    assert exc_info.value.total == 180
    assert exc_info.value.mapped == 165
    assert exc_info.value.quarantined == 15
    assert exc_info.value.batch_id
    (n,) = landing.con.execute('SELECT COUNT(*) FROM "obj_Quotation"').fetchone()
    assert n == 180, "熔断时旧对象表必须原样保留"

    report = apply_objects(landing, pack, SOURCE)
    assert [r.object for r in report.aborted] == ["Quotation"]
    assert {r.object for r in report.results} > {"Quotation"}, "单对象熔断不应阻塞其他对象"


def test_derived_state_matches_source(landing, pack):
    apply_objects(landing, pack, SOURCE)
    raw = raw_table_name(SOURCE, "SALES_ORDER")
    expected = {}
    for state, cond in {
        "已作废": "INVALID_STATE = 'Y'",
        "草稿": "INVALID_STATE = 'N' AND APPROVE_DATE IS NULL",
        "已结案": "INVALID_STATE = 'N' AND APPROVE_DATE IS NOT NULL AND CLOSE_STATE = 'C'",
        "完全出货": "INVALID_STATE = 'N' AND APPROVE_DATE IS NOT NULL AND CLOSE_STATE = 'F'",
        "部分出货": "INVALID_STATE = 'N' AND APPROVE_DATE IS NOT NULL AND CLOSE_STATE = 'P'",
        "已接单": "INVALID_STATE = 'N' AND APPROVE_DATE IS NOT NULL AND CLOSE_STATE = 'N'",
    }.items():
        (n,) = landing.con.execute(f'SELECT COUNT(*) FROM "{raw}" WHERE {cond}').fetchone()
        if n:
            expected[state] = n
    actual = dict(landing.con.execute(
        'SELECT state, COUNT(*) FROM "obj_SalesOrder" GROUP BY state'))
    assert actual == expected, "派生状态分布必须与源数据逐条一致"
    assert sum(actual.values()) == 97, "全部订单都应有状态(含草稿/已作废)"


def test_derived_no_match_quarantined(landing, pack):
    raw = raw_table_name(SOURCE, "SALES_ORDER")
    # 挑有效且已审核的单(否则先命中 已作废/草稿 规则,走不到 CLOSE_STATE)
    (oid,) = landing.con.execute(
        f'SELECT Id FROM "{raw}" WHERE INVALID_STATE = \'N\' '
        "AND APPROVE_DATE IS NOT NULL LIMIT 1").fetchone()
    landing.con.execute(
        f'UPDATE "{raw}" SET CLOSE_STATE = \'X\' WHERE Id = ?', (oid,))  # 决策表未覆盖的取值
    landing.con.commit()
    tpl = next(o for o in pack.objects if o.object == "SalesOrder")
    result = apply_object(landing, tpl, SOURCE)
    assert result.quarantined == 1
    reason = landing.con.execute(
        "SELECT reason FROM d2a_quarantine WHERE resolved_at IS NULL").fetchone()
    assert "派生规则无匹配" in reason["reason"]


def test_quarantine_superseded_after_fix(landing, pack):
    raw = raw_table_name(SOURCE, "QUOTATION")
    (doc_no,) = landing.con.execute(f'SELECT DOC_NO FROM "{raw}" WHERE Id = 5').fetchone()
    landing.con.execute(f'UPDATE "{raw}" SET DOC_NO = NULL WHERE Id = 5')
    landing.con.commit()
    apply_object(landing, _quotation_tpl(pack), SOURCE)
    assert landing.quarantine_count(SOURCE, "Quotation") == 1

    landing.con.execute(f'UPDATE "{raw}" SET DOC_NO = ? WHERE Id = 5', (doc_no,))
    landing.con.commit()
    apply_object(landing, _quotation_tpl(pack), SOURCE)
    assert landing.quarantine_count(SOURCE, "Quotation") == 0, "修复后旧隔离记录应标记为已取代"
    (history,) = landing.con.execute("SELECT COUNT(*) FROM d2a_quarantine").fetchone()
    assert history == 1, "隔离历史保留"
