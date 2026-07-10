from pathlib import Path

import pytest

from data2agent.metamodel.loader import load_pack
from data2agent.metamodel.schema import ObjectTemplate

ROOT = Path(__file__).resolve().parents[1]


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
