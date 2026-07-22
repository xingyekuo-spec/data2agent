"""纯映射转换核心:解码 / 校验 / 派生 / 业务键。

正式 apply 与 Preview 共用本模块。无数据库依赖;返回结构化行评估。
隔离中文文案由 format_quarantine_reason 生成,须与历史 apply 文本逐字一致。

M4-T02:在真实求值点记录 FieldTrace(read/map/coerce/derived);不另建转换器。
join 源记录身份由 T03 SelectPlan 合并;本模块仅在表达式声明 join 时记下步骤。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..mapping import FieldExpr
from ..metamodel.schema import ObjectTemplate, Property, SourceBinding

DEFAULT_BREAKER_THRESHOLD = 0.05

ReasonCode = Literal[
    "enum_unmapped",
    "enum_invalid",
    "type_coercion",
    "derived_unmatched",
    "derived_invalid_enum",
    "business_key_missing",
    "business_key_duplicate",
]

FieldTraceStatus = Literal["available", "unavailable"]

FieldUnavailableReason = Literal[
    "property_unmapped",
    "join_target_missing",
    "source_evidence_unavailable",
]

TransformStepKind = Literal[
    "read",
    "join",
    "map",
    "coerce",
    "derived_rule",
    "derived_default",
]

InputRole = Literal["value", "join_fk", "derived_condition"]

_COERCE_TYPES = frozenset({"int", "decimal", "money", "bool"})


@dataclass(frozen=True)
class TransformIssue:
    reason_code: ReasonCode
    field: str | None
    detail: str
    source_value: Any | None = None


@dataclass(frozen=True)
class DerivedHit:
    """单行单个 derived 字段的求值结果(供 Preview 覆盖率聚合)。

    outcome:
      rule — 有序规则首命中(rule_index 为规则下标)
      default — 无规则命中但使用了 default
      unmatched — 无规则且无 default(随后隔离)
    """

    field: str
    outcome: Literal["rule", "default", "unmatched"]
    rule_index: int | None = None


@dataclass(frozen=True)
class TransformStep:
    """实际执行过的有序转换步骤。"""

    kind: TransformStepKind
    before: Any = None
    after: Any = None
    map_hit: bool | None = None
    coerce_type: str | None = None
    derived_rule_index: int | None = None
    derived_when: dict[str, Any] | None = None
    detail: str | None = None


@dataclass
class FieldTrace:
    """单字段转换追溯:与最终 output 值一致,不另算第二套逻辑。"""

    property: str
    raw_value: Any = None
    result_value: Any = None
    status: FieldTraceStatus = "available"
    unavailable_reason: FieldUnavailableReason | None = None
    steps: list[TransformStep] = field(default_factory=list)
    derived_hit: DerivedHit | None = None
    input_roles: list[InputRole] = field(default_factory=list)


@dataclass
class RowEvaluation:
    status: Literal["mapped", "quarantined"]
    raw: dict
    output: dict | None = None
    issues: list[TransformIssue] = field(default_factory=list)
    derived_hits: list[DerivedHit] = field(default_factory=list)
    field_traces: list[FieldTrace] = field(default_factory=list)


@dataclass
class TransformEvaluation:
    rows: list[RowEvaluation]

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def mapped(self) -> int:
        return sum(1 for r in self.rows if r.status == "mapped")

    @property
    def quarantined(self) -> int:
        return sum(1 for r in self.rows if r.status == "quarantined")


def would_trip_breaker(
    quarantined: int,
    total: int,
    threshold: float = DEFAULT_BREAKER_THRESHOLD,
) -> bool:
    """与正式 apply 一致:仅当 quarantined/total > threshold 时为 True(== 不触发)。"""
    if not total:
        return False
    return quarantined / total > threshold


def format_quarantine_reason(issue: TransformIssue) -> str:
    """将结构化 issue 适配为历史隔离 reason 文本(逐字兼容)。"""
    return issue.detail


def _coerce(prop: Property, value: Any) -> tuple[Any, TransformIssue | None]:
    """按属性类型转换;返回 (值, issue 或 None)。"""
    if value is None:
        return None, None
    try:
        if prop.type == "int":
            return int(value), None
        if prop.type in ("decimal", "money"):
            return float(value), None
        if prop.type == "bool":
            if value in (0, 1, True, False):
                return int(bool(value)), None
            return None, TransformIssue(
                reason_code="type_coercion",
                field=prop.name,
                detail=f"{prop.name}: 无法解释为 bool 的值 {value!r}",
                source_value=value,
            )
        return value, None
    except (TypeError, ValueError):
        return None, TransformIssue(
            reason_code="type_coercion",
            field=prop.name,
            detail=f"{prop.name}: 类型 {prop.type} 转换失败,值 {value!r}",
            source_value=value,
        )


def _trace_direct_field(
    name: str,
    prop: Property,
    expr: FieldExpr,
    row: dict,
) -> tuple[Any, TransformIssue | None, FieldTrace]:
    """对单个 field_map/key_map 属性执行 read→join?→map?→coerce?,并记录 trace。"""
    raw_value = row.get(name)
    v = raw_value
    steps: list[TransformStep] = [
        TransformStep(kind="read", before=None, after=raw_value),
    ]
    roles: list[InputRole] = ["value"]

    if expr.join_fk is not None:
        roles.append("join_fk")
        fk_table, fk_col = expr.join_fk
        steps.append(
            TransformStep(
                kind="join",
                before=raw_value,
                after=v,
                detail=f"join {fk_table}.{fk_col} → {expr.table}.{expr.column}",
            )
        )

    if expr.value_map is not None and v is not None:
        if v not in expr.value_map:
            steps.append(
                TransformStep(kind="map", before=v, after=None, map_hit=False)
            )
            issue = TransformIssue(
                reason_code="enum_unmapped",
                field=name,
                detail=f"{name}: 源码值 {v!r} 未在 map 中声明",
                source_value=v,
            )
            return None, issue, FieldTrace(
                property=name,
                raw_value=raw_value,
                result_value=None,
                status="unavailable",
                steps=steps,
                input_roles=roles,
            )
        mapped = expr.value_map[v]
        steps.append(
            TransformStep(kind="map", before=v, after=mapped, map_hit=True)
        )
        v = mapped

    if prop.type == "enum" and v is not None and v not in prop.enum_values:
        issue = TransformIssue(
            reason_code="enum_invalid",
            field=name,
            detail=f"{name}: 取值 {v!r} 不在枚举 {prop.enum_values} 内",
            source_value=v,
        )
        return None, issue, FieldTrace(
            property=name,
            raw_value=raw_value,
            result_value=None,
            status="unavailable",
            steps=steps,
            input_roles=roles,
        )

    if prop.type in _COERCE_TYPES:
        before = v
        v, err = _coerce(prop, v)
        steps.append(
            TransformStep(
                kind="coerce",
                before=before,
                after=v if err is None else None,
                coerce_type=prop.type,
            )
        )
        if err:
            return None, err, FieldTrace(
                property=name,
                raw_value=raw_value,
                result_value=None,
                status="unavailable",
                steps=steps,
                input_roles=roles,
            )
    else:
        v, err = _coerce(prop, v)
        if err:
            # 非数值类型极少走到这里;仍保持与历史行为一致
            return None, err, FieldTrace(
                property=name,
                raw_value=raw_value,
                result_value=None,
                status="unavailable",
                steps=steps,
                input_roles=roles,
            )

    return v, None, FieldTrace(
        property=name,
        raw_value=raw_value,
        result_value=v,
        status="available",
        steps=steps,
        input_roles=roles,
    )


def _apply_derived(
    binding: SourceBinding,
    props: dict[str, Property],
    row: dict,
) -> tuple[TransformIssue | None, list[DerivedHit], list[FieldTrace]]:
    """执行派生决策表(规则有序,首个匹配生效)。

    返回 (issue 或 None, DerivedHit 列表, 每字段 FieldTrace)。
    命中元数据不影响隔离文案;format_quarantine_reason 仍只用 issue.detail。
    """
    hits: list[DerivedHit] = []
    traces: list[FieldTrace] = []
    for prop_name, spec in binding.derived.items():
        value = None
        matched = False
        rule_index: int | None = None
        matched_when: dict[str, Any] | None = None
        for idx, rule in enumerate(spec.rules):
            if all(row.get(f"__{col}") == expect for col, expect in rule.when.items()):
                value, matched, rule_index = rule.value, True, idx
                matched_when = dict(rule.when)
                break

        if matched:
            hit = DerivedHit(field=prop_name, outcome="rule", rule_index=rule_index)
            hits.append(hit)
            steps = [
                TransformStep(
                    kind="derived_rule",
                    before=None,
                    after=value,
                    derived_rule_index=rule_index,
                    derived_when=matched_when,
                )
            ]
        elif spec.default is not None:
            value = spec.default
            matched = True
            hit = DerivedHit(field=prop_name, outcome="default")
            hits.append(hit)
            steps = [
                TransformStep(kind="derived_default", before=None, after=value)
            ]
        else:
            hit = DerivedHit(field=prop_name, outcome="unmatched")
            hits.append(hit)
            seen = {
                col: row.get(f"__{col}")
                for s in binding.derived.values()
                for r in s.rules
                for col in r.when
            }
            traces.append(
                FieldTrace(
                    property=prop_name,
                    raw_value=None,
                    result_value=None,
                    status="unavailable",
                    steps=[],
                    derived_hit=hit,
                    input_roles=["derived_condition"],
                )
            )
            return (
                TransformIssue(
                    reason_code="derived_unmatched",
                    field=prop_name,
                    detail=f"{prop_name}: 派生规则无匹配(源值 {seen})",
                    source_value=seen,
                ),
                hits,
                traces,
            )

        prop = props.get(prop_name)
        if prop is not None and prop.type == "enum" and value not in prop.enum_values:
            traces.append(
                FieldTrace(
                    property=prop_name,
                    raw_value=None,
                    result_value=None,
                    status="unavailable",
                    steps=steps,
                    derived_hit=hit,
                    input_roles=["derived_condition"],
                )
            )
            return (
                TransformIssue(
                    reason_code="derived_invalid_enum",
                    field=prop_name,
                    detail=f"{prop_name}: 派生值 {value!r} 不在枚举 {prop.enum_values} 内",
                    source_value=value,
                ),
                hits,
                traces,
            )
        row[prop_name] = value
        traces.append(
            FieldTrace(
                property=prop_name,
                raw_value=None,
                result_value=value,
                status="available",
                steps=steps,
                derived_hit=hit,
                input_roles=["derived_condition"],
            )
        )
    return None, hits, traces


def _unmapped_property_traces(
    tpl: ObjectTemplate,
    binding: SourceBinding,
    exprs: dict[str, FieldExpr],
    traced: set[str],
) -> list[FieldTrace]:
    """模板属性既无 field_map/key_map 也无 derived 时标记 property_unmapped。"""
    covered = set(exprs) | set(binding.derived)
    out: list[FieldTrace] = []
    for prop in tpl.properties:
        if prop.name in traced or prop.name in covered:
            continue
        out.append(
            FieldTrace(
                property=prop.name,
                raw_value=None,
                result_value=None,
                status="unavailable",
                unavailable_reason="property_unmapped",
                steps=[],
                input_roles=[],
            )
        )
    return out


def _ordered_field_traces(
    tpl: ObjectTemplate,
    by_name: dict[str, FieldTrace],
) -> list[FieldTrace]:
    """按模板属性顺序排列;额外字段追加在后。"""
    ordered: list[FieldTrace] = []
    seen: set[str] = set()
    for prop in tpl.properties:
        trace = by_name.get(prop.name)
        if trace is not None:
            ordered.append(trace)
            seen.add(prop.name)
    for name, trace in by_name.items():
        if name not in seen:
            ordered.append(trace)
    return ordered


def evaluate_object_rows(
    tpl: ObjectTemplate,
    binding: SourceBinding,
    raw_rows: list[dict],
    exprs: dict[str, FieldExpr],
) -> TransformEvaluation:
    """纯转换:对输入行做结构化评估。不读写数据库。"""
    props = {p.name: p for p in tpl.properties}
    evaluations: list[RowEvaluation] = []
    seen_keys: set[tuple] = set()

    for raw in raw_rows:
        row: dict = dict(raw)
        issue: TransformIssue | None = None
        derived_hits: list[DerivedHit] = []
        traces_by_name: dict[str, FieldTrace] = {}

        for name, expr in exprs.items():
            prop = props.get(name)
            if prop is None:
                continue
            v, err, trace = _trace_direct_field(name, prop, expr, row)
            traces_by_name[name] = trace
            if err:
                issue = err
                break
            row[name] = v

        if issue is None:
            issue, derived_hits, derived_traces = _apply_derived(binding, props, row)
            for trace in derived_traces:
                traces_by_name[trace.property] = trace

        if issue is None:
            key = tuple(row.get(k) for k in tpl.keys)
            if any(v is None for v in key):
                issue = TransformIssue(
                    reason_code="business_key_missing",
                    field=None,
                    detail=f"业务键缺失:{dict(zip(tpl.keys, key))}",
                    source_value=dict(zip(tpl.keys, key)),
                )
            elif key in seen_keys:
                issue = TransformIssue(
                    reason_code="business_key_duplicate",
                    field=None,
                    detail=f"业务键重复:{dict(zip(tpl.keys, key))}",
                    source_value=dict(zip(tpl.keys, key)),
                )
            else:
                seen_keys.add(key)

        if issue is None:
            for trace in _unmapped_property_traces(
                tpl, binding, exprs, set(traces_by_name)
            ):
                traces_by_name[trace.property] = trace

        field_traces = _ordered_field_traces(tpl, traces_by_name)

        if issue is not None:
            evaluations.append(
                RowEvaluation(
                    status="quarantined",
                    raw=raw,
                    output=None,
                    issues=[issue],
                    derived_hits=derived_hits,
                    field_traces=field_traces,
                )
            )
        else:
            evaluations.append(
                RowEvaluation(
                    status="mapped",
                    raw=raw,
                    output=row,
                    issues=[],
                    derived_hits=derived_hits,
                    field_traces=field_traces,
                )
            )
    return TransformEvaluation(rows=evaluations)


def transform_object_rows(
    tpl: ObjectTemplate,
    binding: SourceBinding,
    raw_rows: list[dict],
    exprs: dict[str, FieldExpr],
) -> tuple[list[dict], list[dict]]:
    """兼容包装:返回 (good_rows, quarantined_records),文案与历史 apply 一致。"""
    evaluation = evaluate_object_rows(tpl, binding, raw_rows, exprs)
    good: list[dict] = []
    quarantined: list[dict] = []
    for row in evaluation.rows:
        if row.status == "quarantined":
            quarantined.append({
                "keys": {k: row.raw.get(k) for k in tpl.keys},
                "reason": format_quarantine_reason(row.issues[0]),
                "raw": row.raw,
            })
        else:
            assert row.output is not None
            good.append(row.output)
    return good, quarantined
