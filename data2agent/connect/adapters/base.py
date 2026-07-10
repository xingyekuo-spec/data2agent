"""适配器基类:白名单 / 仅 SELECT / 限流 / 审计四项安全强制所在层。

子类只实现三个钩子:_execute(执行 SQL 返回行)、table_info(表结构)、
_page_sql(分页语句)。所有数据读取都必须经 _audited_fetch,由基类完成
只读守卫、限流与审计;绕过它属于 bug。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

Column = tuple[str, str]  # (列名, 可移植类型 int/real/text/blob)

#: (action, sql, rows, duration_ms) -> None;由 sync 层接到落地库审计表
AuditHook = Callable[[str, str, int, float], None]


class ReadOnlyViolation(Exception):
    """适配器试图执行非只读语句 —— 安全承诺的硬失败。"""


class WhitelistViolation(Exception):
    """访问了白名单之外的表。"""


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[Column]
    pk: list[str]          # 源主键列(落地 upsert 的依据)


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
        """按主键排序的分页 SELECT(全量路径)。"""

    @abstractmethod
    def _increment_sql(self, table: TableInfo, watermark_col: str,
                       *, resume: bool, filtered: bool) -> str:
        """水位增量 SELECT(keyset,单列主键),占位符按 qmark:
        resume=False, filtered=False → 无 WHERE(首轮建立水位)
        resume=False, filtered=True  → WHERE wm >= ?
        resume=True                  → WHERE wm > ? OR (wm = ? AND pk > ?)
        均须 ORDER BY wm, pk 且限定 batch_size 行。"""

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

    def read_increment(self, table: TableInfo, since=None,
                       watermark_col: Optional[str] = None) -> Iterator[list[dict]]:
        """分批读取。watermark_col=None 为全量(主键分页);
        指定水位列则按 (水位, 主键) keyset 分页,since 为回看后的起点
        (由增量引擎计算,含边界;upsert 幂等所以重叠安全)。

        已知边界:水位列为 NULL 的行只有全量 / 首轮能带回,增量抓不到,
        兜底靠分段对账(E3)。
        """
        self._check_table(table.name)
        if watermark_col is None:
            yield from self._read_full(table)
            return
        if len(table.pk) != 1:
            raise NotImplementedError(
                f"{table.name}: keyset 增量暂只支持单列主键,got {table.pk}")
        pk = table.pk[0]
        cursor: tuple | None = None
        while True:
            if cursor is None:
                sql = self._increment_sql(table, watermark_col,
                                          resume=False, filtered=since is not None)
                params = (since,) if since is not None else ()
            else:
                sql = self._increment_sql(table, watermark_col, resume=True, filtered=False)
                params = (cursor[0], cursor[0], cursor[1])
            rows = self._audited_fetch(sql, params)
            if not rows:
                return
            yield rows
            if len(rows) < self.batch_size:
                return
            cursor = (rows[-1][watermark_col], rows[-1][pk])

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
