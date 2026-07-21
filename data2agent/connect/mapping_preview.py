"""映射 Preview 只读样本冻结(M3-T03)。

在显式 SQLite 读事务内按锚表 DDL 主键稳定排序、过滤软删/批次、冻结主键集合,
并计算 sample_fingerprint。current/candidate 双跑共用同一 FrozenSample。

本模块暂不实现草稿预检、聚合、diff 与脱敏(T04+)。
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from ..mapping import build_select
from ..metamodel.schema import ObjectTemplate, SourceBinding
from .landing import LandingStore, raw_table_name

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAMPLE_LIMIT_MIN, _SAMPLE_LIMIT_MAX = 1, 200
_SAMPLE_OFFSET_MIN, _SAMPLE_OFFSET_MAX = 0, 10000
_ACTIVE_COL = "_d2a_deleted_at"
_BATCH_COL = "_d2a_batch_id"
_ROW_HASH_COL = "_d2a_row_hash"

PreviewSampleReason = str  # sample_batch_not_found | sample_invalid | raw_table_not_found


class PreviewSampleError(Exception):
    """样本冻结失败;reason_code 供后续 API 映射 HTTP 状态。"""

    def __init__(self, reason_code: PreviewSampleReason, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class FrozenSample:
    anchor_table: str
    pk_cols: tuple[str, ...]
    pk_tuples: tuple[tuple, ...]
    sample_batch_ids: tuple[str, ...]
    fingerprint: str
    sampled_rows: int
    offset: int
    limit: int
    requested_batch_id: str | None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _require_ident(name: str, *, label: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise PreviewSampleError("sample_invalid", f"非法{label}")
    return name


def _validate_bounds(offset: int, limit: int) -> tuple[int, int]:
    if not isinstance(offset, int) or not isinstance(limit, int):
        raise PreviewSampleError("sample_invalid", "offset/limit 须为整数")
    if offset < _SAMPLE_OFFSET_MIN or offset > _SAMPLE_OFFSET_MAX:
        raise PreviewSampleError(
            "sample_invalid",
            f"offset 须在 {_SAMPLE_OFFSET_MIN}..{_SAMPLE_OFFSET_MAX}",
        )
    if limit < _SAMPLE_LIMIT_MIN or limit > _SAMPLE_LIMIT_MAX:
        raise PreviewSampleError(
            "sample_invalid",
            f"limit 须在 {_SAMPLE_LIMIT_MIN}..{_SAMPLE_LIMIT_MAX}",
        )
    return offset, limit


@contextmanager
def preview_read_tx(store: LandingStore) -> Iterator[LandingStore]:
    """显式只读事务;可重入。Preview 样本冻结与双跑须包裹在此内。"""
    depth = getattr(store, "_preview_read_depth", 0)
    if depth > 0:
        yield store
        return
    isolation = store.con.isolation_level
    store.con.isolation_level = None
    store._preview_read_depth = 1
    try:
        store.con.execute("BEGIN")
        try:
            yield store
            store.con.execute("COMMIT")
        except Exception:
            try:
                store.con.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        store._preview_read_depth = 0
        store.con.isolation_level = isolation


def _table_exists(store: LandingStore, physical: str) -> bool:
    row = store.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (physical,),
    ).fetchone()
    return row is not None


def discover_pk_columns(store: LandingStore, physical: str) -> list[str]:
    """DDL 主键列,按 PRAGMA table_info.pk 位序。无主键返回空列表。"""
    rows = store.con.execute(f"PRAGMA table_info({_quote(physical)})").fetchall()
    return [
        r["name"]
        for r in sorted((r for r in rows if r["pk"] > 0), key=lambda r: r["pk"])
    ]


def _canonical_fingerprint(
    *,
    source: str,
    anchor: str,
    pk_cols: list[str],
    rows: list[tuple[tuple, str | None, str | None]],
) -> str:
    """规范化 SHA-256:source + anchor + 主键元组 + row_hash + batch_id。"""
    payload = {
        "source": source,
        "anchor": anchor,
        "pk_cols": pk_cols,
        "rows": [
            {
                "pk": list(pk),
                "row_hash": row_hash,
                "batch_id": batch_id,
            }
            for pk, row_hash, batch_id in rows
        ],
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_sample_row_id(ordinal: int, pk_tuple: tuple) -> str:
    """样本内公开行 ID:ordinal + 主键摘要;不直接暴露业务键。"""
    digest = hashlib.sha256(
        json.dumps(list(pk_tuple), ensure_ascii=False, separators=(",", ":"), default=str)
        .encode("utf-8")
    ).hexdigest()[:12]
    return f"{ordinal}:{digest}"


def freeze_sample(
    store: LandingStore,
    *,
    source: str,
    anchor_table: str,
    offset: int = 0,
    limit: int = 50,
    batch_id: str | None = None,
) -> FrozenSample:
    """在当前读事务内冻结锚表样本主键集合与 fingerprint。"""
    source = _require_ident(source, label="source")
    anchor_table = _require_ident(anchor_table, label="锚表")
    offset, limit = _validate_bounds(offset, limit)
    if batch_id is not None and (not isinstance(batch_id, str) or not batch_id):
        raise PreviewSampleError("sample_invalid", "batch_id 不能为空字符串")

    physical = raw_table_name(source, anchor_table)
    if not _table_exists(store, physical):
        raise PreviewSampleError("raw_table_not_found", "锚表尚未落地")

    pk_cols = discover_pk_columns(store, physical)
    if not pk_cols:
        raise PreviewSampleError("sample_invalid", "锚表无主键,无法稳定抽样")
    for col in pk_cols:
        _require_ident(col, label="主键列")

    quoted = _quote(physical)
    pk_sql = ", ".join(_quote(c) for c in pk_cols)
    meta_sql = f"{_quote(_ROW_HASH_COL)}, {_quote(_BATCH_COL)}"

    if batch_id is not None:
        exists = store.con.execute(
            f"SELECT 1 FROM {quoted} WHERE {_quote(_BATCH_COL)} = ? LIMIT 1",
            (batch_id,),
        ).fetchone()
        if exists is None:
            raise PreviewSampleError("sample_batch_not_found", "指定批次不存在")

    where = [f"{_quote(_ACTIVE_COL)} IS NULL"]
    params: list = []
    if batch_id is not None:
        where.append(f"{_quote(_BATCH_COL)} = ?")
        params.append(batch_id)
    where_sql = " AND ".join(where)

    sql = (
        f"SELECT {pk_sql}, {meta_sql} FROM {quoted} "
        f"WHERE {where_sql} "
        f"ORDER BY {pk_sql} "
        f"LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    fetched = store.con.execute(sql, params).fetchall()

    pk_tuples: list[tuple] = []
    fingerprint_rows: list[tuple[tuple, str | None, str | None]] = []
    batch_ids: set[str] = set()
    for row in fetched:
        pk = tuple(row[c] for c in pk_cols)
        row_hash = row[_ROW_HASH_COL]
        row_batch = row[_BATCH_COL]
        pk_tuples.append(pk)
        fingerprint_rows.append((pk, row_hash, row_batch))
        if row_batch is not None:
            batch_ids.add(str(row_batch))

    fingerprint = _canonical_fingerprint(
        source=source,
        anchor=anchor_table,
        pk_cols=pk_cols,
        rows=fingerprint_rows,
    )
    return FrozenSample(
        anchor_table=anchor_table,
        pk_cols=tuple(pk_cols),
        pk_tuples=tuple(pk_tuples),
        sample_batch_ids=tuple(sorted(batch_ids)),
        fingerprint=fingerprint,
        sampled_rows=len(pk_tuples),
        offset=offset,
        limit=limit,
        requested_batch_id=batch_id,
    )


def load_sample_rows(
    store: LandingStore,
    template: ObjectTemplate,
    binding: SourceBinding,
    sample: FrozenSample,
    *,
    source: str,
    extra_anchor_cols: list[str] | None = None,
) -> list[dict]:
    """用冻结主键集合经 build_select 读取一行集;结果按冻结主键序排列。"""
    if not binding.tables or binding.tables[0] != sample.anchor_table:
        raise PreviewSampleError("sample_invalid", "binding 锚表与冻结样本不一致")
    if not sample.pk_tuples:
        return []

    # 附带锚主键列(别名 __col)以便按冻结序重排;不进入对外属性输出。
    pk_extras = list(dict.fromkeys([*(extra_anchor_cols or []), *sample.pk_cols]))
    sql, params, _ = build_select(
        template,
        binding,
        limit=None,
        physical=lambda t: raw_table_name(source, t),
        active_col=_ACTIVE_COL,
        extra_anchor_cols=pk_extras,
        anchor_pk_cols=list(sample.pk_cols),
        anchor_pk_values=list(sample.pk_tuples),
    )
    keyed = [dict(r) for r in store.con.execute(sql, params)]
    by_pk: dict[tuple, dict] = {}
    for row in keyed:
        pk = tuple(row.get(f"__{c}") for c in sample.pk_cols)
        cleaned = {k: v for k, v in row.items() if not k.startswith("__")}
        by_pk[pk] = cleaned

    return [by_pk[pk] for pk in sample.pk_tuples if pk in by_pk]
