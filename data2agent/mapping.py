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


def build_select(
    template: ObjectTemplate,
    binding: SourceBinding,
    *,
    filters: dict | None = None,
    order_by: str | None = None,
    desc: bool = False,
    limit: int = 20,
) -> tuple[str, list, dict[str, FieldExpr]]:
    """按 binding 生成参数化 SELECT。返回 (sql, params, 属性->表达式)。"""
    if not binding.tables:
        raise ValueError(f"{template.object}: binding 未声明 tables,无法确定锚表")
    anchor = binding.tables[0]
    exprs = {p: parse_field_expr(v) for p, v in binding.field_map.items()}
    if not exprs:
        raise ValueError(f"{template.object}: binding({binding.source})未声明 field_map")

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

    order = ""
    if order_by:
        if order_by not in exprs:
            raise ValueError(f"未知排序字段 '{order_by}',可用:{sorted(exprs)}")
        order = f' ORDER BY {sql_col(exprs[order_by])} {"DESC" if desc else "ASC"}'

    from_clause = f'"{anchor}" a' + "".join(
        f' LEFT JOIN "{t}" {alias} ON {alias}."Id" = a."{fk}"'
        for (t, fk), alias in joins.items()
    )
    where_clause = f" WHERE {' AND '.join(where)}" if where else ""
    limit = max(1, min(int(limit), 200))
    sql = f"SELECT {select} FROM {from_clause}{where_clause}{order} LIMIT {limit}"
    return sql, params, exprs
