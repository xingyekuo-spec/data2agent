"""M4-T06 浏览内核测试:标识符、JSON-safe、目录、列分类、分页/搜索/脱敏/截断。"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.sync import whitelist_from_pack
from data2agent.console import data_browser as br
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    return landing, pack


# ---- 标识符与 JSON-safe ----


def test_quote_ident():
    assert br.quote_ident("CUSTOMER") == '"CUSTOMER"'
    assert br.quote_ident('a"b') == '"a""b"'


def test_json_safe_scalars_and_blob():
    assert br.json_safe(None) == (None, False)
    assert br.json_safe("短") == ("短", False)
    assert br.json_safe(3.14)[0] == 3.14
    assert br.json_safe(float("nan"))[0] == "nan"
    assert br.json_safe(float("inf"))[0] == "inf"
    blob, truncated = br.json_safe(b"\x00\x01" * 100)
    assert blob["__blob__"] is True and blob["bytes"] == 200 and blob["truncated"] is True
    assert truncated is True
    # 64 KiB 边界:不超不截,超出必截且标记
    small, t1 = br.json_safe("x" * (64 * 1024))
    assert t1 is False and len(small) == 64 * 1024
    big, t2 = br.json_safe("x" * (64 * 1024 + 10))
    assert t2 is True and "已截断" in big


# ---- 目录与列分类 ----


def test_raw_catalog_honest_counts(env):
    landing, pack = env
    items, warnings = br.raw_catalog(landing, pack, [SOURCE])
    assert warnings == []
    assert items, "同步后应有 raw 目录"
    customer = next(i for i in items if i["table"] == "CUSTOMER")
    assert customer["rows"] == 24
    assert customer["searchable"] is True
    assert customer["latest_batch_id"]
    assert customer["extracted_at"].tzinfo is not None
    # 含无法定位的列(如水位列)→ classification_warning
    assert customer["classification_warning"] is True


def test_raw_catalog_does_not_whitelist_db_orphans(env):
    landing, pack = env
    landing.con.execute('CREATE TABLE "raw_orphan__SECRET" ("Id" INTEGER PRIMARY KEY)')
    landing.con.execute(f'CREATE TABLE "raw_{SOURCE}__SECRET" ("Id" INTEGER PRIMARY KEY)')
    landing.con.execute('INSERT INTO "raw_orphan__SECRET" VALUES (1)')
    landing.con.execute(f'INSERT INTO "raw_{SOURCE}__SECRET" VALUES (1)')
    landing.con.commit()
    items, _warnings = br.raw_catalog(landing, pack, br.allowed_sources(pack, [SOURCE]))
    assert ("orphan", "SECRET") not in {(i["source"], i["table"]) for i in items}
    assert (SOURCE, "SECRET") not in {(i["source"], i["table"]) for i in items}


def test_disabled_binding_excluded_from_raw_catalog(env):
    landing, pack = env
    for tpl in pack.objects:
        for binding in tpl.bindings:
            if "CUSTOMER" in binding.tables:
                binding.status = "disabled"
    assert "CUSTOMER" not in br.allowed_raw_tables(pack, SOURCE)
    items, _warnings = br.raw_catalog(landing, pack, br.allowed_sources(pack, [SOURCE]))
    assert (SOURCE, "CUSTOMER") not in {(i["source"], i["table"]) for i in items}


def test_raw_column_meta_roles_and_masking(env):
    landing, pack = env
    cols = {c["name"]: c for c in br.raw_column_meta(landing, pack, SOURCE, "CUSTOMER")}
    assert cols["CUSTOMER_CODE"]["role"] == "business_key"
    assert cols["CUSTOMER_CODE"]["searchable"] is True
    assert cols["_d2a_extracted_at"]["role"] == "metadata"
    # binding 可定位的敏感列 → 服务端脱敏
    assert cols["CONTACT_EMAIL"]["classification"] == "sensitive"
    assert cols["CONTACT_EMAIL"]["masked"] is True
    # binding 可定位的非敏感列 → normal
    assert cols["CUSTOMER_NAME"]["classification"] == "normal"
    # 无法定位的列 → unknown(持续警告)
    assert cols["LAST_MODIFIED_DATE"]["classification"] == "unknown"


def test_sensitive_business_key_stays_sensitive(env):
    landing, pack = env
    customer = next(t for t in pack.objects if t.object == "Customer")
    prop = next(p for p in customer.properties if p.name == "customer_code")
    prop.sensitive = True
    customer.bindings[0].field_map.pop("customer_code", None)
    raw_cols = {c["name"]: c for c in br.raw_column_meta(landing, pack, SOURCE, "CUSTOMER")}
    obj_physical = br.physical_object("Customer")
    landing.con.execute(
        f'CREATE TABLE "{obj_physical}" '
        '("customer_code" TEXT PRIMARY KEY, "name" TEXT, "contact" TEXT)')
    obj_cols = {c["name"]: c for c in br.object_column_meta(landing, customer)}
    assert raw_cols["CUSTOMER_CODE"]["role"] == "business_key"
    assert raw_cols["CUSTOMER_CODE"]["classification"] == "sensitive"
    assert raw_cols["CUSTOMER_CODE"]["masked"] is True
    assert obj_cols["customer_code"]["role"] == "business_key"
    assert obj_cols["customer_code"]["classification"] == "sensitive"
    assert obj_cols["customer_code"]["masked"] is True


def test_require_source_and_table_404(env):
    landing, pack = env
    with pytest.raises(br.BrowseError) as e:
        br.require_source([SOURCE], landing, "bogus")
    assert e.value.status == 404
    with pytest.raises(br.BrowseError) as e:
        br.require_raw_table(landing, SOURCE, "sqlite_master")
    assert e.value.status == 404


# ---- 分页浏览 ----


def _customer_cols(landing, pack):
    return br.raw_column_meta(landing, pack, SOURCE, "CUSTOMER")


def test_browse_stable_sort_pagination_and_masking(env):
    landing, pack = env
    cols = _customer_cols(landing, pack)
    physical = br.physical_raw(SOURCE, "CUSTOMER")
    page1 = br.browse_table(landing, physical, cols, limit=10, offset=0, q="")
    page2 = br.browse_table(landing, physical, cols, limit=10, offset=10, q="")
    assert page1["total"] == 24
    assert page1["sort"].startswith("pk:")
    codes1 = [r["CUSTOMER_CODE"] for r in page1["rows"]]
    assert codes1 == sorted(codes1)  # 主键稳定排序
    codes2 = [r["CUSTOMER_CODE"] for r in page2["rows"]]
    assert not (set(codes1) & set(codes2))  # 页间不重复
    # 敏感列脱敏
    assert all(r["CONTACT_EMAIL"] == "***" for r in page1["rows"])


def test_browse_search_business_key_and_escaping(env):
    landing, pack = env
    cols = _customer_cols(landing, pack)
    physical = br.physical_raw(SOURCE, "CUSTOMER")
    hit = br.browse_table(landing, physical, cols, limit=50, offset=0, q="C001")
    assert hit["total"] >= 1
    assert all("C001" in r["CUSTOMER_CODE"] for r in hit["rows"])
    # 通配符按字面处理(不放大)
    literal = br.browse_table(landing, physical, cols, limit=50, offset=0, q="100%")
    assert literal["total"] == 0
    # 注入尝试只是普通字符串
    inject = br.browse_table(landing, physical, cols, limit=50, offset=0,
                             q="x' OR '1'='1")
    assert inject["total"] == 0
    # 超长 q → 422
    with pytest.raises(br.BrowseError) as e:
        br.browse_table(landing, physical, cols, limit=50, offset=0, q="x" * 201)
    assert e.value.status == 422
    # 越界 limit → 422
    with pytest.raises(br.BrowseError):
        br.browse_table(landing, physical, cols, limit=0, offset=0, q="")


def test_browse_no_searchable_key_rejected(tmp_path):
    db = LandingStore(tmp_path / "l.sqlite")
    db.con.execute('CREATE TABLE "t" ("a" TEXT)')
    db.con.commit()
    with pytest.raises(br.BrowseError) as e:
        br.browse_table(
            db, "t",
            [{"name": "a", "data_type": "TEXT", "role": "data",
              "classification": "normal", "masked": False, "searchable": False}],
            limit=50, offset=0, q="x")
    assert e.value.status == 422


def test_browse_truncation_marks_fields(tmp_path):
    db = LandingStore(tmp_path / "l.sqlite")
    db.con.execute('CREATE TABLE "t" ("k" TEXT PRIMARY KEY, "big" TEXT, "bin" BLOB)')
    db.con.execute('INSERT INTO "t" VALUES (?, ?, ?)',
                   ("k1", "y" * (64 * 1024 + 5), b"\xff" * 100))
    db.con.commit()
    cols = [
        {"name": "k", "data_type": "TEXT", "role": "business_key",
         "classification": "normal", "masked": False, "searchable": True},
        {"name": "big", "data_type": "TEXT", "role": "data",
         "classification": "normal", "masked": False, "searchable": False},
        {"name": "bin", "data_type": "BLOB", "role": "data",
         "classification": "normal", "masked": False, "searchable": False},
    ]
    page = br.browse_table(db, "t", cols, limit=50, offset=0, q="")
    row = page["rows"][0]
    assert "已截断" in row["big"]
    assert row["bin"]["__blob__"] is True
    assert page["truncations"] == [{"row_index": 0, "fields": ["big", "bin"]}]


# ================================================================
# M5-T03: _sanitize_quarantine_reason — 结构化安全摘要
# ================================================================
# 不再对原始错误文本做正则脱敏(Python !r 单引号/双引号/数值 repr
# 无一可全局可靠屏蔽),改为返回结构化安全摘要。

from data2agent.metamodel.schema import ObjectTemplate, Property, SourceBinding, TemplatePack


def _make_pack_with_binding() -> TemplatePack:
    """构造含基本 field_map 的 binding 用于测试。"""
    obj_tpl = ObjectTemplate(
        object="TestObj",
        display_name="测试对象",
        domain="销售",
        keys=["code"],
        properties=[
            Property(name="code", type="string", desc="编号"),
            Property(name="status", type="enum", enum_values=["active", "inactive"],
                     desc="状态"),
        ],
        bindings=[
            SourceBinding(
                source="test_source",
                tables=["T1"],
                status="verified",
                key_map={"code": "T1.CODE"},
                field_map={
                    "code": "T1.CODE",
                    "status": "T1.STATUS (map VIP-SECRET→active / C-SECRET→inactive)",
                },
            ),
        ],
    )
    return TemplatePack(version="1.0", objects=[obj_tpl], metrics=[])


def test_sanitize_reason_with_binding_returns_safe_summary():
    """有已启用 binding 时返回分类安全摘要,不包含原始错误文本。"""
    pack = _make_pack_with_binding()
    reason = "status: 源码值 'VIP-SECRET' 未在 map 中声明"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "枚举未映射" in result
    assert "映射失败" in result
    assert "raw 预览" in result
    assert "VIP-SECRET" not in result
    assert "源码值" not in result


def test_sanitize_reason_with_number_value_returns_safe_summary():
    """数值 repr 无引号(如 987654) → 分类安全摘要不泄露。"""
    pack = _make_pack_with_binding()
    # 模拟 "源码值 987654 未在 map 中声明" 格式(!r of int)
    reason = "status: 源码值 987654 未在 map 中声明"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "987654" not in result
    assert "枚举未映射" in result
    assert "映射失败" in result


def test_sanitize_reason_with_double_quoted_string_returns_safe_summary():
    """含单引号的字符串 repr 用双引号(如 \"O'Reilly-SECRET\") → 分类安全摘要不泄露。"""
    pack = _make_pack_with_binding()
    # Python repr("O'Reilly-SECRET") → "O'Reilly-SECRET" (双引号)
    reason = "name: 源码值 \"O'Reilly-SECRET\" 未在 map 中声明"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "O'Reilly" not in result
    assert "SECRET" not in result
    assert "枚举未映射" in result
    assert "映射失败" in result


def test_sanitize_reason_no_binding_returns_generic():
    """无匹配 binding 时返回固定通用摘要,不泄露原始原因。"""
    pack = _make_pack_with_binding()
    result = br._sanitize_quarantine_reason(
        "SECRET=leaked-value", pack, "unknown_source", "TestObj")
    assert "leaked-value" not in result
    assert "SECRET" not in result
    assert "映射失败" in result
    assert "raw 预览" in result
    assert "模板未识别此来源" in result


def test_sanitize_reason_none_or_empty():
    """空/None/纯空白 reason 返回空字符串。"""
    assert br._sanitize_quarantine_reason(None, None, "s", "o") == ""
    assert br._sanitize_quarantine_reason("", None, "s", "o") == ""
    assert br._sanitize_quarantine_reason("   ", None, "s", "o") == ""


def test_sanitize_reason_length_budget():
    """安全摘要不超过 _REASON_MAX_LEN 预算。"""
    pack = _make_pack_with_binding()
    # 即使传入超长 reason,摘要本身是固定短字符串
    long_reason = "x" * 600
    result = br._sanitize_quarantine_reason(long_reason, pack, "test_source", "TestObj")
    assert len(result) <= 512
    assert "映射失败" in result


# ---- Issue 1 (M5 seventh review): reason 分类摘要 ----

def test_sanitize_reason_category_enum_unmapped():
    """'未在 map 中声明' → 枚举未映射。"""
    pack = _make_pack_with_binding()
    reason = "status: 源码值 'UNKNOWN' 未在 map 中声明"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "映射失败（枚举未映射）" in result
    assert "UNKNOWN" not in result


def test_sanitize_reason_category_type_conversion():
    """'类型.*转换失败' → 类型转换异常。"""
    pack = _make_pack_with_binding()
    reason = "amount: 类型 decimal 转换失败,值 'abc'"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "类型转换异常" in result
    assert "abc" not in result


def test_sanitize_reason_category_enum_mismatch():
    """'不在枚举' → 枚举不匹配。"""
    pack = _make_pack_with_binding()
    reason = "status: 取值 'UNKNOWN' 不在枚举 ['active', 'inactive'] 内"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "枚举不匹配" in result
    assert "UNKNOWN" not in result


def test_sanitize_reason_category_business_key_missing():
    """'业务键缺失' → 业务键缺失。"""
    pack = _make_pack_with_binding()
    reason = "业务键缺失:{'code': None, 'name': 'X'}"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "业务键缺失" in result
    assert "X" not in result


def test_sanitize_reason_category_business_key_duplicate():
    """'业务键重复' → 业务键重复。"""
    pack = _make_pack_with_binding()
    reason = "业务键重复:{'code': 'DUP001'}"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "业务键重复" in result
    assert "DUP001" not in result


def test_sanitize_reason_category_derived_no_match():
    """'无匹配' → 派生规则无匹配。"""
    pack = _make_pack_with_binding()
    reason = "status: 派生规则无匹配(源值 {'season': 'fall', 'type': 'Z'})"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "派生规则无匹配" in result
    assert "fall" not in result
    assert "Z" not in result


def test_sanitize_reason_combined_categories():
    """多个错误类型同时出现 → 以顿号连接。"""
    pack = _make_pack_with_binding()
    # 模拟包含"未在 map 中声明"和"业务键缺失"两个关键词
    reason = "name: 源码值 'X' 未在 map 中声明; 业务键缺失:{'code': None}"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert "枚举未映射" in result
    assert "业务键缺失" in result
    assert "、" in result  # 顿号连接
    assert "X" not in result


def test_sanitize_reason_unknown_keyword_fallback():
    """无已知关键词 → 回退到通用摘要。"""
    pack = _make_pack_with_binding()
    reason = "未知错误: something went wrong internally"
    result = br._sanitize_quarantine_reason(reason, pack, "test_source", "TestObj")
    assert result == "映射失败，详情请查看隔离 raw 预览"


# ---- Issue 2: sanitize_quarantine_raw 按属性名匹配 ----

def _make_pack_with_mixed_sensitivity() -> TemplatePack:
    """构造含敏感/非敏感属性 + 多余原始列的模板。"""
    obj_tpl = ObjectTemplate(
        object="TestObj",
        display_name="测试",
        domain="销售",
        keys=["id"],
        properties=[
            Property(name="id", type="string", desc="ID"),
            Property(name="name", type="string", desc="名称"),
            Property(name="phone", type="string", desc="电话", sensitive=True),
        ],
        bindings=[
            SourceBinding(
                source="test_source",
                tables=["T1"],
                status="verified",
                key_map={"id": "T1.ID"},
                field_map={
                    "id": "T1.ID",
                    "name": "T1.NAME",
                    "phone": "T1.PHONE",
                },
            ),
        ],
    )
    return TemplatePack(version="1.0", objects=[obj_tpl], metrics=[])


def test_sanitize_raw_matches_by_property_names():
    """raw dict 的 key 是属性名(build_select AS 别名),应按属性名匹配。"""
    pack = _make_pack_with_mixed_sensitivity()
    # raw dict keys 来自 build_select 的 AS "prop" 别名,是属性名(小写)
    raw = {
        "id": "001",           # 属性名,映射到非敏感属性 id
        "name": "张三",        # 属性名,映射到非敏感属性 name
        "phone": "13800138000", # 属性名,映射到敏感属性 phone → MASKED
        "extra_field": "secret", # 未知属性 → MASKED
    }
    sanitized, truncs = br.sanitize_quarantine_raw(raw, pack, "test_source", "TestObj")
    assert sanitized is not None
    assert sanitized["id"] == "001"
    assert sanitized["name"] == "张三"
    assert sanitized["phone"] == "***"
    assert sanitized["extra_field"] == "***"


def test_sanitize_raw_unmapped_column_masked():
    """field_map 中未出现的列默认遮罩。"""
    pack = _make_pack_with_mixed_sensitivity()
    raw = {
        "id": "001",
        "name": "张三",
        "phone": "13800138000",
        "secret_col": "13900139000",  # 未映射列 → mask
    }
    sanitized, truncs = br.sanitize_quarantine_raw(raw, pack, "test_source", "TestObj")
    assert sanitized is not None
    assert sanitized["secret_col"] == "***"
    # 映射到非敏感属性的列 → 显示
    assert sanitized["id"] == "001"
    assert sanitized["name"] == "张三"
    # 映射到敏感属性的列 → 遮罩
    assert sanitized["phone"] == "***"


def test_sanitize_raw_non_sensitive_properties_shown():
    """非敏感属性值完整显示,经过 json_safe。"""
    pack = _make_pack_with_mixed_sensitivity()
    raw = {"id": "C001", "name": "Acme Corp", "phone": "555-0001"}
    sanitized, _truncs = br.sanitize_quarantine_raw(raw, pack, "test_source", "TestObj")
    assert sanitized["id"] == "C001"
    assert sanitized["name"] == "Acme Corp"
    assert sanitized["phone"] == "***"


def test_sanitize_raw_sensitive_properties_masked():
    """敏感属性值替换为 MASKED。"""
    pack = _make_pack_with_mixed_sensitivity()
    raw = {"id": "D001", "name": "Beta Ltd", "phone": "confidential-666"}
    sanitized, _truncs = br.sanitize_quarantine_raw(raw, pack, "test_source", "TestObj")
    assert sanitized["phone"] == "***"


def test_sanitize_raw_no_binding_masks_all():
    """无匹配 binding 时所有列全部遮罩。"""
    pack = _make_pack_with_mixed_sensitivity()
    raw = {"id": "001", "name": "张三", "phone": "13800138000", "secret_col": "13900139000"}
    sanitized, truncs = br.sanitize_quarantine_raw(raw, pack, "unknown_source", "TestObj")
    assert sanitized is not None
    for v in sanitized.values():
        assert v == "***"


def test_sanitize_raw_no_pack_masks_all():
    """pack=None 时所有列全部遮罩。"""
    raw = {"id": "001", "name": "张三", "secret_col": "13900139000"}
    sanitized, truncs = br.sanitize_quarantine_raw(raw, None, "test_source", "TestObj")
    assert sanitized is not None
    for v in sanitized.values():
        assert v == "***"
