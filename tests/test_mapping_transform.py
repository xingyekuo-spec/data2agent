"""M3-T02: 纯映射转换核心 — 结构化 issue 与 apply 兼容包装一致性。"""

from __future__ import annotations

from data2agent.connect.mapping_apply import transform_object_rows
from data2agent.connect.mapping_transform import (
    DEFAULT_BREAKER_THRESHOLD,
    evaluate_object_rows,
    format_quarantine_reason,
    would_trip_breaker,
)
from data2agent.mapping import FieldExpr
from data2agent.metamodel.schema import (
    DeriveRule,
    DerivedField,
    ObjectTemplate,
    Property,
    SourceBinding,
)


def _tpl(
    *,
    keys: list[str] | None = None,
    properties: list[Property] | None = None,
) -> ObjectTemplate:
    props = properties or [
        Property(name="id", type="string"),
        Property(name="status", type="enum", enum_values=["open", "closed"]),
        Property(name="qty", type="int"),
        Property(name="amount", type="money"),
        Property(name="rate", type="decimal"),
        Property(name="flag", type="bool"),
        Property(name="phase", type="enum", enum_values=["A", "B", "C"]),
    ]
    return ObjectTemplate(
        object="Widget",
        display_name="Widget",
        description="",
        domain="销售",
        source_of_truth="test",
        keys=keys or ["id"],
        properties=props,
    )


def _binding(*, derived: dict | None = None) -> SourceBinding:
    return SourceBinding(
        source="src",
        tables=["T"],
        status="verified",
        key_map={"id": "T.id"},
        field_map={"id": "T.id", "status": "T.status", "qty": "T.qty"},
        derived=derived or {},
    )


def _expr(column: str = "c", value_map: dict[str, str] | None = None) -> FieldExpr:
    return FieldExpr(table="T", column=column, value_map=value_map)


# --- reason codes -----------------------------------------------------------


def test_map_miss_is_enum_unmapped():
    tpl = _tpl()
    binding = _binding()
    exprs = {"status": _expr(value_map={"1": "open", "2": "closed"})}
    rows = [{"id": "r1", "status": "9"}]
    result = evaluate_object_rows(tpl, binding, rows, exprs)
    assert len(result.rows) == 1
    issue = result.rows[0].issues[0]
    assert issue.reason_code == "enum_unmapped"
    assert issue.field == "status"
    assert issue.source_value == "9"
    assert format_quarantine_reason(issue) == "status: 源码值 '9' 未在 map 中声明"


def test_enum_invalid():
    tpl = _tpl()
    binding = _binding()
    exprs = {"status": _expr()}  # no map
    rows = [{"id": "r1", "status": "weird"}]
    result = evaluate_object_rows(tpl, binding, rows, exprs)
    issue = result.rows[0].issues[0]
    assert issue.reason_code == "enum_invalid"
    assert issue.field == "status"
    assert format_quarantine_reason(issue) == (
        "status: 取值 'weird' 不在枚举 ['open', 'closed'] 内"
    )


def test_type_coercion_int_decimal_money_bool():
    tpl = _tpl()
    binding = _binding()
    cases = [
        ({"id": "i", "qty": "x"}, {"qty": _expr()}, "qty", "int"),
        ({"id": "d", "rate": "nope"}, {"rate": _expr()}, "rate", "decimal"),
        ({"id": "m", "amount": "bad"}, {"amount": _expr()}, "amount", "money"),
        ({"id": "b", "flag": "maybe"}, {"flag": _expr()}, "flag", "bool"),
    ]
    for raw, exprs, field, typ in cases:
        result = evaluate_object_rows(tpl, binding, [raw], exprs)
        issue = result.rows[0].issues[0]
        assert issue.reason_code == "type_coercion", (field, issue)
        assert issue.field == field
        reason = format_quarantine_reason(issue)
        if typ == "bool":
            assert reason == f"{field}: 无法解释为 bool 的值 'maybe'"
        else:
            assert reason == f"{field}: 类型 {typ} 转换失败,值 {raw[field]!r}"


def test_derived_first_match_default_unmatched_and_invalid_enum():
    tpl = _tpl()
    derived = {
        "phase": DerivedField(
            rules=[
                DeriveRule(when={"st": "X"}, value="A"),
                DeriveRule(when={"st": "Y"}, value="B"),
            ],
            default="C",
        ),
    }
    binding = _binding(derived=derived)
    exprs = {"id": _expr("id")}

    # first-match (second rule)
    r1 = evaluate_object_rows(
        tpl, binding, [{"id": "1", "__st": "Y"}], exprs,
    )
    assert r1.rows[0].status == "mapped"
    assert r1.rows[0].output["phase"] == "B"

    # default
    r2 = evaluate_object_rows(
        tpl, binding, [{"id": "2", "__st": "Z"}], exprs,
    )
    assert r2.rows[0].output["phase"] == "C"

    # unmatched (no default)
    binding_no_default = _binding(
        derived={"phase": DerivedField(rules=[DeriveRule(when={"st": "X"}, value="A")])},
    )
    r3 = evaluate_object_rows(
        tpl, binding_no_default, [{"id": "3", "__st": "Z"}], exprs,
    )
    issue = r3.rows[0].issues[0]
    assert issue.reason_code == "derived_unmatched"
    assert issue.field == "phase"
    assert "派生规则无匹配" in format_quarantine_reason(issue)

    # invalid derived enum
    binding_bad = _binding(
        derived={
            "phase": DerivedField(
                rules=[DeriveRule(when={"st": "X"}, value="Z")],
            ),
        },
    )
    r4 = evaluate_object_rows(
        tpl, binding_bad, [{"id": "4", "__st": "X"}], exprs,
    )
    issue = r4.rows[0].issues[0]
    assert issue.reason_code == "derived_invalid_enum"
    assert format_quarantine_reason(issue) == (
        "phase: 派生值 'Z' 不在枚举 ['A', 'B', 'C'] 内"
    )


