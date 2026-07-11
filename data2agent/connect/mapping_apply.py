"""映射应用(E4):binding → 落地库物化对象表 obj_{Object},带隔离区与熔断。

流程(docs/design/02-extraction.md §2/§7):
1. 用 mapping.build_select 在 raw_* 上取数(物理表名解析 + 软删过滤,不限行);
2. 逐行解码(map)与校验:业务键非空且唯一、枚举取值合法、数值类型可转换;
   坏行进 d2a_quarantine(原样 JSON + 原因),批次继续;
3. 熔断:单对象隔离率超阈值(默认 5%)→ 该对象回滚保留旧数据并中止,
   防止系统性口径错误(如源表结构变更)被静默吞掉;
4. 好行重建 obj_{Object}(主键 = 模板 keys),供 MCP 网关与指标消费。

已知边界:ref 解析失败(外键悬空 vs 本身为空)在解码后无法区分,暂不隔离;
兜底靠上游对账与下游指标口径警示。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from ..mapping import build_select
from ..metamodel.schema import ObjectTemplate, TemplatePack
from .landing import LandingStore, _now, raw_table_name

DEFAULT_BREAKER_THRESHOLD = 0.05

_TYPE_SQL = {"int": "INTEGER", "decimal": "REAL", "money": "REAL", "bool": "INTEGER"}
# 其余类型(string/text/date/datetime/ref/enum)落 TEXT


class MappingCircuitBreaker(Exception):
    """单对象隔离率超阈值,映射中止,旧对象表保留。"""


@dataclass
class ObjectApplyResult:
    object: str
    total: int
    mapped: int
    quarantined: int
    status: str = "ok"          # ok / aborted


@dataclass
class ApplyReport:
    source: str
    results: list[ObjectApplyResult] = field(default_factory=list)

    @property
    def aborted(self) -> list[ObjectApplyResult]:
        return [r for r in self.results if r.status == "aborted"]


def obj_table_name(object_name: str) -> str:
    return f"obj_{object_name}"


def _coerce(prop, value):
    """按属性类型转换;返回 (值, 错误原因或 None)。"""
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
            return None, f"{prop.name}: 无法解释为 bool 的值 {value!r}"
        return value, None
    except (TypeError, ValueError):
        return None, f"{prop.name}: 类型 {prop.type} 转换失败,值 {value!r}"


def apply_object(landing: LandingStore, tpl: ObjectTemplate, source: str,
                 threshold: float = DEFAULT_BREAKER_THRESHOLD) -> ObjectApplyResult:
    binding = next((b for b in tpl.bindings if b.source == source), None)
    if binding is None or not binding.field_map:
        return ObjectApplyResult(tpl.object, 0, 0, 0, status="skipped(无可用 binding)")

    derive_cols = sorted({col for spec in binding.derived.values()
                          for rule in spec.rules for col in rule.when})
    sql, params, exprs = build_select(
        tpl, binding, limit=None,
        physical=lambda t: raw_table_name(source, t),
        active_col="_d2a_deleted_at",
        extra_anchor_cols=derive_cols)
    raw_rows = [dict(r) for r in landing.con.execute(sql, params)]

    props = {p.name: p for p in tpl.properties}
    batch_id = uuid.uuid4().hex[:12]
    good: list[dict] = []
    quarantined: list[dict] = []
    seen_keys: set[tuple] = set()

    for raw in raw_rows:
        row, reason = dict(raw), None
        for name, expr in exprs.items():
            prop = props.get(name)
            if prop is None:
                continue
            v = row.get(name)
            if expr.value_map is not None and v is not None:
                if v not in expr.value_map:
                    reason = f"{name}: 源码值 {v!r} 未在 map 中声明"
                    break
                v = expr.value_map[v]
            if prop.type == "enum" and v is not None and v not in prop.enum_values:
                reason = f"{name}: 取值 {v!r} 不在枚举 {prop.enum_values} 内"
                break
            v, err = _coerce(prop, v)
            if err:
                reason = err
                break
            row[name] = v
        if reason is None:
            reason = _apply_derived(binding, props, row)
        if reason is None:
            key = tuple(row.get(k) for k in tpl.keys)
            if any(v is None for v in key):
                reason = f"业务键缺失:{dict(zip(tpl.keys, key))}"
            elif key in seen_keys:
                reason = f"业务键重复:{dict(zip(tpl.keys, key))}"
            else:
                seen_keys.add(key)
        if reason is not None:
            quarantined.append({"keys": {k: raw.get(k) for k in tpl.keys},
                                "reason": reason, "raw": raw})
        else:
            good.append(row)

    total = len(raw_rows)
    landing.quarantine_supersede(source, tpl.object)
    landing.quarantine_add(source, tpl.object, quarantined, batch_id)

    if total and len(quarantined) / total > threshold:
        raise MappingCircuitBreaker(
            f"{tpl.object}: 隔离率 {len(quarantined)}/{total} 超过阈值 {threshold:.0%},"
            f"映射中止,旧对象表保留;隔离明细见 d2a_quarantine(batch {batch_id})")

    _rebuild_obj_table(landing, tpl, good, batch_id)
    return ObjectApplyResult(tpl.object, total, len(good), len(quarantined))


def _apply_derived(binding, props: dict, row: dict) -> str | None:
    """执行派生决策表(规则有序,首个匹配生效)。返回隔离原因或 None。

    条件值与落地原样值做等值比较(None = IS NULL);无匹配且无 default
    视为契约不完整 → 隔离,而不是静默给空值。
    """
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
            seen = {col: row.get(f"__{col}")
                    for s in binding.derived.values()
                    for r in s.rules for col in r.when}
            return f"{prop_name}: 派生规则无匹配(源值 {seen})"
        prop = props.get(prop_name)
        if prop is not None and prop.type == "enum" and value not in prop.enum_values:
            return f"{prop_name}: 派生值 {value!r} 不在枚举 {prop.enum_values} 内"
        row[prop_name] = value
    return None


def _rebuild_obj_table(landing: LandingStore, tpl: ObjectTemplate,
                       rows: list[dict], batch_id: str) -> None:
    table = obj_table_name(tpl.object)
    cols = [p.name for p in tpl.properties]
    col_defs = ",\n".join(
        [f'    "{p.name}" {_TYPE_SQL.get(p.type, "TEXT")}' for p in tpl.properties]
        + ['    "_d2a_mapped_at" TEXT', '    "_d2a_batch_id" TEXT'])
    pk = ", ".join(f'"{k}"' for k in tpl.keys)
    con = landing.con
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.execute(f'CREATE TABLE "{table}" (\n{col_defs},\n    PRIMARY KEY ({pk})\n)')
    now = _now()
    all_cols = cols + ["_d2a_mapped_at", "_d2a_batch_id"]
    col_sql = ", ".join('"{}"'.format(c) for c in all_cols)
    val_sql = ", ".join(":" + c for c in all_cols)
    con.executemany(
        f'INSERT INTO "{table}" ({col_sql}) VALUES ({val_sql})',
        [{**{c: r.get(c) for c in cols}, "_d2a_mapped_at": now, "_d2a_batch_id": batch_id}
         for r in rows])
    con.commit()


def apply_objects(landing: LandingStore, pack: TemplatePack, source: str,
                  threshold: float = DEFAULT_BREAKER_THRESHOLD) -> ApplyReport:
    """物化全部有 binding 的对象;单对象熔断记为 aborted,不阻塞其他对象。"""
    report = ApplyReport(source=source)
    run_id = landing.start_run(source)
    aborted_msgs = []
    for tpl in pack.objects:
        try:
            result = apply_object(landing, tpl, source, threshold)
        except MappingCircuitBreaker as e:
            result = ObjectApplyResult(tpl.object, 0, 0, 0, status="aborted")
            aborted_msgs.append(str(e))
        if not result.status.startswith("skipped"):
            report.results.append(result)
    landing.finish_run(
        run_id,
        tables=len(report.results),
        rows=sum(r.mapped for r in report.results),
        status="failed" if report.aborted else "ok",
        detail="apply: " + ("; ".join(aborted_msgs) if aborted_msgs else
               f"隔离 {sum(r.quarantined for r in report.results)} 行"))
    return report
