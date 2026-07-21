"""数据集发布域服务:快照解析(T03)、候选构建(T05)、原子发布/回滚与保留(T06)。

调用约定(T08/T09):resolve_published_snapshot 与后续对象/指标查询必须位于同一
SQLite 读事务(BEGIN … COMMIT)内,避免并发发布时看到混版。本模块不自动开事务,
由调用方持有连接并包裹。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from data2agent.connect.landing import LandingStore, _now
from data2agent.connect.mapping_apply import (
    DEFAULT_BREAKER_THRESHOLD,
    MappingCircuitBreaker,
    ObjectApplyResult,
    apply_object,
)
from data2agent.metamodel.dataset_publish_contract import (
    evaluate_publish,
    evaluate_rollback,
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

logger = logging.getLogger(__name__)

SnapshotReason = Literal["not_published", "snapshot_corrupt"]
BuildOutcome = Literal["ok", "conflict", "failed"]
ActionOutcome = Literal["ok", "idempotent", "not_found", "conflict", "error"]


@contextmanager
def published_read_tx(store: LandingStore) -> Iterator[LandingStore]:
    """同一 SQLite 读快照内完成 resolve + 后续对象/指标查询;可重入。

    MCP / Console 读路径应包裹 resolve_published_snapshot 与跟随查询,
    避免并发 publish 时单次请求内混版。嵌套调用共用外层事务。
    """
    depth = getattr(store, "_published_read_depth", 0)
    if depth > 0:
        yield store
        return
    isolation = store.con.isolation_level
    store.con.isolation_level = None
    store._published_read_depth = 1
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
        store._published_read_depth = 0
        store.con.isolation_level = isolation


class PublishedSnapshotError(Exception):
    """公开安全错误;detail 不含表名/SQL/内部异常。"""

    def __init__(self, reason_code: SnapshotReason, detail: str = "数据集快照不可用"):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail)


class _TxnRecheckAbort(Exception):
    """临界事务内重检得到非 execute 决策;外层映射为幂等/409/404,非 500。"""

    def __init__(self, outcome: str, reason_code: str | None, http_status: int | None):
        self.outcome = outcome
        self.reason_code = reason_code
        self.http_status = http_status
        super().__init__(f"recheck:{outcome}:{reason_code}")


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
    status: Literal["building", "failed", "published"] | None
    ready: bool
    published: bool
    results: list[ObjectApplyResult] = field(default_factory=list)
    outcome: BuildOutcome = "failed"
    reason_code: str | None = None
    error: str | None = None
    error_id: str | None = None
    run_id: int | None = None
    step_ids: dict[str, int] = field(default_factory=dict)


@dataclass
class DatasetMutationResult:
    """publish/rollback 领域结果;HTTP 层映射 200/404/409/500。"""

    executed: bool
    dataset_version: str
    outcome: ActionOutcome
    reason_code: str | None = None
    http_status: int | None = None
    error: str | None = None
    error_id: str | None = None
    note: str = ""


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


def _safe_action_error(detail: str) -> tuple[str, str]:
    error_id = hashlib.sha256(detail.encode("utf-8")).hexdigest()[:12]
    return "数据集发布操作失败", error_id


def _drop_table_best_effort(store: LandingStore, table: str | None) -> bool:
    """尝试删除物理表。仅在确认表已不存在时返回 True。"""
    if not table or not is_valid_build_table(table):
        return False
    try:
        store.con.execute(f"DROP TABLE IF EXISTS {_quote_ident(table)}")
        store.con.commit()
    except Exception:
        try:
            store.con.rollback()
        except Exception:
            pass
        return False
    return not _table_exists(store, table)


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
    """关闭同 source 陈旧 building;若仍有运行中任务则返回 conflict reason。

    每个候选在 BEGIN IMMEDIATE 内重读:仍为 building 且无 running 租约才标记
    failed。publish 已获胜则跳过。物理表仅在元数据事务提交后清理。
    """
    building_rows, _ = store.list_dataset_versions(
        source=source, status="building", limit=50, offset=0,
    )
    for ds in building_rows:
        version = ds.dataset_version
        if _has_active_build_run(store, version):
            return "active_build"

        tables_to_drop: list[str] = []
        marked_failed = False

        def _txn() -> None:
            nonlocal marked_failed
            current = store.get_dataset_version(version)
            if current is None or current.status != "building":
                # publish/rollback 或其他路径已改变状态:不得按旧快照回收。
                return
            if _has_active_build_run(store, version):
                raise _TxnRecheckAbort("conflict", "active_build", 409)

            protected = _protected_build_tables(store, source)
            objs = store.list_object_versions(version)
            if any(o.status == "published" for o in objs):
                # 数据集行仍 building 但对象已发布:异常交错,跳过以免破坏 publish。
                return
            for obj in objs:
                table = obj.build_table
                if obj.status == "building":
                    try:
                        store.update_object_build_result(
                            version,
                            obj.object,
                            status="failed",
                            row_count=0,
                            build_table=None,
                            commit=False,
                        )
                    except ValueError:
                        store.update_object_lifecycle(
                            version, obj.object, status="failed", commit=False,
                        )
                elif obj.status == "built":
                    # 冻结字段不允许清空 build_table;至少把状态改成 failed。
                    store.update_object_lifecycle(
                        version, obj.object, status="failed", commit=False,
                    )
                if table and table not in protected:
                    tables_to_drop.append(table)
            store.update_dataset_lifecycle(
                version,
                status="failed",
                error="interrupted build recovered",
                commit=False,
            )
            marked_failed = True

        try:
            _run_immediate_txn(store, _txn)
        except _TxnRecheckAbort as e:
            if e.reason_code == "active_build":
                return "active_build"
            raise

        if marked_failed:
            for table in tables_to_drop:
                _drop_table_best_effort(store, table)
    return None


def _claim_building_candidate(
    store: LandingStore,
    *,
    dataset_version: str,
    source: str,
    pack: TemplatePack,
    previous: str | None,
    manifest: list[str],
    now: str,
) -> int:
    """同一 IMMEDIATE 事务内预占 building 候选并绑定 running Run。

    避免“building 已提交、Run 尚未绑定”窗口被并发 _recover_stale_building 误判。
    """
    run_ids: list[int] = []

    def _txn() -> None:
        building_rows, _ = store.list_dataset_versions(
            source=source, status="building", limit=50, offset=0,
        )
        for ds in building_rows:
            if _has_active_build_run(store, ds.dataset_version):
                raise _TxnRecheckAbort("conflict", "active_build", 409)
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
            ),
            commit=False,
        )
        run_id = store.start_run(source, "apply", commit=False)
        store.set_run_dataset_version(run_id, dataset_version, commit=False)
        run_ids.append(run_id)

    try:
        _run_immediate_txn(store, _txn)
    except _TxnRecheckAbort:
        raise
    return run_ids[0]

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


def _validate_version_tables(
    store: LandingStore,
    ds: DatasetVersionRecord,
    objs: list[ObjectVersionRecord],
    *,
    expected_status: str,
) -> str | None:
    """物理表完整性校验(临界事务外)。失败返回 reason_code,成功返回 None。"""
    manifest = parse_object_manifest(ds.object_manifest)
    if not manifest:
        return "not_ready"
    by_name = {o.object: o for o in objs if o.dataset_version == ds.dataset_version}
    if set(by_name) != set(manifest):
        return "not_ready"
    try:
        _parse_template_snapshot(
            ds.template_snapshot,
            expected_version=ds.template_version,
            manifest=manifest,
        )
    except PublishedSnapshotError:
        return "not_ready"
    for name in manifest:
        row = by_name[name]
        if row.status != expected_status:
            return "not_ready"
        if row.purged_at is not None or not row.build_table:
            return "not_ready"
        try:
            table = validate_build_table(row.build_table)
        except ValueError:
            return "not_ready"
        if not _table_exists(store, table):
            return "not_ready"
        if _count_rows(store, table) != row.row_count:
            return "not_ready"
    return None


def _run_immediate_txn(store: LandingStore, body: Callable[[], None]) -> None:
    """BEGIN IMMEDIATE 短事务;失败整段 ROLLBACK。禁止在 body 内扫对象数据表。"""
    isolation = store.con.isolation_level
    store.con.isolation_level = None
    try:
        store.con.execute("BEGIN IMMEDIATE")
        try:
            body()
            store.con.execute("COMMIT")
        except Exception:
            try:
                store.con.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        store.con.isolation_level = isolation


def _gc_retired_physical_tables(store: LandingStore, source: str) -> None:
    """临界事务外 best-effort GC:永不清理 current 与 current.previous。"""
    published = store.get_published_dataset(source)
    if published is None:
        return
    protected = {published.dataset_version}
    if published.previous_dataset_version:
        protected.add(published.previous_dataset_version)
    retired, _ = store.list_dataset_versions(
        source=source, status="retired", limit=500, offset=0,
    )
    now = _now()
    for ds in retired:
        if ds.dataset_version in protected:
            continue
        for obj in store.list_object_versions(ds.dataset_version):
            table = obj.build_table
            if not table or obj.purged_at is not None:
                continue
            try:
                if not _drop_table_best_effort(store, table):
                    logger.warning(
                        "retention GC drop failed for %s/%s table=%s",
                        ds.dataset_version, obj.object, table,
                    )
                    continue
                store.purge_object_build_table(
                    ds.dataset_version, obj.object, purged_at=now,
                )
            except Exception:
                logger.warning(
                    "retention GC failed for %s/%s",
                    ds.dataset_version, obj.object, exc_info=True,
                )


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


def publish_dataset(store: LandingStore, version: str) -> DatasetMutationResult:
    """原子发布候选数据集;幂等、陈旧 previous→409;成功后 best-effort GC。"""
    candidate = store.get_dataset_version(version)
    if candidate is None:
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="not_found",
            reason_code="not_found",
            http_status=404,
            note="数据集版本不存在",
        )
    source = candidate.source
    objects = store.list_object_versions(version)
    current = store.get_published_dataset(source)
    decision = evaluate_publish(
        candidate=candidate, objects=objects, current_published=current,
    )
    if decision.outcome == "idempotent":
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="idempotent",
            note="already published",
        )
    if decision.outcome != "execute":
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome=decision.outcome,  # type: ignore[arg-type]
            reason_code=decision.reason_code,
            http_status=decision.http_status,
            note=decision.reason_code or "conflict",
        )

    physical = _validate_version_tables(
        store, candidate, objects, expected_status="built",
    )
    if physical is not None:
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="conflict",
            reason_code=physical,
            http_status=409,
            note=physical,
        )

    run_id = store.start_run(source, "publish")
    now = _now()

    def _txn() -> None:
        cand = store.get_dataset_version(version)
        objs = store.list_object_versions(version)
        cur = store.get_published_dataset(source)
        again = evaluate_publish(
            candidate=cand, objects=objs, current_published=cur,
        )
        if again.outcome == "idempotent":
            raise _TxnRecheckAbort("idempotent", None, None)
        if again.outcome != "execute":
            raise _TxnRecheckAbort(
                again.outcome, again.reason_code, again.http_status,
            )
        if cur is not None:
            for obj in store.list_object_versions(cur.dataset_version):
                store.update_object_lifecycle(
                    cur.dataset_version, obj.object,
                    status="retired", commit=False,
                )
            store.update_dataset_lifecycle(
                cur.dataset_version, status="retired", commit=False,
            )
        for obj in objs:
            store.update_object_lifecycle(
                version, obj.object,
                status="published", published_at=now, commit=False,
            )
        store.update_dataset_lifecycle(
            version, status="published", published_at=now, commit=False,
        )
        store.set_run_dataset_version(run_id, version, commit=False)
        store.finish_run(
            run_id,
            tables=len(objects),
            rows=sum(o.row_count for o in objects),
            status="ok",
            detail=f"published: {version}",
            commit=False,
        )

    try:
        _run_immediate_txn(store, _txn)
    except _TxnRecheckAbort as e:
        try:
            store.finish_run(
                run_id, tables=0, rows=0, status="aborted",
                detail=f"publish recheck:{e.outcome}",
            )
        except Exception:
            pass
        if e.outcome == "idempotent":
            return DatasetMutationResult(
                executed=False,
                dataset_version=version,
                outcome="idempotent",
                note="already published",
            )
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome=e.outcome,  # type: ignore[arg-type]
            reason_code=e.reason_code,
            http_status=e.http_status,
            note=e.reason_code or e.outcome,
        )
    except Exception as e:
        summary, error_id = _safe_action_error(str(e))
        try:
            store.finish_run(
                run_id, tables=0, rows=0, status="failed", detail=summary,
            )
        except Exception:
            pass
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="error",
            http_status=500,
            error=summary,
            error_id=error_id,
            note=summary,
        )

    # 仅在完整发布成功后取代旧隔离;保留本轮 build 的 quarantine batch。
    for obj in objects:
        keep = {obj.batch_id} if obj.batch_id else set()
        try:
            store.quarantine_supersede_except(source, obj.object, keep)
        except Exception:
            logger.warning(
                "quarantine supersede after publish failed object=%s",
                obj.object,
                exc_info=True,
            )
    try:
        _gc_retired_physical_tables(store, source)
    except Exception:
        logger.warning("retention GC after publish failed", exc_info=True)

    return DatasetMutationResult(
        executed=True,
        dataset_version=version,
        outcome="ok",
        note="published",
    )


def rollback_dataset(store: LandingStore, version: str) -> DatasetMutationResult:
    """一步回滚到 current.previous;幂等;成功后 best-effort GC。"""
    target = store.get_dataset_version(version)
    if target is None:
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="not_found",
            reason_code="not_found",
            http_status=404,
            note="数据集版本不存在",
        )
    source = target.source
    current = store.get_published_dataset(source)
    decision = evaluate_rollback(target=target, current_published=current)
    if decision.outcome == "idempotent":
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="idempotent",
            note="already current",
        )
    if decision.outcome != "execute":
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome=decision.outcome,  # type: ignore[arg-type]
            reason_code=decision.reason_code,
            http_status=decision.http_status,
            note=decision.reason_code or "conflict",
        )

    assert current is not None
    target_objs = store.list_object_versions(version)
    physical = _validate_version_tables(
        store, target, target_objs, expected_status="retired",
    )
    if physical is not None:
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="conflict",
            reason_code=physical,
            http_status=409,
            note=physical,
        )

    leaving = current.dataset_version
    run_id = store.start_run(source, "rollback")
    now = _now()

    def _txn() -> None:
        tgt = store.get_dataset_version(version)
        cur = store.get_published_dataset(source)
        again = evaluate_rollback(target=tgt, current_published=cur)
        if again.outcome == "idempotent":
            raise _TxnRecheckAbort("idempotent", None, None)
        if again.outcome != "execute":
            raise _TxnRecheckAbort(
                again.outcome, again.reason_code, again.http_status,
            )
        assert cur is not None
        for obj in store.list_object_versions(cur.dataset_version):
            store.update_object_lifecycle(
                cur.dataset_version, obj.object,
                status="retired", commit=False,
            )
        store.update_dataset_lifecycle(
            cur.dataset_version, status="retired", commit=False,
        )
        for obj in store.list_object_versions(version):
            store.update_object_lifecycle(
                version, obj.object,
                status="published", published_at=now, commit=False,
            )
        store.update_dataset_lifecycle(
            version,
            status="published",
            published_at=now,
            previous_dataset_version=leaving,
            commit=False,
        )
        store.set_run_dataset_version(run_id, version, commit=False)
        store.finish_run(
            run_id,
            tables=len(target_objs),
            rows=sum(o.row_count for o in target_objs),
            status="ok",
            detail=f"rolled back to: {version}",
            commit=False,
        )

    try:
        _run_immediate_txn(store, _txn)
    except _TxnRecheckAbort as e:
        try:
            store.finish_run(
                run_id, tables=0, rows=0, status="aborted",
                detail=f"rollback recheck:{e.outcome}",
            )
        except Exception:
            pass
        if e.outcome == "idempotent":
            return DatasetMutationResult(
                executed=False,
                dataset_version=version,
                outcome="idempotent",
                note="already current",
            )
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome=e.outcome,  # type: ignore[arg-type]
            reason_code=e.reason_code,
            http_status=e.http_status,
            note=e.reason_code or e.outcome,
        )
    except Exception as e:
        summary, error_id = _safe_action_error(str(e))
        try:
            store.finish_run(
                run_id, tables=0, rows=0, status="failed", detail=summary,
            )
        except Exception:
            pass
        return DatasetMutationResult(
            executed=False,
            dataset_version=version,
            outcome="error",
            http_status=500,
            error=summary,
            error_id=error_id,
            note=summary,
        )

    try:
        _gc_retired_physical_tables(store, source)
    except Exception:
        logger.warning("retention GC after rollback failed", exc_info=True)

    return DatasetMutationResult(
        executed=True,
        dataset_version=version,
        outcome="ok",
        note="rolled back",
    )


def build_dataset(
    store: LandingStore,
    pack: TemplatePack,
    source: str,
    *,
    auto_publish: bool = False,
    threshold: float = DEFAULT_BREAKER_THRESHOLD,
) -> BuildDatasetResult:
    """构建完整候选数据集;成功后保持 building+ready,或 auto_publish 原子发布。"""
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
    try:
        run_id = _claim_building_candidate(
            store,
            dataset_version=dataset_version,
            source=source,
            pack=pack,
            previous=previous,
            manifest=manifest,
            now=now,
        )
    except _TxnRecheckAbort as e:
        return BuildDatasetResult(
            source=source,
            dataset_version=None,
            previous_dataset_version=None,
            status=None,
            ready=False,
            published=False,
            outcome="conflict",
            reason_code=e.reason_code or "active_build",
            error="已有运行中的数据集构建",
        )

    results: list[ObjectApplyResult] = []
    step_ids: dict[str, int] = {}
    any_failed = False
    internal_errors: list[str] = []
    # 延后写 built:对象保持 building,失败时可在冻结规则内清空 build_table。
    pending_ok: list[tuple[str, ObjectApplyResult, str]] = []

    for ordinal, (tpl, binding) in enumerate(members, start=1):
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
        step_id: int | None = None
        try:
            step_id = store.add_step(run_id, ordinal, "object", tpl.object)
            step_ids[tpl.object] = step_id
            result = apply_object(
                store, tpl, source, build_table=table, threshold=threshold,
                supersede_quarantine=False,
            )
            store.update_step(
                step_id, status="ok",
                rows_in=result.total, rows_out=result.mapped,
                quarantined=result.quarantined, batch_id=result.batch_id,
                error=None if result.status == "ok" else result.status,
            )
            pending_ok.append((tpl.object, result, result.build_table or table))
            results.append(result)
        except MappingCircuitBreaker as e:
            any_failed = True
            internal_errors.append(str(e))
            if step_id is not None:
                try:
                    store.update_step(
                        step_id, status="aborted",
                        rows_in=e.total, rows_out=e.mapped,
                        quarantined=e.quarantined, batch_id=e.batch_id,
                        error=str(e)[:500],
                    )
                except Exception:
                    pass
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
            if step_id is not None:
                try:
                    store.update_step(step_id, status="failed", error=str(e)[:500])
                except Exception:
                    pass
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
                tpl.object, 0, 0, 0, status="failed",
            ))

    if any_failed:
        for object_name, result, table in pending_ok:
            _drop_table_best_effort(store, table)
            try:
                store.update_object_build_result(
                    dataset_version,
                    object_name,
                    status="failed",
                    row_count=0,
                    build_table=None,
                    batch_id=result.batch_id,
                )
            except ValueError:
                store.update_object_lifecycle(
                    dataset_version, object_name, status="failed",
                )
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
            run_id=run_id,
            step_ids=step_ids,
        )

    for object_name, result, table in pending_ok:
        store.update_object_build_result(
            dataset_version,
            object_name,
            status="built",
            row_count=result.mapped,
            build_table=table,
            batch_id=result.batch_id,
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
        pub = publish_dataset(store, dataset_version)
        if pub.outcome in ("ok", "idempotent"):
            return BuildDatasetResult(
                source=source,
                dataset_version=dataset_version,
                previous_dataset_version=previous,
                status="published",
                ready=False,
                published=True,
                results=results,
                outcome="ok",
                run_id=run_id,
                step_ids=step_ids,
            )
        return BuildDatasetResult(
            source=source,
            dataset_version=dataset_version,
            previous_dataset_version=previous,
            status="building",
            ready=ready,
            published=False,
            results=results,
            outcome="failed",
            reason_code=pub.reason_code or "publish_failed",
            error=pub.error or pub.note or "发布失败",
            error_id=pub.error_id,
            run_id=run_id,
            step_ids=step_ids,
        )
    return BuildDatasetResult(
        source=source,
        dataset_version=dataset_version,
        previous_dataset_version=previous,
        status="building",
        ready=ready,
        published=False,
        results=results,
        outcome="ok",
        run_id=run_id,
        step_ids=step_ids,
    )
