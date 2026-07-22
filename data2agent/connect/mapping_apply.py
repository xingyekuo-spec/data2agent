"""映射应用(E4):binding → 不可变候选表 objv_*,带隔离区与熔断。

M2-T04:不再 DROP/CREATE 稳定 obj_{Object};物化只写入调用方提供的
已校验 build_table。数据集级发布由 T05/T06 编排。

流程:
1. 用 mapping.build_select 在 raw_* 上取数(物理表名解析 + 软删过滤);
2. 纯转换:解码/校验/派生/业务键(transform_object_rows);
3. 坏行进 d2a_quarantine;隔离率超阈值 → 熔断,不写候选表;
4. 好行写入不可变候选物理表(write_candidate_table;已存在则冲突失败)。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field

from ..mapping import build_select, build_select_plan, split_row_provenance
from ..metamodel.dataset_publish_contract import make_build_table, validate_build_table
from ..metamodel.schema import ObjectTemplate, SourceBinding, TemplatePack
from .field_lineage import (
    ApplyVersionContext,
    build_lineage_nodes_and_inputs,
)
from .landing import LandingStore, _now, raw_table_name
from .mapping_preview import discover_pk_columns
from .mapping_transform import (
    DEFAULT_BREAKER_THRESHOLD,
    evaluate_object_rows,
    transform_object_rows,
    would_trip_breaker,
)

_TYPE_SQL = {"int": "INTEGER", "decimal": "REAL", "money": "REAL", "bool": "INTEGER"}

__all__ = [
    "ApplyReport",
    "DEFAULT_BREAKER_THRESHOLD",
    "MappingCircuitBreaker",
    "ObjectApplyResult",
    "apply_object",
    "apply_objects",
    "obj_table_name",
    "transform_object_rows",
    "would_trip_breaker",
    "write_candidate_table",
]


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


def write_candidate_table(
    landing: LandingStore,
    tpl: ObjectTemplate,
    rows: list[dict],
    batch_id: str,
    build_table: str,
    *,
    commit: bool = True,
) -> str:
    """写入不可变候选物理表;只接受严格校验过的 objv_* 名。

    commit=False 供调用方将候选表与 lineage 写入包裹在同一显式事务内。
    """
    table = validate_build_table(build_table)
    cols = [p.name for p in tpl.properties]
    col_defs = ",\n".join(
        [f'    "{p.name}" {_TYPE_SQL.get(p.type, "TEXT")}' for p in tpl.properties]
        + ['    "_d2a_mapped_at" TEXT', '    "_d2a_batch_id" TEXT'])
    pk = ", ".join(f'"{k}"' for k in tpl.keys)
    con = landing.con
    try:
        con.execute(
            f'CREATE TABLE "{table}" (\n{col_defs},\n    PRIMARY KEY ({pk})\n)'
        )
    except sqlite3.OperationalError as e:
        if "already exists" in str(e).lower():
            raise ValueError(f"候选物理表已存在,拒绝覆盖: {table}") from e
        raise
    now = _now()
    all_cols = cols + ["_d2a_mapped_at", "_d2a_batch_id"]
    col_sql = ", ".join('"{}"'.format(c) for c in all_cols)
    val_sql = ", ".join(":" + c for c in all_cols)
    con.executemany(
        f'INSERT INTO "{table}" ({col_sql}) VALUES ({val_sql})',
        [{**{c: r.get(c) for c in cols}, "_d2a_mapped_at": now, "_d2a_batch_id": batch_id}
         for r in rows])
    if commit:
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
    version_context: ApplyVersionContext | None = None,
) -> ObjectApplyResult:
    """映射单对象到候选表;version_context 存在时同时写入字段级 lineage。

    无 version_context 时行为与 M2/M3 完全一致(遗留 apply_objects 路径)。
    """
    binding = next((b for b in tpl.bindings if b.source == source and b.enabled), None)
    if binding is None or not binding.field_map:
        return ObjectApplyResult(
            tpl.object, 0, 0, 0, status="skipped(无可用 binding)",
            build_table=None,
        )

    table = validate_build_table(build_table)
    derive_cols = sorted({col for spec in binding.derived.values()
                          for rule in spec.rules for col in rule.when})

    use_lineage = version_context is not None
    physical = lambda t: raw_table_name(source, t)  # noqa: E731

    if use_lineage:
        def _pk_cols(logical_table: str) -> list[str]:
            return discover_pk_columns(landing, physical(logical_table))

        plan = build_select_plan(
            tpl, binding, limit=None,
            physical=physical,
            active_col="_d2a_deleted_at",
            extra_anchor_cols=derive_cols,
            include_provenance=True,
            table_pk_cols=_pk_cols,
        )
        sql, params, exprs = plan.sql, plan.params, plan.exprs
    else:
        plan = None
        sql, params, exprs = build_select(
            tpl, binding, limit=None,
            physical=physical,
            active_col="_d2a_deleted_at",
            extra_anchor_cols=derive_cols,
        )

    sql_rows = [dict(r) for r in landing.con.execute(sql, params)]

    if use_lineage and plan is not None:
        # 拆分 provenance;转换核心只看字段行
        transform_rows: list[dict] = []
        prov_rows: list[dict[str, object]] = []
        for row in sql_rows:
            t_row, prov = split_row_provenance(row, plan)
            transform_rows.append(t_row)
            prov_rows.append(prov)
        evaluation = evaluate_object_rows(tpl, binding, transform_rows, exprs)
        good = [r.output for r in evaluation.rows if r.status == "mapped"]
        quarantined_records = [
            {
                "keys": {k: r.raw.get(k) for k in tpl.keys},
                "reason": r.issues[0].detail if r.issues else "",
                "raw": r.raw,
            }
            for r in evaluation.rows if r.status == "quarantined"
        ]
    else:
        evaluation = None
        prov_rows = []
        good, quarantined_records = transform_object_rows(
            tpl, binding, sql_rows, exprs,
        )

    total = len(sql_rows)
    batch_id = (
        version_context.map_batch_id if version_context else uuid.uuid4().hex[:12]
    )
    # 数据集编排(build_dataset)延后到原子发布成功后再取代旧隔离。
    if supersede_quarantine:
        landing.quarantine_supersede(source, tpl.object)
    landing.quarantine_add(source, tpl.object, quarantined_records, batch_id)

    if would_trip_breaker(len(quarantined_records), total, threshold):
        raise MappingCircuitBreaker(
            f"{tpl.object}: 隔离率 {len(quarantined_records)}/{total} "
            f"超过阈值 {threshold:.0%},"
            f"映射中止,候选表未写入;隔离明细见 d2a_quarantine(batch {batch_id})",
            total=total,
            mapped=len(good),
            quarantined=len(quarantined_records),
            batch_id=batch_id,
        )

    if use_lineage and version_context is not None and evaluation is not None:
        assert plan is not None  # noqa: S101
        nodes, inputs = build_lineage_nodes_and_inputs(
            evaluations=evaluation.rows,
            provenance_rows=prov_rows,
            template_keys=tpl.keys,
            object_name=tpl.object,
            context=version_context,
            source=source,
            anchor_table=binding.tables[0],
            plan_provenance=plan.provenance,
            exprs=exprs,
        )
        # 同一事务:候选表 + lineage 节点 + 输入边 + 完整性计数
        isolation = landing.con.isolation_level
        landing.con.isolation_level = None
        try:
            landing.con.execute("BEGIN IMMEDIATE")
            try:
                write_candidate_table(
                    landing, tpl, good, batch_id, table, commit=False,
                )
                landing.insert_field_lineage(nodes, inputs, commit=False)
                landing.update_object_lineage_meta(
                    version_context.dataset_version,
                    tpl.object,
                    lineage_schema_version=1,
                    lineage_field_count=len(nodes),
                    commit=False,
                )
                # 完整性校验:mapped 行 × 模板属性数
                expected = len(good) * len(tpl.properties)
                if len(nodes) != expected:
                    raise ValueError(
                        f"lineage 完整性校验失败:期望 {expected} 节点,"
                        f"实际 {len(nodes)}"
                    )
                landing.con.execute("COMMIT")
            except Exception:
                try:
                    landing.con.execute("ROLLBACK")
                except Exception:
                    pass
                # 候选表在事务内创建,ROLLBACK 后自动消失
                raise
        finally:
            landing.con.isolation_level = isolation
    else:
        write_candidate_table(landing, tpl, good, batch_id, table)

    return ObjectApplyResult(
        tpl.object, total, len(good), len(quarantined_records),
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
