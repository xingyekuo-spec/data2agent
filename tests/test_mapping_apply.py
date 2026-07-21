"""抽取框架 E4 测试:映射物化、解码校验、隔离区、熔断(候选表)。"""

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
    write_candidate_table,
)
from data2agent.connect.sync import whitelist_from_pack
from data2agent.metamodel.dataset_publish_contract import make_build_table
from data2agent.metamodel.loader import load_pack
from data2agent.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord
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


def _cand(object_name: str, token: str = "aabbccddeeff") -> str:
    return make_build_table(SOURCE, object_name, token)


def _table_of(report, object_name: str) -> str:
    by = {r.object: r for r in report.results}
    table = by[object_name].build_table
    assert table
    return table


def test_apply_materializes_all_objects(landing, pack):
    report = apply_objects(landing, pack, SOURCE)
    by = {r.object: r for r in report.results}
    assert by["Customer"].mapped == 24 and by["Quotation"].mapped == 180
    assert by["SalesOrderLine"].mapped == 239, "复合业务键对象也应物化"
    assert all(r.quarantined == 0 for r in report.results)
    assert all(r.build_table and r.build_table.startswith("objv_") for r in report.results)

    cust = _table_of(report, "Customer")
    row = landing.con.execute(f'SELECT * FROM "{cust}" LIMIT 1').fetchone()
    assert row["currency"] in {"USD", "EUR", "JPY", "CNY"}, "币别应经 join 解码"
    quot = _table_of(report, "Quotation")
    results = {
        r[0] for r in landing.con.execute(f'SELECT DISTINCT result FROM "{quot}"')
    }
    assert results <= {"成交", "未成交", "待定"}, "map 解码应在物化阶段完成"
    so = _table_of(report, "SalesOrder")
    (fx,) = landing.con.execute(f'SELECT fx_rate FROM "{so}" LIMIT 1').fetchone()
    assert isinstance(fx, float) and fx > 0


def test_soft_deleted_rows_excluded(landing, pack):
    landing.mark_deleted(SOURCE, "CUSTOMER", "Id", {1})
    report = apply_objects(landing, pack, SOURCE)
    cust = _table_of(report, "Customer")
    (n,) = landing.con.execute(f'SELECT COUNT(*) FROM "{cust}"').fetchone()
    assert n == 23, "软删行不得进入对象层"


def test_null_business_key_quarantined(landing, pack):
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET DOC_NO = NULL WHERE Id = 5')
    landing.con.commit()
    result = apply_object(
        landing, _quotation_tpl(pack), SOURCE, build_table=_cand("Quotation"),
    )
    assert result.mapped == 179 and result.quarantined == 1
    reason = landing.con.execute(
        "SELECT reason FROM d2a_quarantine WHERE resolved_at IS NULL").fetchone()
    assert "业务键缺失" in reason["reason"]
    assert landing.quarantine_count(SOURCE, "Quotation") == 1


def test_unknown_enum_code_quarantined(landing, pack):
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET RESULT_STATE = \'X\' WHERE Id = 7')
    landing.con.commit()
    result = apply_object(
        landing, _quotation_tpl(pack), SOURCE, build_table=_cand("Quotation"),
    )
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
    result = apply_object(
        landing, _quotation_tpl(pack), SOURCE, build_table=_cand("Quotation"),
    )
    assert result.quarantined == 1 and result.mapped == 179


def test_type_coercion_failure_quarantined(landing, pack):
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET QUOTE_PRICE = \'abc\' WHERE Id = 9')
    landing.con.commit()
    result = apply_object(
        landing, _quotation_tpl(pack), SOURCE, build_table=_cand("Quotation"),
    )
    assert result.quarantined == 1


def test_circuit_breaker_preserves_old_candidate(landing, pack):
    report = apply_objects(landing, pack, SOURCE)
    old_quot = _table_of(report, "Quotation")
    (n_before,) = landing.con.execute(f'SELECT COUNT(*) FROM "{old_quot}"').fetchone()
    assert n_before == 180

    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET DOC_NO = NULL WHERE Id <= 15')
    landing.con.commit()  # 15/180 > 5%

    new_table = _cand("Quotation", "112233445566")
    with pytest.raises(MappingCircuitBreaker, match="超过阈值") as exc_info:
        apply_object(
            landing, _quotation_tpl(pack), SOURCE, build_table=new_table,
        )
    assert exc_info.value.total == 180
    assert exc_info.value.mapped == 165
    assert exc_info.value.quarantined == 15
    assert exc_info.value.batch_id
    (n,) = landing.con.execute(f'SELECT COUNT(*) FROM "{old_quot}"').fetchone()
    assert n == 180, "熔断时既有候选表必须原样保留"
    exists = landing.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (new_table,),
    ).fetchone()
    assert exists is None, "熔断不得创建新候选表"

    report2 = apply_objects(landing, pack, SOURCE)
    assert [r.object for r in report2.aborted] == ["Quotation"]
    assert {r.object for r in report2.results} > {"Quotation"}, "单对象熔断不应阻塞其他对象"


