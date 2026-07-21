"""映射应用(E4):binding → 不可变候选表 objv_*,带隔离区与熔断。

M2-T04:不再 DROP/CREATE 稳定 obj_{Object};物化只写入调用方提供的
已校验 build_table。数据集级发布由 T05/T06 编排。

流程:
1. 用 mapping.build_select 在 raw_* 上取数(物理表名解析 + 软删过滤);
2. 纯转换:解码/校验/派生/业务键(transform_object_rows);
3. 坏行进 d2a_quarantine;隔离率超阈值 → 熔断,不写候选表;
4. 好行写入不可变候选物理表(write_candidate_table)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..mapping import build_select
from ..metamodel.dataset_publish_contract import make_build_table, validate_build_table
from ..metamodel.schema import ObjectTemplate, SourceBinding, TemplatePack
from .landing import LandingStore, _now, raw_table_name

DEFAULT_BREAKER_THRESHOLD = 0.05

_TYPE_SQL = {"int": "INTEGER", "decimal": "REAL", "money": "REAL", "bool": "INTEGER"}


class MappingCircuitBreaker(Exception):
    """单对象隔离率超阈值,映射中止,不写候选表。"""

    def __init__(
        self,
        message: str,
        *,
        total: int = 0,
        mapped: int = 0,
        quarantined: int = 0,
        batch_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.total = total
        self.mapped = mapped
        self.quarantined = quarantined
        self.batch_id = batch_id


@dataclass
class ObjectApplyResult:
    object: str
    total: int
    mapped: int
    quarantined: int
    status: str = "ok"          # ok / aborted / skipped(...)
    batch_id: str | None = None
    build_table: str | None = None


@dataclass
class ApplyReport:
    source: str
    results: list[ObjectApplyResult] = field(default_factory=list)

    @property
    def aborted(self) -> list[ObjectApplyResult]:
        return [r for r in self.results if r.status == "aborted"]


def obj_table_name(object_name: str) -> str:
    """遗留稳定表名;M2 起不得作为发布写入目标。"""
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


def _apply_derived(binding, props: dict, row: dict) -> str | None:
    """执行派生决策表(规则有序,首个匹配生效)。返回隔离原因或 None。"""
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


def transform_object_rows(
    tpl: ObjectTemplate,
    binding: SourceBinding,
    raw_rows: list[dict],
    exprs: dict,
) -> tuple[list[dict], list[dict]]:
    """纯转换:返回 (good_rows, quarantined_records)。不读写数据库。"""
    props = {p.name: p for p in tpl.properties}
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
            quarantined.append({
                "keys": {k: raw.get(k) for k in tpl.keys},
                "reason": reason,
                "raw": raw,
            })
        else:
            good.append(row)
    return good, quarantined


def write_candidate_table(
    landing: LandingStore,
    tpl: ObjectTemplate,
    rows: list[dict],
    batch_id: str,
    build_table: str,
) -> str:
    """写入不可变候选物理表;只接受严格校验过的 objv_* 名。"""
    table = validate_build_table(build_table)
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
    return table


def apply_object(
    landing: LandingStore,
    tpl: ObjectTemplate,
    source: str,
    *,
    build_table: str,
    threshold: float = DEFAULT_BREAKER_THRESHOLD,
    supersede_quarantine: bool = True,
) -> ObjectApplyResult:
    binding = next((b for b in tpl.bindings if b.source == source and b.enabled), None)
    if binding is None or not binding.field_map:
        return ObjectApplyResult(
            tpl.object, 0, 0, 0, status="skipped(无可用 binding)",
            build_table=None,
        )

    table = validate_build_table(build_table)
    derive_cols = sorted({col for spec in binding.derived.values()
                          for rule in spec.rules for col in rule.when})
    sql, params, exprs = build_select(
        tpl, binding, limit=None,
        physical=lambda t: raw_table_name(source, t),
        active_col="_d2a_deleted_at",
        extra_anchor_cols=derive_cols)
    raw_rows = [dict(r) for r in landing.con.execute(sql, params)]
    good, quarantined = transform_object_rows(tpl, binding, raw_rows, exprs)

    total = len(raw_rows)
    batch_id = uuid.uuid4().hex[:12]
    # 数据集编排(build_dataset)延后到原子发布成功后再取代旧隔离。
    if supersede_quarantine:
        landing.quarantine_supersede(source, tpl.object)
    landing.quarantine_add(source, tpl.object, quarantined, batch_id)

    if total and len(quarantined) / total > threshold:
        raise MappingCircuitBreaker(
            f"{tpl.object}: 隔离率 {len(quarantined)}/{total} 超过阈值 {threshold:.0%},"
            f"映射中止,候选表未写入;隔离明细见 d2a_quarantine(batch {batch_id})",
            total=total, mapped=len(good), quarantined=len(quarantined), batch_id=batch_id)

    write_candidate_table(landing, tpl, good, batch_id, table)
    return ObjectApplyResult(
        tpl.object, total, len(good), len(quarantined),
        batch_id=batch_id, build_table=table,
    )


def apply_objects(landing: LandingStore, pack: TemplatePack, source: str,
                  threshold: float = DEFAULT_BREAKER_THRESHOLD) -> ApplyReport:
    """为每个对象写入独立候选表;不触碰稳定 obj_* 或 published 元数据。

    单对象熔断记为 aborted,不阻塞其他对象(数据集级全体失败由 T05 接管)。
    """
    report = ApplyReport(source=source)
    run_id = landing.start_run(source, "apply")
    aborted_msgs = []
    for ordinal, tpl in enumerate(pack.objects, start=1):
        step_id: int | None = None
        build_table = make_build_table(source, tpl.object, uuid.uuid4().hex[:12])
        try:
            step_id = landing.add_step(run_id, ordinal, "object", tpl.object)
            result = apply_object(
                landing, tpl, source, build_table=build_table, threshold=threshold,
            )
            landing.update_step(
                step_id, status="ok",
                rows_in=result.total, rows_out=result.mapped,
                quarantined=result.quarantined, batch_id=result.batch_id,
                error=None if result.status == "ok" else result.status)
        except MappingCircuitBreaker as e:
            result = ObjectApplyResult(
                tpl.object, e.total, e.mapped, e.quarantined, status="aborted",
                batch_id=e.batch_id, build_table=None,
            )
            if step_id is not None:
                try:
                    landing.update_step(
                        step_id, status="aborted",
                        rows_in=result.total, rows_out=result.mapped,
                        quarantined=result.quarantined, batch_id=e.batch_id,
                        error=str(e)[:500])
                except Exception as step_error:
                    landing.finish_run(
                        run_id,
                        tables=len(report.results),
                        rows=sum(r.mapped for r in report.results),
                        status="failed",
                        detail=f"apply observation failed: {step_error}")
                    raise
            aborted_msgs.append(str(e))
        except Exception as e:
            if step_id is not None:
                try:
                    landing.update_step(step_id, status="failed", error=str(e)[:500])
                except Exception:
                    pass
            landing.finish_run(
                run_id,
                tables=len(report.results),
                rows=sum(r.mapped for r in report.results),
                status="failed",
                detail=f"apply failed: {e}")
            raise
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
