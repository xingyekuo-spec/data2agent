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