def test_derived_state_matches_source(landing, pack):
    report = apply_objects(landing, pack, SOURCE)
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
    so = _table_of(report, "SalesOrder")
    actual = dict(landing.con.execute(
        f'SELECT state, COUNT(*) FROM "{so}" GROUP BY state'))
    assert actual == expected, "派生状态分布必须与源数据逐条一致"
    assert sum(actual.values()) == 97, "全部订单都应有状态(含草稿/已作废)"


def test_derived_no_match_quarantined(landing, pack):
    raw = raw_table_name(SOURCE, "SALES_ORDER")
    (oid,) = landing.con.execute(
        f'SELECT Id FROM "{raw}" WHERE INVALID_STATE = \'N\' '
        "AND APPROVE_DATE IS NOT NULL LIMIT 1").fetchone()
    landing.con.execute(
        f'UPDATE "{raw}" SET CLOSE_STATE = \'X\' WHERE Id = ?', (oid,))
    landing.con.commit()
    tpl = next(o for o in pack.objects if o.object == "SalesOrder")
    result = apply_object(
        landing, tpl, SOURCE, build_table=_cand("SalesOrder"),
    )
    assert result.quarantined == 1
    reason = landing.con.execute(
        "SELECT reason FROM d2a_quarantine WHERE resolved_at IS NULL").fetchone()
    assert "派生规则无匹配" in reason["reason"]


def test_quarantine_superseded_after_fix(landing, pack):
    raw = raw_table_name(SOURCE, "QUOTATION")
    (doc_no,) = landing.con.execute(f'SELECT DOC_NO FROM "{raw}" WHERE Id = 5').fetchone()
    landing.con.execute(f'UPDATE "{raw}" SET DOC_NO = NULL WHERE Id = 5')
    landing.con.commit()
    apply_object(
        landing, _quotation_tpl(pack), SOURCE, build_table=_cand("Quotation", "aaaabbbbcccc"),
    )
    assert landing.quarantine_count(SOURCE, "Quotation") == 1

    landing.con.execute(f'UPDATE "{raw}" SET DOC_NO = ? WHERE Id = 5', (doc_no,))
    landing.con.commit()
    apply_object(
        landing, _quotation_tpl(pack), SOURCE, build_table=_cand("Quotation", "ddddeeeeffff"),
    )
    assert landing.quarantine_count(SOURCE, "Quotation") == 0, "修复后旧隔离记录应标记为已取代"
    (history,) = landing.con.execute("SELECT COUNT(*) FROM d2a_quarantine").fetchone()
    assert history == 1, "隔离历史保留"


def test_write_candidate_rejects_legacy_obj_name(landing, pack):
    tpl = _quotation_tpl(pack)
    with pytest.raises(ValueError, match="非法物理构建表名"):
        write_candidate_table(landing, tpl, [], "batch", "obj_Quotation")
    with pytest.raises(ValueError, match="非法物理构建表名"):
        apply_object(landing, tpl, SOURCE, build_table="obj_Quotation")


def test_candidate_build_does_not_touch_published_baseline(landing, pack):
    """候选构建不得改写已有 published 元数据或遗留 obj_*。"""
    landing.con.execute('CREATE TABLE "obj_Customer" (customer_code TEXT PRIMARY KEY)')
    landing.con.execute('INSERT INTO "obj_Customer" VALUES ("KEEP")')
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-pub",
            source=SOURCE,
            template_version="0.1.0",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Customer"]',
            template_snapshot='{"version":"0.1.0","objects":[],"metrics":[]}',
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-pub",
            object="Customer",
            object_version="obj-keep",
            binding_hash="sha256:" + "ab" * 32,
            row_count=1,
            build_table="objv_deadbeef0001_deadbeef0002_deadbeef0003",
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
        )
    )
    before_ds = landing.get_dataset_version("ds-pub")
    before_obj = landing.list_object_versions("ds-pub")[0]
    before_legacy = landing.con.execute(
        'SELECT customer_code FROM "obj_Customer"'
    ).fetchone()[0]

    apply_objects(landing, pack, SOURCE)

    after_ds = landing.get_dataset_version("ds-pub")
    after_obj = landing.list_object_versions("ds-pub")[0]
    after_legacy = landing.con.execute(
        'SELECT customer_code FROM "obj_Customer"'
    ).fetchone()[0]
    assert after_ds == before_ds
    assert after_obj == before_obj
    assert after_legacy == before_legacy == "KEEP"
