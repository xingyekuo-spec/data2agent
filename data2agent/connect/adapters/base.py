"""适配器基类:白名单 / 仅 SELECT / 限流 / 审计四项安全强制所在层。

子类只实现钩子:_execute、table_info、_page_sql、_quote、_limit_clause。
复合键增量 SQL 由基类统一生成。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Callable, Iterator, Optional

Column = tuple[str, str]  # (列名, 可移植类型 int/real/text/blob)

#: (action, sql, rows, duration_ms) -> None;由 sync 层接到落地库审计表
AuditHook = Callable[[str, str, int, float], None]


class ReadOnlyViolation(Exception):
    """适配器试图执行非只读语句 —— 安全承诺的硬失败。"""


class WhitelistViolation(Exception):
    """访问了白名单之外的表。"""


class RuntimeKeyError(ValueError):
    """配置运行键无效:缺列、空键或存在 NULL。"""


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[Column]
    pk: list[str]          # 运行键(落地 upsert / 增量 keyset 依据)
    key_source: str = "database_pk"  # database_pk | configured


def encode_keyset_cursor(watermark, key_values: list | None) -> str:
    """统一序列化增量游标。

    key_values=None 表示该水位上整表已完成;list 表示已完成到该键边界(含)。
    """
    return json.dumps({"w": watermark, "k": key_values}, ensure_ascii=False, default=str)


def decode_keyset_cursor(raw: str) -> tuple[object, list | None]:
    """解码游标。兼容旧版纯水位字符串(视为已完成)。"""
    text = raw.strip()
    if text.startswith("{"):
        data = json.loads(text)
        return data["w"], data.get("k")
    return text, None


def apply_configured_keys(info: TableInfo, key_columns: list[str]) -> TableInfo:
    """用配置业务键覆盖数据库主键;校验列存在且不重复。"""
    if not key_columns:
        raise RuntimeKeyError(f"{info.name}: key_columns 不能为空")
    if len(key_columns) != len(set(key_columns)):
        raise RuntimeKeyError(f"{info.name}: key_columns 不得包含重复列")
    known = {c for c, _ in info.columns}
    missing = [c for c in key_columns if c not in known]
    if missing:
        raise RuntimeKeyError(
            f"{info.name}: 配置键列不存在: {', '.join(missing)}")
    return replace(info, pk=list(key_columns), key_source="configured")


def resolve_runtime_keys(
    info: TableInfo,
    key_columns: list[str] | None,
    *,
    require_keys: bool = True,
) -> TableInfo:
    """解析运行键:配置优先,否则使用数据库 PK。"""
    if key_columns:
        return apply_configured_keys(info, key_columns)
    if require_keys and not info.pk:
        raise RuntimeKeyError(
            f"{info.name}: 无数据库主键且未配置 key_columns,无法幂等落地/增量")
    return replace(info, key_source="database_pk")


_ALLOWED_PREFIXES = ("SELECT", "PRAGMA TABLE_INFO")  # PRAGMA 仅用于 SQLite 读表结构


class SourceAdapter(ABC):
    def __init__(self, whitelist: set[str], *, batch_size: int = 5000,
                 rows_per_second: int = 0, audit_hook: AuditHook | None = None):
        if not whitelist:
            raise ValueError("白名单为空:适配器拒绝在无白名单下工作")
        self.whitelist = set(whitelist)
        self.batch_size = max(1, int(batch_size))
        self.rows_per_second = max(0, int(rows_per_second))
        self.audit_hook = audit_hook
        self._last_read_at = 0.0

    # ---- 子类钩子 ----

    @abstractmethod
    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行(已通过守卫的)SQL,返回 dict 行。"""

    @abstractmethod
    def table_info(self, name: str) -> TableInfo:
        """读取白名单内一张表的结构(实现内须先调 _check_table)。"""

    @abstractmethod
    def _page_sql(self, table: TableInfo, limit: int, offset: int) -> str:
        """按运行键排序的分页 SELECT(全量路径)。"""

    @abstractmethod
    def _quote(self, ident: str) -> str:
        """标识符引用(sqlite: "x" / mssql: [x])。"""

    @abstractmethod
    def _limit_clause(self, limit: int) -> str:
        """返回限制行数的 SQL 片段(含前导空格),如 ' TOP 100' 或 ' LIMIT 100'。"""

    # ---- 安全强制 ----

    def _check_table(self, name: str) -> None:
        if name not in self.whitelist:
            raise WhitelistViolation(f"表 '{name}' 不在白名单内:{sorted(self.whitelist)}")

    def _guard_select(self, sql: str) -> None:
        head = sql.lstrip().upper()
        if not head.startswith(_ALLOWED_PREFIXES) or ";" in sql.rstrip().rstrip(";"):
            raise ReadOnlyViolation(f"适配器只允许单条 SELECT,拒绝执行: {sql[:80]!r}")

    def _throttle(self, rows: int) -> None:
        if not self.rows_per_second:
            return
        min_interval = rows / self.rows_per_second
        elapsed = time.monotonic() - self._last_read_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_read_at = time.monotonic()

    def _audited_fetch(self, sql: str, params: tuple = (), action: str = "read") -> list[dict]:
        self._guard_select(sql)
        t0 = time.monotonic()
        rows = self._execute(sql, params)
        if self.audit_hook:
            self.audit_hook(action, sql, len(rows), (time.monotonic() - t0) * 1000)
        self._throttle(len(rows))
        return rows

    # ---- 对外接口 ----

    def tables(self) -> list[TableInfo]:
        return [self.table_info(t) for t in sorted(self.whitelist)]

    def validate_runtime_keys(self, table: TableInfo) -> None:
        """同步前校验运行键:列存在、无 NULL、组合唯一。"""
        if not table.pk:
            raise RuntimeKeyError(f"{table.name}: 运行键为空")
        known = {c for c, _ in table.columns}
        missing = [c for c in table.pk if c not in known]
        if missing:
            raise RuntimeKeyError(
                f"{table.name}: 运行键列不存在: {', '.join(missing)}")
        null_pred = " OR ".join(f"{self._quote(c)} IS NULL" for c in table.pk)
        sql = (f"SELECT COUNT(*) AS c FROM {self._quote(table.name)} "
               f"WHERE {null_pred}")
        (row,) = self._audited_fetch(sql, action="validate_keys")
        if int(row["c"]) > 0:
            raise RuntimeKeyError(
                f"{table.name}: 运行键存在 NULL 值({row['c']} 行),"
                f"键来源={table.key_source}")
        key_sql = ", ".join(self._quote(c) for c in table.pk)
        dup_sql = (
            f"SELECT COUNT(*) AS c FROM ("
            f"SELECT {key_sql} FROM {self._quote(table.name)} "
            f"GROUP BY {key_sql} HAVING COUNT(*) > 1)"
        )
        (dup,) = self._audited_fetch(dup_sql, action="validate_keys")
        if int(dup["c"]) > 0:
            raise RuntimeKeyError(
                f"{table.name}: 运行键不唯一({dup['c']} 组重复),"
                f"键来源={table.key_source}")

    def read_increment(self, table: TableInfo, since=None,
                       watermark_col: Optional[str] = None,
                       resume_after: tuple | None = None) -> Iterator[list[dict]]:
        """分批读取。watermark_col=None 为全量(运行键分页);
        指定水位列则按 (水位, 运行键...) keyset 分页。
        resume_after=(wm, *key_values):从该稳定边界之后续传。
        """
        self._check_table(table.name)
        if watermark_col is None:
            yield from self._read_full(table)
            return
        yield from self._keyset_read(
            table, watermark_col, since, None, resume_after=resume_after)

    def read_segment(self, table: TableInfo, watermark_col: str,
                     start, end) -> Iterator[list[dict]]:
        """有界段读取 [start, end),对账 L2 重抽用。"""
        self._check_table(table.name)
        yield from self._keyset_read(table, watermark_col, start, end)

    def segment_stats(self, table: TableInfo, watermark_col: str, start, end) -> dict:
        """L1 对账:段内 COUNT + MAX(水位)。"""
        self._check_table(table.name)
        wm = self._quote(watermark_col)
        sql = (f"SELECT COUNT(*) AS c, MAX({wm}) AS m "
               f"FROM {self._quote(table.name)} WHERE {wm} >= ? AND {wm} < ?")
        (row,) = self._audited_fetch(sql, (start, end), action="reconcile")
        return {"count": row["c"], "max": row["m"]}

    def table_count(self, table: TableInfo) -> int:
        """整表行数(无水位表的 L1 对账)。"""
        self._check_table(table.name)
        sql = f"SELECT COUNT(*) AS c FROM {self._quote(table.name)}"
        (row,) = self._audited_fetch(sql, action="reconcile")
        return row["c"]

    def _increment_sql(self, table: TableInfo, watermark_col: str,
                       *, resume: bool, filtered: bool, bounded: bool = False) -> str:
        """水位增量 SELECT(keyset,支持复合运行键),占位符按 qmark。

        resume=False, filtered=False → 无 WHERE(首轮建立水位)
        resume=False, filtered=True  → WHERE wm >= ?
        resume=True                  → 元组序续传 (wm, k1, k2, ...)
        bounded=True 追加 AND wm < ?
        均须 ORDER BY wm, k1, ... 且限定 batch_size 行。
        """
        if not table.pk:
            raise RuntimeKeyError(f"{table.name}: 增量需要运行键")
        cols = ", ".join(self._quote(c) for c, _ in table.columns)
        wm = self._quote(watermark_col)
        key_qs = [self._quote(k) for k in table.pk]
        order = ", ".join([wm, *key_qs])
        conds: list[str] = []
        if resume:
            conds.append(self._keyset_resume_predicate(wm, key_qs))
        elif filtered:
            conds.append(f"{wm} >= ?")
        if bounded:
            conds.append(f"{wm} < ?")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        # MSSQL: TOP 在 SELECT 后; SQLite: LIMIT 在末尾
        limit = self._limit_clause(self.batch_size)
        if limit.lstrip().upper().startswith("TOP"):
            return (f"SELECT{limit} {cols} FROM {self._quote(table.name)}"
                    f"{where} ORDER BY {order}")
        return (f"SELECT {cols} FROM {self._quote(table.name)}{where} "
                f"ORDER BY {order}{limit}")

    def _keyset_resume_predicate(self, wm_q: str, key_qs: list[str]) -> str:
        """(wm, k1, ..., kn) > (?,?,...) 的 SQL 展开。"""
        parts = [f"{wm_q} > ?"]
        # (wm = ? AND k1 > ?) OR (wm = ? AND k1 = ? AND k2 > ?) OR ...
        for i, key_q in enumerate(key_qs):
            eqs = [f"{wm_q} = ?"] + [f"{key_qs[j]} = ?" for j in range(i)]
            parts.append("(" + " AND ".join(eqs + [f"{key_q} > ?"]) + ")")
        return "(" + " OR ".join(parts) + ")"

    def _keyset_resume_params(self, watermark, key_values: list) -> tuple:
        """与 _keyset_resume_predicate 对应的参数序列。"""
        params: list = [watermark]
        for i in range(len(key_values)):
            params.append(watermark)
            params.extend(key_values[:i])
            params.append(key_values[i])
        return tuple(params)

    def _keyset_read(self, table: TableInfo, watermark_col: str,
                     since, until, *, resume_after: tuple | None = None) -> Iterator[list[dict]]:
        if not table.pk:
            raise RuntimeKeyError(
                f"{table.name}: keyset 增量需要运行键,got {table.pk}")
        bounded = until is not None
        # resume_after=(wm, *keys) 表示已完成到该边界,从其后继续
        cursor: tuple | None = resume_after
        while True:
            if cursor is None:
                sql = self._increment_sql(table, watermark_col, resume=False,
                                          filtered=since is not None, bounded=bounded)
                params = tuple(p for p in (since, until) if p is not None)
            else:
                sql = self._increment_sql(table, watermark_col, resume=True,
                                          filtered=False, bounded=bounded)
                params = self._keyset_resume_params(cursor[0], list(cursor[1:]))
                if bounded:
                    params = params + (until,)
            rows = self._audited_fetch(sql, params)
            if not rows:
                return
            for row in rows:
                if any(row.get(k) is None for k in table.pk):
                    raise RuntimeKeyError(
                        f"{table.name}: 增量批次中运行键含 NULL,"
                        f"键来源={table.key_source}")
                if row.get(watermark_col) is None:
                    raise RuntimeKeyError(
                        f"{table.name}: 增量批次中水位列 '{watermark_col}' 为 NULL")
            yield rows
            if len(rows) < self.batch_size:
                return
            last = rows[-1]
            cursor = (last[watermark_col], *[last[k] for k in table.pk])

    def _read_full(self, table: TableInfo) -> Iterator[list[dict]]:
        offset = 0
        while True:
            rows = self._audited_fetch(self._page_sql(table, self.batch_size, offset))
            if not rows:
                return
            yield rows
            if len(rows) < self.batch_size:
                return
            offset += len(rows)
