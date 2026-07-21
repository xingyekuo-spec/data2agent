"""数据集发布域服务:快照解析(T03)与候选构建(T05);发布/回滚见 T06。

调用约定(T08/T09):resolve_published_snapshot 与后续对象/指标查询必须位于同一
SQLite 读事务(BEGIN … COMMIT)内,避免并发发布时看到混版。本模块不自动开事务,
由调用方持有连接并包裹。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from data2agent.connect.landing import LandingStore, _now
from data2agent.connect.mapping_apply import (
    DEFAULT_BREAKER_THRESHOLD,
    MappingCircuitBreaker,
    ObjectApplyResult,
    apply_object,
)
from data2agent.metamodel.dataset_publish_contract import (
    is_dataset_ready,
    is_valid_build_table,
    make_build_table,
    validate_build_table,
)
from data2agent.metamodel.schema import ObjectTemplate, SourceBinding, TemplatePack
from data2agent.metamodel.versioning import (
    DatasetVersionRecord,
    ObjectVersionRecord,
    binding_hash,
    object_layer_fully_published,
    parse_object_manifest,
)

SnapshotReason = Literal["not_published", "snapshot_corrupt"]
BuildOutcome = Literal["ok", "conflict", "failed"]


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


@dataclass
class BuildDatasetResult:
    source: str
    dataset_version: str | None
    previous_dataset_version: str | None
    status: Literal["building", "failed"] | None
    ready: bool
    published: bool
    results: list[ObjectApplyResult] = field(default_factory=list)
    outcome: BuildOutcome = "failed"
    reason_code: str | None = None
    error: str | None = None
    error_id: str | None = None


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


def _safe_error(detail: str) -> tuple[str, str]:
    error_id = hashlib.sha256(detail.encode("utf-8")).hexdigest()[:12]
    return "数据集构建失败", error_id


def _drop_table_best_effort(store: LandingStore, table: str | None) -> None:
    if not table or not is_valid_build_table(table):
        return
    try:
        store.con.execute(f"DROP TABLE IF EXISTS {_quote_ident(table)}")
        store.con.commit()
    except Exception:
        try:
            store.con.rollback()
        except Exception:
            pass


def _new_dataset_version() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"ds_{stamp}_{uuid.uuid4().hex[:8]}"


def enabled_object_bindings(
    pack: TemplatePack, source: str,
) -> list[tuple[ObjectTemplate, SourceBinding]]:
    """按模板包稳定顺序返回 source 下 enabled 的对象绑定。"""
    out: list[tuple[ObjectTemplate, SourceBinding]] = []
    for tpl in pack.objects:
        binding = next(
            (b for b in tpl.bindings if b.source == source and b.enabled),
            None,
        )
        if binding is None:
            continue
        out.append((tpl, binding))
    return out


def _protected_build_tables(store: LandingStore, source: str) -> set[str]:
    protected: set[str] = set()
    published = store.get_published_dataset(source)
    if published is None:
        return protected
    for obj in store.list_object_versions(published.dataset_version):
        if obj.build_table:
            protected.add(obj.build_table)
    if published.previous_dataset_version:
        for obj in store.list_object_versions(published.previous_dataset_version):
            if obj.build_table:
                protected.add(obj.build_table)
    return protected


def _has_active_build_run(store: LandingStore, dataset_version: str) -> bool:
    row = store.con.execute(
        "SELECT 1 FROM d2a_sync_run "
        "WHERE dataset_version = ? AND status = 'running' LIMIT 1",
        (dataset_version,),
    ).fetchone()
    return row is not None


def _recover_stale_building(store: LandingStore, source: str) -> str | None:
    """关闭同 source 陈旧 building;若仍有运行中任务则返回 conflict reason。"""
    protected = _protected_build_tables(store, source)
    building_rows, _ = store.list_dataset_versions(
        source=source, status="building", limit=50, offset=0,
    )
    for ds in building_rows:
        if _has_active_build_run(store, ds.dataset_version):
            return "active_build"
        for obj in store.list_object_versions(ds.dataset_version):
            table = obj.build_table
            if obj.status == "building":
                try:
                    store.update_object_build_result(
                        ds.dataset_version,
                        obj.object,
                        status="failed",
                        row_count=0,
                        build_table=None,
                    )
                except ValueError:
                    store.update_object_lifecycle(
                        ds.dataset_version, obj.object, status="failed",
                    )
            if table and table not in protected:
                _drop_table_best_effort(store, table)
        store.update_dataset_lifecycle(
            ds.dataset_version,
            status="failed",
            error="interrupted build recovered",
        )
    return None


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


def build_dataset(
    store: LandingStore,
    pack: TemplatePack,
    source: str,
    *,
    auto_publish: bool = False,
    threshold: float = DEFAULT_BREAKER_THRESHOLD,
) -> BuildDatasetResult:
    """构建完整候选数据集;成功后保持 building+ready,auto_publish 由 T06 落地。"""
    members = enabled_object_bindings(pack, source)
    if not members:
        return BuildDatasetResult(
            source=source,
            dataset_version=None,
            previous_dataset_version=None,
            status=None,
            ready=False,
            published=False,
            outcome="conflict",
            reason_code="empty_manifest",
            error="无可用对象绑定",
        )
    for tpl, binding in members:
        if not binding.field_map:
            return BuildDatasetResult(
                source=source,
                dataset_version=None,
                previous_dataset_version=None,
                status=None,
                ready=False,
                published=False,
                outcome="conflict",
                reason_code="empty_field_map",
                error=f"{tpl.object}: 启用 binding 缺少 field_map",
            )

    conflict = _recover_stale_building(store, source)
    if conflict:
        return BuildDatasetResult(
            source=source,
            dataset_version=None,
            previous_dataset_version=None,
            status=None,
            ready=False,
            published=False,
            outcome="conflict",
            reason_code=conflict,
            error="已有运行中的数据集构建",
        )

    published = store.get_published_dataset(source)
    previous = published.dataset_version if published else None
    dataset_version = _new_dataset_version()
    manifest = [tpl.object for tpl, _ in members]
    now = _now()
    store.insert_dataset_version(
        DatasetVersionRecord(
            dataset_version=dataset_version,
            source=source,
            template_version=pack.version,
            status="building",
            built_at=now,
            previous_dataset_version=previous,
            object_manifest=json.dumps(manifest, ensure_ascii=False),
            template_snapshot=pack.model_dump_json(),
        )
    )

    run_id = store.start_run(source, "apply")
    store.set_run_dataset_version(run_id, dataset_version)

    results: list[ObjectApplyResult] = []
    any_failed = False
    internal_errors: list[str] = []
    built_tables: list[str] = []

    for tpl, binding in members:
        object_version = f"ov_{uuid.uuid4().hex[:16]}"
        store.insert_object_version(
            ObjectVersionRecord(
                dataset_version=dataset_version,
                object=tpl.object,
                object_version=object_version,
                binding_hash=binding_hash(binding),
                row_count=0,
                build_table=None,
                status="building",
                built_at=now,
            )
        )
        table = make_build_table(source, tpl.object, uuid.uuid4().hex[:12])
        try:
            result = apply_object(
                store, tpl, source, build_table=table, threshold=threshold,
            )
            store.update_object_build_result(
                dataset_version,
                tpl.object,
                status="built",
                row_count=result.mapped,
                build_table=result.build_table,
                batch_id=result.batch_id,
            )
            if result.build_table:
                built_tables.append(result.build_table)
            results.append(result)
        except MappingCircuitBreaker as e:
            any_failed = True
            internal_errors.append(str(e))
            store.update_object_build_result(
                dataset_version,
                tpl.object,
                status="failed",
                row_count=0,
                build_table=None,
                batch_id=e.batch_id,
            )
            _drop_table_best_effort(store, table)
            results.append(ObjectApplyResult(
                tpl.object, e.total, e.mapped, e.quarantined,
                status="aborted", batch_id=e.batch_id,
            ))
        except Exception as e:
            any_failed = True
            internal_errors.append(f"{tpl.object}: {e}")
            try:
                store.update_object_build_result(
                    dataset_version,
                    tpl.object,
                    status="failed",
                    row_count=0,
                    build_table=None,
                )
            except Exception:
                pass
            _drop_table_best_effort(store, table)
            results.append(ObjectApplyResult(
                tpl.object, 0, 0, 0, status="aborted",
            ))

    if any_failed:
        for table in built_tables:
            _drop_table_best_effort(store, table)
        summary, error_id = _safe_error("; ".join(internal_errors) or "build failed")
        store.update_dataset_lifecycle(
            dataset_version, status="failed", error=f"{summary} [{error_id}]",
        )
        store.finish_run(
            run_id,
            tables=len(results),
            rows=sum(r.mapped for r in results),
            status="failed",
            detail=summary,
        )
        return BuildDatasetResult(
            source=source,
            dataset_version=dataset_version,
            previous_dataset_version=previous,
            status="failed",
            ready=False,
            published=False,
            results=results,
            outcome="failed",
            reason_code="build_failed",
            error=summary,
            error_id=error_id,
        )

    store.finish_run(
        run_id,
        tables=len(results),
        rows=sum(r.mapped for r in results),
        status="ok",
        detail=f"build ready: {dataset_version}",
    )
    ds = store.get_dataset_version(dataset_version)
    objs = store.list_object_versions(dataset_version)
    ready = bool(ds and is_dataset_ready(ds, objs))
    if auto_publish:
        raise NotImplementedError("publish_dataset is M2-T06")
    return BuildDatasetResult(
        source=source,
        dataset_version=dataset_version,
        previous_dataset_version=previous,
        status="building",
        ready=ready,
        published=False,
        results=results,
        outcome="ok",
    )
