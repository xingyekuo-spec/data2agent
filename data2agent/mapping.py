"""映射表达式与对象查询 SQL 构建:binding(field_map)→ 只读 SELECT。

表达式文法(SourceBinding.key_map / field_map 的值):

    表.字段
    表.字段 (join 锚表.外键字段)              # 外键在锚表上,目标表以 Id 为主键
    表.字段 (map 源值→对象值 / 源值→对象值)   # 源系统编码 → 对象模型取值

join 与 map 可同时出现,顺序固定为先 join 后 map;binding.tables[0] 为锚表
(对象一行 = 锚表一行)。本模块被 MCP query_objects 消费,后续抽取管道复用。

M4-T03:build_select_plan 为 canonical builder;正式 apply 可投影 provenance
(锚/关联主键、join 外键、_d2a_batch_id)。build_select 保持兼容包装,
Preview/MCP 默认 SQL 不变。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Optional

from .metamodel.schema import ObjectTemplate, SourceBinding

_EXPR_RE = re.compile(
    r"""^\s*
    (?P<table>[A-Za-z_]\w*)\.(?P<column>[A-Za-z_]\w*)
    (?:\s*\(join\s+(?P<jt>[A-Za-z_]\w*)\.(?P<jc>[A-Za-z_]\w*)\s*\))?
    (?:\s*\(map\s+(?P<vmap>[^)]+)\))?
    \s*$""",
    re.X,
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# provenance 内部别名保留前缀;不得与模板属性或 __derived 条件列冲突。
_PROV_PREFIX = "__d2a_p_"
_BATCH_COL = "_d2a_batch_id"

ProvenanceRole = Literal[
    "anchor_pk",
    "join_pk",
    "join_fk",
    "extract_batch",
    "derived_condition",
]


@dataclass(frozen=True)
class FieldExpr:
    table: str
    column: str
    join_fk: tuple[str, str] | None = None   # (外键所在表, 外键字段)
    value_map: dict[str, str] | None = None  # 源值 -> 对象值


@dataclass(frozen=True)
class ProvenanceProjection:
    """SelectPlan 中的源记录身份投影(不进入对象 output)。"""

    alias: str
    role: ProvenanceRole
    logical_table: str
    column: str
    sql_alias: str  # FROM 子句中的表别名:a / j1 / ...
    join_key: tuple[str, str] | None = None  # (目标逻辑表, 锚表外键列)
    property_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectPlan:
    """canonical SELECT 计划:字段表达式 + 可选 provenance 投影。"""

    sql: str
    params: list
    exprs: dict[str, FieldExpr]
    provenance: tuple[ProvenanceProjection, ...]
    anchor: str
    joins: tuple[tuple[tuple[str, str], str], ...]  # ((目标表, fk), sql_alias)

    def as_legacy_tuple(self) -> tuple[str, list, dict[str, FieldExpr]]:
        return self.sql, self.params, self.exprs

    def provenance_aliases(self) -> frozenset[str]:
        return frozenset(p.alias for p in self.provenance)


def parse_field_expr(raw: str) -> FieldExpr:
    m = _EXPR_RE.match(raw)
    if not m:
        raise ValueError(f"映射表达式不符合文法: '{raw}'(见 data2agent/mapping.py)")
    value_map = None
    if m["vmap"]:
        value_map = {}
        for entry in m["vmap"].split("/"):
            if "→" not in entry:
                raise ValueError(f"map 条目须为 源值→对象值: '{entry.strip()}'(表达式 '{raw}')")
            k, v = entry.split("→", 1)
            value_map[k.strip()] = v.strip()
    join_fk = (m["jt"], m["jc"]) if m["jt"] else None
    return FieldExpr(m["table"], m["column"], join_fk, value_map)


def _validate_ident(name: str, *, label: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法{label}标识符: {name!r}")
    return name


def split_row_provenance(
    row: dict,
    plan: SelectPlan,
) -> tuple[dict, dict[str, object]]:
    """将 SQL 行拆成 (转换用字段行, provenance 别名→值)。

    仅剥离 `__d2a_p_*` 内部投影;derived 条件列(`__col`)仍留给 mapping_transform。
    """
    strip = {
        p.alias for p in plan.provenance if p.alias.startswith(_PROV_PREFIX)
    }
    transform_row = {k: v for k, v in row.items() if k not in strip}
    prov = {k: row[k] for k in strip if k in row}
    return transform_row, prov


def build_select_plan(
    template: ObjectTemplate,
    binding: SourceBinding,
    *,
    filters: dict | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: Optional[int] = 20,
    physical: Callable[[str], str] | None = None,   # 逻辑表名 → 物理表名(落地库 raw_*)
    active_col: str | None = None,                  # 软删列:锚表过滤 + join 条件排除已删行
    extra_anchor_cols: list[str] | None = None,     # 额外锚表列(派生决策表用),别名 __列名
    anchor_pk_cols: list[str] | None = None,        # 内部:锚表主键列(DDL 序)
    anchor_pk_values: list[tuple] | None = None,    # 内部:冻结主键元组;与 cols 同用
    include_provenance: bool = False,
    table_pk_cols: Callable[[str], Sequence[str]] | None = None,
) -> SelectPlan:
    """按 binding 生成 SelectPlan。include_provenance 时附加源记录身份投影。

    Preview/MCP 应继续用 build_select(include_provenance=False 默认路径)。
    正式 apply 传 include_provenance=True 与 table_pk_cols。
    """
    physical = physical or (lambda t: t)
    if not binding.tables:
        raise ValueError(f"{template.object}: binding 未声明 tables,无法确定锚表")
    if include_provenance and table_pk_cols is None:
        raise ValueError("include_provenance=True 时必须提供 table_pk_cols")

    anchor = binding.tables[0]
    prop_names = {p.name for p in template.properties}
    exprs: dict[str, FieldExpr] = {}
    for prop, raw in binding.field_map.items():
        # 属性名进入 AS 别名:必须属于模板且为合法标识符,禁止客户端键注入表达式。
        if prop not in prop_names:
            raise ValueError(
                f"{template.object}: field_map 未知属性 '{prop}',"
                f"可用:{sorted(prop_names)}")
        prop = _validate_ident(prop, label="属性")
        if prop.startswith(_PROV_PREFIX):
            raise ValueError(
                f"{template.object}: 属性名不得使用 provenance 保留前缀 "
                f"{_PROV_PREFIX!r}: '{prop}'")
        exprs[prop] = parse_field_expr(raw)
    if not exprs:
        raise ValueError(f"{template.object}: binding({binding.source})未声明 field_map")

    if (anchor_pk_cols is None) ^ (anchor_pk_values is None):
        raise ValueError("anchor_pk_cols 与 anchor_pk_values 必须同时提供或同时省略")
    if anchor_pk_cols is not None and not anchor_pk_cols:
        raise ValueError("anchor_pk_cols 不能为空列表")

    joins: dict[tuple[str, str], str] = {}  # (目标表, 锚表外键) -> 别名

    def sql_col(e: FieldExpr) -> str:
        if e.join_fk:
            fk_table, fk_col = e.join_fk
            if fk_table != anchor:
                raise ValueError(f"join 外键须在锚表 {anchor} 上,got {fk_table}.{fk_col}")
            alias = joins.setdefault((e.table, fk_col), f"j{len(joins) + 1}")
            return f'{alias}."{e.column}"'
        if e.table != anchor:
            raise ValueError(f"非锚表字段 {e.table}.{e.column} 必须声明 (join {anchor}.外键)")
        return f'a."{e.column}"'

    select_parts = [f'{sql_col(e)} AS "{p}"' for p, e in exprs.items()]
    used_aliases: set[str] = set(exprs)

    provenance: list[ProvenanceProjection] = []

    # extra_anchor_cols 可能来自草稿 derived.when:必须先校验为合法标识符,
    # 禁止把客户端字符串直接拼进 a."…"(否则可改写 SELECT)。
    for col in extra_anchor_cols or []:
        col = _validate_ident(col, label="额外锚表列")
        alias = f"__{col}"
        if alias in used_aliases or alias.startswith(_PROV_PREFIX):
            raise ValueError(f"额外锚表列别名冲突: {alias!r}")
        select_parts.append(f'a."{col}" AS "{alias}"')
        used_aliases.add(alias)
        if include_provenance:
            provenance.append(
                ProvenanceProjection(
                    alias=alias,
                    role="derived_condition",
                    logical_table=anchor,
                    column=col,
                    sql_alias="a",
                )
            )

    if include_provenance:
        assert table_pk_cols is not None
        # 属性 → 所用 join_key,便于后续合并 FieldTrace
        props_by_join: dict[tuple[str, str], list[str]] = {}
        for prop, e in exprs.items():
            if e.join_fk is not None:
                props_by_join.setdefault((e.table, e.join_fk[1]), []).append(prop)

        anchor_pks = [
            _validate_ident(c, label="锚表主键列")
            for c in table_pk_cols(anchor)
        ]
        if not anchor_pks:
            raise ValueError(f"锚表 {anchor} 无 DDL 主键,无法投影 provenance")
        for i, col in enumerate(anchor_pks):
            alias = f"{_PROV_PREFIX}a_pk_{i}_{col}"
            if alias in used_aliases:
                raise ValueError(f"provenance 别名冲突: {alias!r}")
            select_parts.append(f'a."{col}" AS "{alias}"')
            used_aliases.add(alias)
            provenance.append(
                ProvenanceProjection(
                    alias=alias,
                    role="anchor_pk",
                    logical_table=anchor,
                    column=col,
                    sql_alias="a",
                )
            )
        batch_alias = f"{_PROV_PREFIX}a_batch"
        if batch_alias in used_aliases:
            raise ValueError(f"provenance 别名冲突: {batch_alias!r}")
        select_parts.append(f'a."{_BATCH_COL}" AS "{batch_alias}"')
        used_aliases.add(batch_alias)
        provenance.append(
            ProvenanceProjection(
                alias=batch_alias,
                role="extract_batch",
                logical_table=anchor,
                column=_BATCH_COL,
                sql_alias="a",
            )
        )

        for (target, fk), jalias in joins.items():
            fk = _validate_ident(fk, label="join 外键列")
            props = tuple(props_by_join.get((target, fk), ()))
            fk_alias = f"{_PROV_PREFIX}{jalias}_fk_{fk}"
            if fk_alias in used_aliases:
                raise ValueError(f"provenance 别名冲突: {fk_alias!r}")
            select_parts.append(f'a."{fk}" AS "{fk_alias}"')
            used_aliases.add(fk_alias)
            provenance.append(
                ProvenanceProjection(
                    alias=fk_alias,
                    role="join_fk",
                    logical_table=anchor,
                    column=fk,
                    sql_alias="a",
                    join_key=(target, fk),
                    property_names=props,
                )
            )
            join_pks = [
                _validate_ident(c, label="关联表主键列")
                for c in table_pk_cols(target)
            ]
            if not join_pks:
                raise ValueError(f"关联表 {target} 无 DDL 主键,无法投影 provenance")
            for i, col in enumerate(join_pks):
                pk_alias = f"{_PROV_PREFIX}{jalias}_pk_{i}_{col}"
                if pk_alias in used_aliases:
                    raise ValueError(f"provenance 别名冲突: {pk_alias!r}")
                select_parts.append(f'{jalias}."{col}" AS "{pk_alias}"')
                used_aliases.add(pk_alias)
                provenance.append(
                    ProvenanceProjection(
                        alias=pk_alias,
                        role="join_pk",
                        logical_table=target,
                        column=col,
                        sql_alias=jalias,
                        join_key=(target, fk),
                        property_names=props,
                    )
                )
            jbatch = f"{_PROV_PREFIX}{jalias}_batch"
            if jbatch in used_aliases:
                raise ValueError(f"provenance 别名冲突: {jbatch!r}")
            select_parts.append(f'{jalias}."{_BATCH_COL}" AS "{jbatch}"')
            used_aliases.add(jbatch)
            provenance.append(
                ProvenanceProjection(
                    alias=jbatch,
                    role="extract_batch",
                    logical_table=target,
                    column=_BATCH_COL,
                    sql_alias=jalias,
                    join_key=(target, fk),
                    property_names=props,
                )
            )

    select = ", ".join(select_parts)

    where, params = [], []
    for prop, val in (filters or {}).items():
        if prop not in exprs:
            raise ValueError(f"未知筛选字段 '{prop}',可用:{sorted(exprs)}")
        e = exprs[prop]
        if e.value_map:
            reverse = {v: k for k, v in e.value_map.items()}
            if val not in reverse:
                raise ValueError(f"'{prop}' 取值须为 {sorted(set(e.value_map.values()))},got '{val}'")
            val = reverse[val]
        where.append(f"{sql_col(e)} = ?")
        params.append(val)

    if anchor_pk_cols is not None and anchor_pk_values is not None:
        pk_cols = [_validate_ident(c, label="主键列") for c in anchor_pk_cols]
        n = len(pk_cols)
        for tup in anchor_pk_values:
            if len(tup) != n:
                raise ValueError(
                    f"主键元组长度须为 {n},got {len(tup)}: {tup!r}")
        if not anchor_pk_values:
            where.append("1 = 0")
        elif n == 1:
            placeholders = ", ".join("?" for _ in anchor_pk_values)
            where.append(f'a."{pk_cols[0]}" IN ({placeholders})')
            params.extend(t[0] for t in anchor_pk_values)
        else:
            cols_sql = ", ".join(f'a."{c}"' for c in pk_cols)
            row_ph = "(" + ", ".join("?" for _ in range(n)) + ")"
            placeholders = ", ".join(row_ph for _ in anchor_pk_values)
            where.append(f"({cols_sql}) IN ({placeholders})")
            for tup in anchor_pk_values:
                params.extend(tup)

    order = ""
    if order_by:
        if order_by not in exprs:
            raise ValueError(f"未知排序字段 '{order_by}',可用:{sorted(exprs)}")
        order = f' ORDER BY {sql_col(exprs[order_by])} {"DESC" if desc else "ASC"}'

    if active_col:
        where.append(f'a."{active_col}" IS NULL')
    join_active = f' AND {{alias}}."{active_col}" IS NULL' if active_col else ""
    from_clause = f'"{physical(anchor)}" a' + "".join(
        f' LEFT JOIN "{physical(t)}" {alias} ON {alias}."Id" = a."{fk}"'
        + join_active.format(alias=alias)
        for (t, fk), alias in joins.items()
    )
    where_clause = f" WHERE {' AND '.join(where)}" if where else ""
    limit_clause = "" if limit is None else f" LIMIT {max(1, min(int(limit), 200))}"
    sql = f"SELECT {select} FROM {from_clause}{where_clause}{order}{limit_clause}"
    return SelectPlan(
        sql=sql,
        params=params,
        exprs=exprs,
        provenance=tuple(provenance),
        anchor=anchor,
        joins=tuple(joins.items()),
    )


def build_select(
    template: ObjectTemplate,
    binding: SourceBinding,
    *,
    filters: dict | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: Optional[int] = 20,
    physical: Callable[[str], str] | None = None,
    active_col: str | None = None,
    extra_anchor_cols: list[str] | None = None,
    anchor_pk_cols: list[str] | None = None,
    anchor_pk_values: list[tuple] | None = None,
) -> tuple[str, list, dict[str, FieldExpr]]:
    """兼容包装:返回 (sql, params, 属性->表达式);等价于无 provenance 的 SelectPlan。"""
    plan = build_select_plan(
        template,
        binding,
        filters=filters,
        order_by=order_by,
        desc=desc,
        limit=limit,
        physical=physical,
        active_col=active_col,
        extra_anchor_cols=extra_anchor_cols,
        anchor_pk_cols=anchor_pk_cols,
        anchor_pk_values=anchor_pk_values,
        include_provenance=False,
    )
    return plan.as_legacy_tuple()
