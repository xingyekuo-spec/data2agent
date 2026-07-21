"""数据集发布域服务:已发布快照解析(M2-T03);构建/发布/回滚后续任务扩展。

调用约定(T08/T09):resolve_published_snapshot 与后续对象/指标查询必须位于同一
SQLite 读事务(BEGIN … COMMIT)内,避免并发发布时看到混版。本模块不自动开事务,
由调用方持有连接并包裹。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from data2agent.connect.landing import LandingStore
from data2agent.metamodel.dataset_publish_contract import validate_build_table
from data2agent.metamodel.schema import TemplatePack
from data2agent.metamodel.versioning import (
    object_layer_fully_published,
    parse_object_manifest,
)

SnapshotReason = Literal["not_published", "snapshot_corrupt"]


class PublishedSnapshotError(Exception):
    """公开安全错误;detail 不含表名/SQL/内部异常。"""

    def __init__(self, reason_code: SnapshotReason, detail: str = "数据集快照不可用"):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail)


class PublishedObjectEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: str
    object_version: str
    binding_hash: str
    row_count: int = Field(ge=0)
    physical_table: str


class PublishedDatasetSnapshot(BaseModel):
    """同一读事务内解析出的已发布数据集快照。"""

    model_config = ConfigDict(extra="forbid")

    source: str
    dataset_version: str
    template_version: str
    template_pack: TemplatePack
    objects: dict[str, PublishedObjectEntry]


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_exists(store: LandingStore, table: str) -> bool:
    row = store.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _count_rows(store: LandingStore, table: str) -> int:
    (n,) = store.con.execute(
        f"SELECT COUNT(*) FROM {_quote_ident(table)}"
    ).fetchone()
    return int(n)


def _parse_template_snapshot(
    raw: str | None, *, expected_version: str, manifest: list[str],
) -> TemplatePack:
    if not raw or not isinstance(raw, str) or not raw.strip():
        raise PublishedSnapshotError("snapshot_corrupt")
    try:
        pack = TemplatePack.model_validate_json(raw)
    except (ValidationError, ValueError, TypeError):
        raise PublishedSnapshotError("snapshot_corrupt") from None
    if pack.cross_validate():
        raise PublishedSnapshotError("snapshot_corrupt")
    if pack.version != expected_version:
        raise PublishedSnapshotError("snapshot_corrupt")
    if not set(manifest).issubset(pack.object_names()):
        raise PublishedSnapshotError("snapshot_corrupt")
    return pack


def resolve_published_snapshot(
    store: LandingStore, source: str,
) -> PublishedDatasetSnapshot:
    """严格解析 source 的当前 published 快照;失败不回退遗留 obj_*。"""
    ds = store.get_published_dataset(source)
    if ds is None or ds.status != "published" or ds.source != source:
        raise PublishedSnapshotError("not_published")

    manifest = parse_object_manifest(ds.object_manifest)
    if not manifest:
        raise PublishedSnapshotError("snapshot_corrupt")

    obj_rows = store.list_object_versions(ds.dataset_version)
    if not object_layer_fully_published(ds, obj_rows):
        raise PublishedSnapshotError("snapshot_corrupt")

    by_name = {o.object: o for o in obj_rows}
    entries: dict[str, PublishedObjectEntry] = {}
    for name in manifest:
        row = by_name[name]
        if row.purged_at is not None or not row.build_table:
            raise PublishedSnapshotError("snapshot_corrupt")
        try:
            table = validate_build_table(row.build_table)
        except ValueError:
            raise PublishedSnapshotError("snapshot_corrupt") from None
        if not _table_exists(store, table):
            raise PublishedSnapshotError("snapshot_corrupt")
        actual = _count_rows(store, table)
        if actual != row.row_count:
            raise PublishedSnapshotError("snapshot_corrupt")
        entries[name] = PublishedObjectEntry(
            object=name,
            object_version=row.object_version,
            binding_hash=row.binding_hash,
            row_count=row.row_count,
            physical_table=table,
        )

    pack = _parse_template_snapshot(
        ds.template_snapshot,
        expected_version=ds.template_version,
        manifest=manifest,
    )
    return PublishedDatasetSnapshot(
        source=ds.source,
        dataset_version=ds.dataset_version,
        template_version=ds.template_version,
        template_pack=pack,
        objects=entries,
    )
