"""映射表达式文法与 SQL 构建测试。"""

import pytest

from data2agent.shared.mapping import (
    FieldExpr,
    build_select,
    build_select_plan,
    parse_field_expr,
    split_row_provenance,
)
from data2agent.shared.metamodel.schema import ObjectTemplate, SourceBinding


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
        object="Sample", display_name="样例", domain="销售", keys=["code"],
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


# --- M4-T03 SelectPlan / provenance -----------------------------------------


def _pk_lookup(mapping: dict[str, list[str]]):
    def _lookup(table: str) -> list[str]:
        if table not in mapping:
            raise AssertionError(f"unexpected table {table}")
        return mapping[table]
    return _lookup


def test_build_select_wrapper_matches_plan_without_provenance():
    tpl, binding = _template_and_binding()
    legacy = build_select(
        tpl, binding, filters={"code": "C001"}, order_by="cur", desc=True, limit=5,
        extra_anchor_cols=["STATUS"], active_col="_d2a_deleted_at",
        physical=lambda t: f"raw_{t}",
    )
    plan = build_select_plan(
        tpl, binding, filters={"code": "C001"}, order_by="cur", desc=True, limit=5,
        extra_anchor_cols=["STATUS"], active_col="_d2a_deleted_at",
        physical=lambda t: f"raw_{t}",
        include_provenance=False,
    )
    assert plan.as_legacy_tuple() == legacy
    assert plan.provenance == ()


def test_select_plan_provenance_single_and_composite_pk():
    tpl, binding = _template_and_binding()
    plan = build_select_plan(
        tpl, binding,
        limit=None,
        physical=lambda t: f"raw_{t}",
        active_col="_d2a_deleted_at",
        include_provenance=True,
        table_pk_cols=_pk_lookup({
            "MAIN": ["Id"],
            "CURRENCY": ["Id"],
        }),
    )
    assert 'a."Id" AS "__d2a_p_a_pk_0_Id"' in plan.sql
    assert 'a."_d2a_batch_id" AS "__d2a_p_a_batch"' in plan.sql
    assert 'a."CURRENCY_ID" AS "__d2a_p_j1_fk_CURRENCY_ID"' in plan.sql
    assert 'j1."Id" AS "__d2a_p_j1_pk_0_Id"' in plan.sql
    assert 'j1."_d2a_batch_id" AS "__d2a_p_j1_batch"' in plan.sql
    assert 'AND j1."_d2a_deleted_at" IS NULL' in plan.sql
    roles = {p.role for p in plan.provenance}
    assert roles >= {"anchor_pk", "join_pk", "join_fk", "extract_batch"}
    cur_join = [p for p in plan.provenance if p.role == "join_pk"][0]
    assert cur_join.property_names == ("cur",)
    assert "code" in plan.exprs and "__d2a_p_a_batch" not in plan.exprs

    # 复合主键
    plan2 = build_select_plan(
        tpl, binding, limit=None, include_provenance=True,
        table_pk_cols=_pk_lookup({
            "MAIN": ["ORG_ID", "DOC_ID"],
            "CURRENCY": ["ORG_ID", "Id"],
        }),
    )
    assert "__d2a_p_a_pk_0_ORG_ID" in plan2.sql
    assert "__d2a_p_a_pk_1_DOC_ID" in plan2.sql
    assert "__d2a_p_j1_pk_0_ORG_ID" in plan2.sql
    assert "__d2a_p_j1_pk_1_Id" in plan2.sql


def test_select_plan_multi_join_same_target_table_and_alias_collision():
    """同目标表多个 FK、同名列用不同 join 别名区分。"""
    tpl = ObjectTemplate(
        object="Sample2", display_name="样例", domain="销售", keys=["code"],
        properties=[
            {"name": "code", "type": "string"},
            {"name": "cur", "type": "string"},
            {"name": "alt_cur", "type": "string"},
        ],
    )
    binding = SourceBinding(
        source="digiwin_e10",
        tables=["MAIN"],
        field_map={
            "code": "MAIN.CODE",
            "cur": "CURRENCY.CODE (join MAIN.CURRENCY_ID)",
            "alt_cur": "CURRENCY.CODE (join MAIN.ALT_CURRENCY_ID)",
        },
    )
    plan = build_select_plan(
        tpl, binding, limit=None, include_provenance=True,
        table_pk_cols=_pk_lookup({"MAIN": ["Id"], "CURRENCY": ["Id"]}),
    )
    assert 'LEFT JOIN "CURRENCY" j1 ON j1."Id" = a."CURRENCY_ID"' in plan.sql
    assert 'LEFT JOIN "CURRENCY" j2 ON j2."Id" = a."ALT_CURRENCY_ID"' in plan.sql
    assert "__d2a_p_j1_pk_0_Id" in plan.sql
    assert "__d2a_p_j2_pk_0_Id" in plan.sql
    assert plan.provenance_aliases().isdisjoint(plan.exprs)


def test_select_plan_requires_pk_and_rejects_missing_join_pk():
    tpl, binding = _template_and_binding()
    with pytest.raises(ValueError, match="table_pk_cols"):
        build_select_plan(tpl, binding, include_provenance=True)
    with pytest.raises(ValueError, match="锚表 .* 无 DDL 主键"):
        build_select_plan(
            tpl, binding, include_provenance=True,
            table_pk_cols=_pk_lookup({"MAIN": [], "CURRENCY": ["Id"]}),
        )


def test_split_row_provenance_keeps_transform_fields_only():
    tpl, binding = _template_and_binding()
    plan = build_select_plan(
        tpl, binding, limit=None, include_provenance=True,
        extra_anchor_cols=["STATUS"],
        table_pk_cols=_pk_lookup({"MAIN": ["Id"], "CURRENCY": ["Id"]}),
    )
    row = {
        "code": "C1",
        "cur": "CNY",
        "__STATUS": "A",
        "__d2a_p_a_pk_0_Id": 9,
        "__d2a_p_a_batch": "batch-a",
        "__d2a_p_j1_fk_CURRENCY_ID": 3,
        "__d2a_p_j1_pk_0_Id": None,  # join 缺失
        "__d2a_p_j1_batch": None,
    }
    transform_row, prov = split_row_provenance(row, plan)
    assert transform_row == {"code": "C1", "cur": "CNY", "__STATUS": "A"}
    assert prov["__d2a_p_j1_pk_0_Id"] is None
    assert "__d2a_p_a_batch" in prov
    # derived_condition 也登记在 provenance 元数据中,但别名仍留给 transform
    assert any(p.role == "derived_condition" for p in plan.provenance)
    assert "__STATUS" in transform_row