def test_business_key_missing_and_sample_duplicate():
    tpl = _tpl(keys=["id"])
    binding = _binding()
    exprs = {"id": _expr("id")}

    missing = evaluate_object_rows(tpl, binding, [{"id": None}], exprs)
    issue = missing.rows[0].issues[0]
    assert issue.reason_code == "business_key_missing"
    assert issue.field is None
    assert format_quarantine_reason(issue) == "业务键缺失:{'id': None}"

    dup = evaluate_object_rows(
        tpl, binding, [{"id": "k1"}, {"id": "k1"}], exprs,
    )
    assert dup.rows[0].status == "mapped"
    issue = dup.rows[1].issues[0]
    assert issue.reason_code == "business_key_duplicate"
    assert format_quarantine_reason(issue) == "业务键重复:{'id': 'k1'}"


def test_priority_map_before_enum_before_type():
    tpl = _tpl()
    binding = _binding()

    # map miss wins over enum-invalid (raw value also not in enum)
    r_map = evaluate_object_rows(
        tpl,
        binding,
        [{"id": "1", "status": "weird"}],
        {"status": _expr(value_map={"1": "open"})},
    )
    assert r_map.rows[0].issues[0].reason_code == "enum_unmapped"

    # after successful map, invalid enum wins (coerce would pass for enum)
    r_enum = evaluate_object_rows(
        tpl,
        binding,
        [{"id": "2", "status": "1"}],
        {"status": _expr(value_map={"1": "nope"})},
    )
    assert r_enum.rows[0].issues[0].reason_code == "enum_invalid"

    # map miss on typed field beats type_coercion
    r_type = evaluate_object_rows(
        tpl,
        binding,
        [{"id": "3", "qty": "not-int"}],
        {"qty": _expr(value_map={"1": "10"})},
    )
    assert r_type.rows[0].issues[0].reason_code == "enum_unmapped"


def test_wrapper_matches_legacy_transform_object_rows():
    tpl = _tpl()
    binding = _binding(
        derived={
            "phase": DerivedField(
                rules=[
                    DeriveRule(when={"st": "X"}, value="A"),
                    DeriveRule(when={"st": "Y"}, value="B"),
                ],
                default="C",
            ),
        },
    )
    exprs = {
        "id": _expr("id"),
        "status": _expr(value_map={"1": "open", "2": "closed", "3": "nope"}),
        "qty": _expr("qty"),
        "amount": _expr("amount"),
        "flag": _expr("flag"),
    }
    rows = [
        {"id": "ok", "status": "1", "qty": "3", "amount": "1.5", "flag": 1, "__st": "X"},
        {"id": "map", "status": "9", "qty": 1, "amount": 1.0, "flag": 0, "__st": "X"},
        {"id": "enum", "status": "3", "qty": 1, "amount": 1.0, "flag": 0, "__st": "X"},
        {"id": "typ", "status": "1", "qty": "x", "amount": 1.0, "flag": 0, "__st": "X"},
        {"id": "bool", "status": "1", "qty": 1, "amount": 1.0, "flag": "no", "__st": "X"},
        {"id": None, "status": "1", "qty": 1, "amount": 1.0, "flag": 0, "__st": "X"},
        {"id": "dup", "status": "1", "qty": 1, "amount": 1.0, "flag": 0, "__st": "Y"},
        {"id": "dup", "status": "2", "qty": 2, "amount": 2.0, "flag": 0, "__st": "Z"},
        {"id": "der", "status": "1", "qty": 1, "amount": 1.0, "flag": 0, "__st": "nope"},
    ]
    # unmatched derived: no default binding case
    binding_unmatched = _binding(
        derived={
            "phase": DerivedField(rules=[DeriveRule(when={"st": "X"}, value="A")]),
        },
    )
    rows_u = [{"id": "u", "status": "1", "qty": 1, "amount": 1.0, "flag": 0, "__st": "Z"}]
    exprs_u = {
        "id": _expr("id"),
        "status": _expr(value_map={"1": "open"}),
        "qty": _expr("qty"),
        "amount": _expr("amount"),
        "flag": _expr("flag"),
    }

    for b, r, e in ((binding, rows, exprs), (binding_unmatched, rows_u, exprs_u)):
        good, quarantined = transform_object_rows(tpl, b, r, e)
        evaluated = evaluate_object_rows(tpl, b, r, e)
        adapted_good = [row.output for row in evaluated.rows if row.status == "mapped"]
        adapted_q = [
            {
                "keys": {k: row.raw.get(k) for k in tpl.keys},
                "reason": format_quarantine_reason(row.issues[0]),
                "raw": row.raw,
            }
            for row in evaluated.rows
            if row.status == "quarantined"
        ]
        assert adapted_good == good
        assert adapted_q == quarantined


def test_would_trip_breaker_strict_gt():
    assert would_trip_breaker(0, 0, DEFAULT_BREAKER_THRESHOLD) is False
    assert would_trip_breaker(1, 20, 0.05) is False  # == 0.05
    assert would_trip_breaker(2, 20, 0.05) is True   # > 0.05
    assert would_trip_breaker(1, 10, 0.05) is True
