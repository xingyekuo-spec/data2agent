"""Excel/CSV 导入测试:列映射建议、双格式导入、端到端物化与隔离。"""

import csv
from pathlib import Path

import pytest

from data2agent.connect.excel_import import (
    import_tabular,
    load_mapping,
    read_tabular,
    render_mapping_yaml,
    suggest_mapping,
)
from data2agent.connect.landing import LandingStore, raw_table_name
from data2agent.connect.mapping_apply import apply_objects
from data2agent.mcp_server.core import QueryService
from data2agent.mcp_server.evidence import EvidenceContext
from data2agent.metamodel.loader import load_pack

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "excel_quotation"

HEADERS = ["报价单号", "客户", "报价日期", "询单接收时间", "报出时间",
           "规格摘要", "目标价", "报价", "币别", "汇率", "结果"]

EXPECTED_MAPPING = {
    "报价单号": "quote_no", "客户": "customer", "报价日期": "quote_date",
    "询单接收时间": "inquiry_at", "报出时间": "submitted_at",
    "规格摘要": "spec_summary", "目标价": "target_price", "报价": "quoted_price",
    "币别": "currency", "汇率": "fx_assumption", "结果": "result",
}


def _ctx() -> EvidenceContext:
    return EvidenceContext(
        principal="test:excel-import",
        session_id="test_session_excel_import_0001",
        channel="demo",
    )


def _rows() -> list[list]:
    """30 行正常 + 1 行缺业务键 + 1 行非法枚举值(中标)= 32 行。"""
    rows = []
    for i in range(1, 31):
        rows.append([f"EQ-{i:04d}", f"C{(i % 3) + 1:03d}", "2025-05-01",
                     "2025-04-28 09:00:00", "2025-04-29 15:30:00",
                     f"矶钓竿 2.{i % 8}m UL", 26.5, 28.4, "USD", 7.1,
                     ["成交", "未成交", "待定"][i % 3]])
    rows.append(["", "C001", "2025-05-02", None, None, "缺单号的行",
                 10, 12, "USD", 7.1, "成交"])
    rows.append(["EQ-9999", "C002", "2025-05-03", None, None, "工厂私有取值",
                 10, 12, "USD", 7.1, "中标"])
    return rows


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture(scope="module")
def quotation(pack):
    return next(o for o in pack.objects if o.object == "Quotation")


@pytest.fixture()
def csv_file(tmp_path) -> Path:
    f = tmp_path / "quotes.csv"
    with f.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADERS)
        w.writerows(_rows())
    return f


def test_suggest_mapping_hits_all_headers(quotation):
    suggestion = suggest_mapping(HEADERS, quotation)
    assert suggestion["columns"] == EXPECTED_MAPPING
    assert suggestion["unmapped"] == []
    text = render_mapping_yaml(quotation, suggestion)
    assert "object: Quotation" in text and "置信度" in text


def test_import_csv_skips_missing_key(csv_file, quotation, tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    report = import_tabular(landing, quotation, SOURCE, csv_file, EXPECTED_MAPPING)
    assert report.total == 32 and report.imported == 31
    assert len(report.skipped) == 1 and "业务键缺失" in report.skipped[0][1]
    row = landing.con.execute(
        f'SELECT * FROM "{raw_table_name(SOURCE, "QUOTATION")}" WHERE quote_no = ?',
        ("EQ-0001",)).fetchone()
    assert row["currency"] == "USD" and row["_d2a_row_hash"]

    report2 = import_tabular(landing, quotation, SOURCE, csv_file, EXPECTED_MAPPING)
    assert report2.imported == 31
    assert landing.count(SOURCE, "QUOTATION") == 31, "重导入必须幂等(按业务键 upsert)"


def test_import_xlsx(quotation, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "quotes.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for r in _rows():
        ws.append(r)
    wb.save(f)

    headers, rows = read_tabular(f)
    assert headers == HEADERS and len(rows) == 32
    landing = LandingStore(tmp_path / "landing.sqlite")
    report = import_tabular(landing, quotation, SOURCE, f, EXPECTED_MAPPING)
    assert report.imported == 31


def test_end_to_end_apply_and_gateway(csv_file, pack, quotation, tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    import_tabular(landing, quotation, SOURCE, csv_file, EXPECTED_MAPPING)

    report = apply_objects(landing, pack, SOURCE)
    assert [r.object for r in report.results] == ["Quotation"], "其余对象无 excel binding,应跳过"
    result = report.results[0]
    assert result.mapped == 30 and result.quarantined == 1, "非法枚举值(中标)应隔离"
    assert result.build_table
    reason = landing.con.execute(
        "SELECT reason FROM d2a_quarantine WHERE resolved_at IS NULL").fetchone()
    assert "中标" in reason["reason"]

    # M2: gateway 只读 published 快照;将 excel apply 候选提升为唯一 published。
    from data2agent.metamodel.versioning import (
        DatasetVersionRecord,
        ObjectVersionRecord,
        binding_hash,
    )

    tpl = next(o for o in pack.objects if o.object == "Quotation")
    binding = next(b for b in tpl.bindings if b.enabled and b.source == SOURCE)
    landing.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version="ds-excel-q",
            source=SOURCE,
            template_version=pack.version,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            object_manifest='["Quotation"]',
            template_snapshot=pack.model_dump_json(),
        )
    )
    landing.insert_object_version(
        ObjectVersionRecord(
            dataset_version="ds-excel-q",
            object="Quotation",
            object_version="obj-excel-q",
            binding_hash=binding_hash(binding),
            row_count=result.mapped,
            build_table=result.build_table,
            status="published",
            built_at="2026-07-21T10:00:00",
            published_at="2026-07-21T10:05:00",
            batch_id=result.batch_id,
        )
    )

    svc = QueryService(
        landing.db_path, ROOT / "templates", source=SOURCE, default_context=_ctx(),
    )
    res = svc.query_objects("Quotation", filters={"result": "成交"})
    assert res["rows"] and res["meta"]["source"] == SOURCE
    assert res["meta"]["quarantined"] == 1
    assert res["meta"]["dataset_version"] == "ds-excel-q"


def test_import_error_guidance(csv_file, quotation, tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    with pytest.raises(ValueError, match="业务键.*未被任何表头映射"):
        import_tabular(landing, quotation, SOURCE, csv_file, {"客户": "customer"})
    with pytest.raises(ValueError, match="未知属性"):
        import_tabular(landing, quotation, SOURCE, csv_file,
                       {**EXPECTED_MAPPING, "报价单号": "nope"})
    with pytest.raises(ValueError, match="不在文件中"):
        import_tabular(landing, quotation, SOURCE, csv_file,
                       {**EXPECTED_MAPPING, "不存在的表头": "quote_no"})


def test_load_mapping_validates(tmp_path):
    f = tmp_path / "map.yaml"
    f.write_text("object: Quotation\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        load_mapping(f)
