from pathlib import Path

import pytest

from data2agent.shared.metamodel.loader import TemplateLoadError, load_pack
from data2agent.shared.metamodel.schema import ObjectTemplate

ROOT = Path(__file__).resolve().parents[2]


def test_load_pack_ok():
    pack = load_pack(ROOT / "templates")
    assert pack.objects, "至少应加载一个对象模板"
    assert pack.metrics, "至少应加载一个指标定义"
    assert "SalesOrder" in pack.object_names()
    assert pack.cross_validate() == []


def test_keys_must_be_properties():
    with pytest.raises(Exception, match="不在属性列表中"):
        ObjectTemplate(
            object="Bad",
            display_name="坏模板",
            domain="销售",
            keys=["not_exist"],
            properties=[{"name": "a", "type": "string"}],
        )


def test_ref_must_have_target():
    with pytest.raises(Exception, match="必须声明 ref 目标对象"):
        ObjectTemplate(
            object="Bad2",
            display_name="坏模板2",
            domain="销售",
            keys=["a"],
            properties=[{"name": "a", "type": "ref"}],
        )


def test_bindings_start_as_draft():
    pack = load_pack(ROOT / "templates")
    order = next(o for o in pack.objects if o.object == "SalesOrder")
    assert order.bindings and order.bindings[0].status == "draft"


def test_gbk_encoded_template_reports_filename(tmp_path):
    """GBK/ANSI 保存的模板必须点名文件并给出 UTF-8 修复指引。"""
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / "bad.yaml").write_bytes("object: 销售订单\n".encode("gbk"))
    with pytest.raises(TemplateLoadError, match=r"bad\.yaml.*UTF-8"):
        load_pack(tmp_path)


def test_yaml_syntax_error_reports_filename(tmp_path):
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / "broken.yaml").write_text("object: [未闭合\n", encoding="utf-8")
    with pytest.raises(TemplateLoadError, match=r"broken\.yaml.*语法"):
        load_pack(tmp_path)


def test_appledouble_files_are_ignored(tmp_path):
    """macOS 拷 U 盘产生的 ._ 伴随文件不得被当作模板读取。"""
    objects = tmp_path / "objects"
    objects.mkdir()
    (objects / "._order.yaml").write_bytes(b"\x00\x05\x16\x07\xb0binary")
    (objects / ".hidden.yaml").write_bytes(b"\x00\xb0binary")
    # 目录里没有合法模板;只要 ._ / 隐藏文件被跳过,就不会报解码错误
    pack = load_pack(tmp_path)
    assert pack.objects == []


def test_binding_status_disabled_accepted():
    """status=disabled 必须能通过 schema 校验(否则模板加载抛错 → API 500)。"""
    from data2agent.shared.metamodel.schema import SourceBinding

    assert SourceBinding(source="e10", tables=["T"], status="disabled").enabled is False
    assert SourceBinding(source="e10", tables=["T"]).enabled is True
    with pytest.raises(Exception, match="disabled"):
        SourceBinding(source="e10", tables=["T"], status="bogus")


def test_disabled_binding_excluded_from_extraction():
    """禁用 binding 不进白名单、不推导水位;共享表若另有启用 binding 仍保留。"""
    from tests.helpers import watermarks_from_pack
    from tests.helpers import whitelist_from_pack
    from data2agent.shared.metamodel.schema import TemplatePack

    quotation = ObjectTemplate(
        object="Q", display_name="报价", domain="销售", keys=["a"],
        properties=[{"name": "a", "type": "string"}],
        bindings=[{"source": "e10", "tables": ["QUOTATION", "CURRENCY"],
                   "status": "disabled", "watermark": "QUOTATION.MODIFY_DATE"}],
    )
    currency = ObjectTemplate(
        object="C", display_name="币别", domain="销售", keys=["a"],
        properties=[{"name": "a", "type": "string"}],
        bindings=[{"source": "e10", "tables": ["CURRENCY"], "status": "draft"}],
    )
    pack = TemplatePack(version="t", objects=[quotation, currency])
    assert whitelist_from_pack(pack, "e10") == {"CURRENCY"}
    assert watermarks_from_pack(pack, "e10") == {}


def _tpl_with_derived(derived):
    return ObjectTemplate(
        object="D", display_name="派生", domain="销售", keys=["a"],
        properties=[{"name": "a", "type": "string"},
                    {"name": "st", "type": "enum", "enum_values": ["开", "关"]}],
        bindings=[{"source": "s", "tables": ["T"], "derived": derived}],
    )


def test_derived_must_target_declared_property():
    with pytest.raises(Exception, match="不在属性列表中"):
        _tpl_with_derived({"nope": {"rules": [{"when": {"C": "1"}, "value": "开"}]}})


def test_derived_enum_values_validated_at_template_level():
    with pytest.raises(Exception, match="不在枚举"):
        _tpl_with_derived({"st": {"rules": [{"when": {"C": "1"}, "value": "半开"}]}})
    _tpl_with_derived({"st": {"rules": [{"when": {"C": "1"}, "value": "开"}],
                              "default": "关"}})  # 合法决策表通过
