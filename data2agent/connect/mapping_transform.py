"""纯映射转换核心:解码 / 校验 / 派生 / 业务键。

正式 apply 与 Preview 共用本模块。无数据库依赖;返回结构化行评估。
隔离中文文案由 format_quarantine_reason 生成,须与历史 apply 文本逐字一致。
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


@dataclass(frozen=True)
class TransformIssue:
    reason_code: ReasonCode
    field: str | None
    detail: str
    source_value: Any | None = None


@dataclass
class RowEvaluation:
    status: Literal["mapped", "quarantined"]
    raw: dict
    output: dict | None = None
    issues: list[TransformIssue] = field(default_factory=list)


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


def _apply_derived(
    binding: SourceBinding,
    props: dict[str, Property],
    row: dict,
) -> TransformIssue | None:
    """执行派生决策表(规则有序,首个匹配生效)。返回 issue 或 None。"""
    for prop_name, spec in binding.derived.items():
        value = None
        matched = False
        for rule in spec.rules:
            if all(row.get(f"__{col}") == expect for col, expect in rule.when.items()):
                value, matched = rule.value, True
                break
        if not matched and spec.default is not None:
            value, matched = spec.default, True
        if not matched:
            seen = {
                col: row.get(f"__{col}")
                for s in binding.derived.values()
                for r in s.rules
                for col in r.when
            }
            return TransformIssue(
                reason_code="derived_unmatched",
                field=prop_name,
                detail=f"{prop_name}: 派生规则无匹配(源值 {seen})",
                source_value=seen,
            )
        prop = props.get(prop_name)
        if prop is not None and prop.type == "enum" and value not in prop.enum_values:
            return TransformIssue(
                reason_code="derived_invalid_enum",
                field=prop_name,
                detail=f"{prop_name}: 派生值 {value!r} 不在枚举 {prop.enum_values} 内",
                source_value=value,
            )
        row[prop_name] = value
    return None


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
        for name, expr in exprs.items():
            prop = props.get(name)
            if prop is None:
                continue
            v = row.get(name)
            if expr.value_map is not None and v is not None:
                if v not in expr.value_map:
                    issue = TransformIssue(
                        reason_code="enum_unmapped",
                        field=name,
                        detail=f"{name}: 源码值 {v!r} 未在 map 中声明",
                        source_value=v,
                    )
                    break
                v = expr.value_map[v]
            if prop.type == "enum" and v is not None and v not in prop.enum_values:
                issue = TransformIssue(
                    reason_code="enum_invalid",
                    field=name,
                    detail=f"{name}: 取值 {v!r} 不在枚举 {prop.enum_values} 内",
                    source_value=v,
                )
                break
            v, err = _coerce(prop, v)
            if err:
                issue = err
                break
            row[name] = v
        if issue is None:
            issue = _apply_derived(binding, props, row)
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
        if issue is not None:
            evaluations.append(
                RowEvaluation(
                    status="quarantined",
                    raw=raw,
                    output=None,
                    issues=[issue],
                )
            )
        else:
            evaluations.append(
                RowEvaluation(status="mapped", raw=raw, output=row, issues=[])
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
