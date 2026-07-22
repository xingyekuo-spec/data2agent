"""映射表达式文法与 SQL 构建测试。"""

import pytest

from data2agent.mapping import FieldExpr, build_select, parse_field_expr
from data2agent.metamodel.schema import ObjectTemplate, SourceBinding


def test_parse_plain():
    assert parse_field_expr("CUSTOMER.CUSTOMER_CODE") == FieldExpr("CUSTOMER", "CUSTOMER_CODE")


def test_parse_join():
    e = parse_field_expr("CURRENCY.CURRENCY_CODE (join CUSTOMER.CURRENCY_ID)")
    assert e.table == "CURRENCY" and e.join_fk == ("CUSTOMER", "CURRENCY_ID")


def test_parse_map():
    e = parse_field_expr("QUOTATION.RESULT_STATE (map W→成交 / L→未成交 / P→待定 / D→待定)")
    assert e.value_map == {"W": "成交", "L": "未成交", "P": "待定", "D": "待定"}


@pytest.mark.parametrize("bad", ["无效表达式", "TABLE", "T.C (join 缺点号)", "T.C (map 缺箭头)"])
def test_parse_rejects_bad_grammar(bad):
    with pytest.raises(ValueError, match="文法|条目"):
        parse_field_expr(bad)


def _template_and_binding(**binding_overrides):
    tpl = ObjectTemplate(
        object="Demo", display_name="演示", domain="销售", keys=["code"],
        properties=[{"name": "code", "type": "string"}, {"name": "cur", "type": "string"}],
    )
    kwargs = {
        "source": "digiwin_e10", "tables": ["MAIN", "CURRENCY"],
        "field_map": {"code": "MAIN.CODE", "cur": "CURRENCY.CODE (join MAIN.CURRENCY_ID)"},
        **binding_overrides,
    }
    binding = SourceBinding(**kwargs)
    return tpl, binding


def test_build_select_joins_and_params():
    tpl, binding = _template_and_binding()
    sql, params, _ = build_select(tpl, binding, filters={"code": "C001"},
                                  order_by="cur", desc=True, limit=5)
    assert 'LEFT JOIN "CURRENCY" j1 ON j1."Id" = a."CURRENCY_ID"' in sql
    assert 'a."CODE" = ?' in sql and params == ["C001"]
    assert 'ORDER BY j1."CODE" DESC' in sql and "LIMIT 5" in sql


def test_build_select_rejects_unknown_filter_and_order():
    tpl, binding = _template_and_binding()
    with pytest.raises(ValueError, match="未知筛选字段"):
        build_select(tpl, binding, filters={"nope": 1})
    with pytest.raises(ValueError, match="未知排序字段"):
        build_select(tpl, binding, order_by="nope")


def test_build_select_rejects_unjoined_foreign_table():
    tpl, binding = _template_and_binding(
        field_map={"code": "OTHER.CODE"})
    with pytest.raises(ValueError, match="必须声明"):
        build_select(tpl, binding)


def test_limit_clamped():
    tpl, binding = _template_and_binding()
    sql, _, _ = build_select(tpl, binding, limit=9999)
    assert "LIMIT 200" in sql


def test_extra_anchor_cols_must_be_safe_identifiers():
    """derived.when 键进入 extra_anchor_cols 时不得改写 SELECT。"""
    tpl, binding = _template_and_binding()
    with pytest.raises(ValueError, match="非法额外锚表列"):
        build_select(
            tpl, binding,
            extra_anchor_cols=['X" FROM sqlite_master --'],
        )
    sql, _, _ = build_select(tpl, binding, extra_anchor_cols=["STATUS"])
    assert 'a."STATUS" AS "__STATUS"' in sql
    assert "sqlite_master" not in sql


def test_field_map_alias_must_be_template_property_ident():
    """field_map 键进入 AS 别名:须为模板属性且合法标识符。"""
    tpl, _binding = _template_and_binding()
    with pytest.raises(ValueError, match="未知属性"):
        build_select(
            tpl,
            SourceBinding(
                source="digiwin_e10",
                tables=["MAIN"],
                field_map={"hex(65)": "MAIN.CODE"},
            ),
        )
    with pytest.raises(ValueError, match="未知属性"):
        build_select(
            tpl,
            SourceBinding(
                source="x", tables=["MAIN"],
                field_map={"not_a_prop": "MAIN.CODE"},
            ),
        )
