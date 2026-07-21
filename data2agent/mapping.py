"""映射表达式与对象查询 SQL 构建:binding(field_map)→ 只读 SELECT。

表达式文法(SourceBinding.key_map / field_map 的值):

    表.字段
    表.字段 (join 锚表.外键字段)              # 外键在锚表上,目标表以 Id 为主键
    表.字段 (map 源值→对象值 / 源值→对象值)   # 源系统编码 → 对象模型取值

join 与 map 可同时出现,顺序固定为先 join 后 map;binding.tables[0] 为锚表
(对象一行 = 锚表一行)。本模块被 MCP query_objects 消费,后续抽取管道复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .metamodel.schema import ObjectTemplate, SourceBinding

_EXPR_RE = re.compile(
    r"""^\s*
    (?P<table>[A-Za-z_]\w*)\.(?P<column>[A-Za-z_]\w*)
    (?:\s*\(join\s+(?P<jt>[A-Za-z_]\w*)\.(?P<jc>[A-Za-z_]\w*)\s*\))?
    (?:\s*\(map\s+(?P<vmap>[^)]+)\))?
    \s*$""",
    re.X,
)


@dataclass(frozen=True)
class FieldExpr:
    table: str
    column: str
    join_fk: tuple[str, str] | None = None   # (外键所在表, 外键字段)
    value_map: dict[str, str] | None = None  # 源值 -> 对象值


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


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str, *, label: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法{label}标识符: {name!r}")
    return name


def build_select(
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
) -> tuple[str, list, dict[str, FieldExpr]]:
    """按 binding 生成参数化 SELECT。返回 (sql, params, 属性->表达式)。

    默认直读源表形(展厅/测试);映射应用传 physical + active_col
    在落地库 raw_* 上物化对象层。limit=None 不限行(仅限内部消费者)。

    anchor_pk_cols / anchor_pk_values 仅供 Preview 等内部消费者锁定冻结样本行;
    默认 unset 时行为与既有查询完全一致。
    """
    physical = physical or (lambda t: t)
    if not binding.tables:
        raise ValueError(f"{template.object}: binding 未声明 tables,无法确定锚表")
    anchor = binding.tables[0]
    exprs = {p: parse_field_expr(v) for p, v in binding.field_map.items()}
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

    select = ", ".join(f'{sql_col(e)} AS "{p}"' for p, e in exprs.items())
    for col in extra_anchor_cols or []:
        select += f', a."{col}" AS "__{col}"'

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
    return sql, params, exprs
