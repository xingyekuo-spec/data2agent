"""只读查询服务 + "说"档建议卡,传输无关。

E4 起网关消费完整管道的产物:源系统 → sync(raw_*)→ apply(objv_*)→ 本服务。
枚举值 / 编码翻译在映射应用阶段已完成,网关只做属性校验、脱敏与口径警示。

治理档位(看/说/做,docs/design/03 §3):
- 看:query_objects / query_metrics,只读;
- 说:propose_action 生成结构化建议卡 —— 不落任何写操作,卡内每条依据
  必须引用本会话某次查询的 meta.query_id + meta.result_digest(数字可溯源);
- 做:审批后的写回,不在开源网关范围;max_tier 为部署级档位上限。

安全边界(lite):
- 落地库以只读模式(mode=ro)打开;
- 对象/指标数据读取在同一 SQLite 读事务内 resolve_published_snapshot,
  再查询该快照的物理表;无 published 不回退遗留 obj_*;
- sensitive 属性一律脱敏为 "***",当前不提供解敏开关(解敏属"做"档治理,后续按权限模型提供)。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from ..connect.dataset_publish import (
    PublishedDatasetSnapshot,
    PublishedSnapshotError,
    published_read_tx,
    resolve_published_snapshot,
)
from ..connect.landing import LandingStore
from ..metamodel.dataset_publish_contract import validate_build_table
from ..metamodel.loader import load_pack
from ..metamodel.schema import ObjectTemplate
from .evidence import (
    EvidenceContext,
    EvidenceStore,
    GatewayAuditRecord,
    MAX_PROPOSAL_EVIDENCE_ITEMS,
    ProposalEvidenceRecord,
    ProposalRecord,
    QueryEvidenceRecord,
    build_metric_result_summary,
    build_object_result_summary,
    build_result_envelope,
    canonical_json_dumps,
    constant_time_digest_equal,
    is_valid_digest,
    normalize_query_metrics,
    normalize_query_objects,
    result_digest,
)
from .metrics_impl import registry

MASK = "***"
TIER_ORDER = {"看": 0, "说": 1, "做": 2}
DEFAULT_EVIDENCE_TTL = timedelta(hours=24)
_QUERY_LOG_CAP = 500


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _safe_execute(con: sqlite3.Connection, sql: str, params: object = ()) -> list:
    """Execute published-data SQL; never surface SQLite messages to clients."""
    try:
        return list(con.execute(sql, params))
    except sqlite3.Error as e:
        raise ValueError("execution_failed: 数据集查询失败") from e


class QueryService:
    def __init__(self, db_path: str | Path, templates_root: str | Path = "templates",
                 source: str = "digiwin_e10", max_tier: str = "说",
                 audit_sink=None,
                 default_context: EvidenceContext | None = None,
                 evidence_ttl: timedelta = DEFAULT_EVIDENCE_TTL):
        """audit_sink:可选的持久审计回调(dict → None),每次工具调用一条;
        与抽取侧 d2a_audit_log 对称,HTTP 部署默认写 JSONL(见 __main__)。"""
        self.db_path = str(db_path)
        self.source = source
        self.pack = load_pack(templates_root)
        self.metrics = registry(source)
        if max_tier not in TIER_ORDER:
            raise ValueError(f"max_tier 须为 {sorted(TIER_ORDER)},got '{max_tier}'")
        self.max_tier = max_tier
        self.audit_sink = audit_sink
        self.default_context = default_context
        self.evidence_ttl = evidence_ttl
        self._query_log: OrderedDict[str, dict] = OrderedDict()
        self._proposal_seq = 0
        self._lock = threading.Lock()

    def _audit(self, record: dict) -> None:
        if self.audit_sink:
            self.audit_sink(record)

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def _resolve_context(self, context: EvidenceContext | None) -> EvidenceContext:
        resolved = context or self.default_context
        if resolved is None:
            raise ValueError("invalid_session: EvidenceContext is required for citable query")
        return resolved

    def _new_query_id(self) -> str:
        return f"qry_{secrets.token_hex(12)}"

    def _remember_query(self, query_id: str, entry: dict[str, object]) -> None:
        with self._lock:
            self._query_log[query_id] = entry
            while len(self._query_log) > _QUERY_LOG_CAP:
                self._query_log.popitem(last=False)

    @staticmethod
    def _parse_iso(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    def _persist_query_evidence(
        self,
        *,
        context: EvidenceContext,
        tool: str,
        target: str,
        normalized_query: dict[str, object],
        response_payload: dict[str, object],
        dataset_version: str | None,
        template_version: str | None,
        binding_hashes: dict[str, str],
        warnings: list[str],
        row_count: int | None,
    ) -> dict[str, object]:
        created_at = self._iso_now()
        expires_at = (
            datetime.fromisoformat(created_at) + self.evidence_ttl
        ).isoformat(timespec="seconds")
        envelope = build_result_envelope(
            tool=tool,
            source=self.source,
            target=target,
            normalized_query=normalized_query,
            dataset_version=dataset_version,
            template_version=template_version,
            binding_hashes=binding_hashes,
            response_payload=response_payload,
        )
        digest = result_digest(envelope)
        if tool == "query_objects":
            summary = build_object_result_summary(
                columns=[str(column) for column in response_payload.get("columns", [])],
                rows=list(response_payload.get("rows") or []),
            )
        else:
            summary = build_metric_result_summary(
                metric=str(response_payload.get("metric") or target),
                status=str(response_payload.get("status") or ""),
                unit=(
                    None if response_payload.get("unit") is None
                    else str(response_payload.get("unit"))
                ),
                group_by=(
                    None if response_payload.get("group_by") is None
                    else str(response_payload.get("group_by"))
                ),
                rows=list(response_payload.get("rows") or []),
            )
        query_id = self._new_query_id()
        detail = (
            f"query:{tool}:{target} rows={row_count if row_count is not None else 'na'}"
        )
        record = QueryEvidenceRecord(
            query_id=query_id,
            principal=context.principal,
            session_id=context.session_id,
            channel=context.channel,
            source=self.source,
            tool=tool,
            target=target,
            normalized_query_json=canonical_json_dumps(normalized_query),
            dataset_version=dataset_version,
            template_version=template_version,
            binding_hashes_json=canonical_json_dumps(binding_hashes),
            result_digest=digest,
            result_summary_json=canonical_json_dumps(summary),
            warnings_json=canonical_json_dumps([w for w in warnings if w]),
            row_count=row_count,
            created_at=created_at,
            expires_at=expires_at,
        )
        audit = GatewayAuditRecord(
            event_id=f"evt_{secrets.token_hex(12)}",
            created_at=created_at,
            principal=context.principal,
            session_id=context.session_id,
            channel=context.channel,
            source=self.source,
            operation=tool,
            target=target,
            outcome="ok",
            reason_code="ok",
            query_id=query_id,
            dataset_version=dataset_version,
            result_digest=digest,
            detail_json=canonical_json_dumps(
                {"detail": detail, "row_count": row_count, "tool": tool}
            ),
        )
        store = LandingStore(self.db_path)
        evidence = EvidenceStore(store)
        try:
            store.con.execute("BEGIN IMMEDIATE")
            evidence.insert_query(record, commit=False)
            evidence.insert_audit(audit, commit=False)
            store.con.commit()
        except sqlite3.Error as exc:
            store.con.rollback()
            raise ValueError(
                "evidence_store_unavailable: query evidence persist failed"
            ) from exc
        finally:
            store.con.close()
        self._remember_query(
            query_id,
            {
                "query_id": query_id,
                "source": self.source,
                "tool": tool,
                "target": target,
                "normalized_query": normalized_query,
                "dataset_version": dataset_version,
                "template_version": template_version,
                "binding_hashes": binding_hashes,
                "result_digest": digest,
                "result_summary": summary,
                "warnings": [w for w in warnings if w],
                "created_at": created_at,
                "expires_at": expires_at,
                "session_id": context.session_id,
                "at": created_at,
            },
        )
        self._audit(
            {
                "query_id": query_id,
                "tool": tool,
                "target": target,
                "detail": detail,
                "at": created_at,
                "dataset_version": dataset_version,
            }
        )
        return {
            "query_id": query_id,
            "session_id": context.session_id,
            "result_digest": digest,
            "result_summary": summary,
            "created_at": created_at,
            "expires_at": expires_at,
            "warnings": [w for w in warnings if w],
        }

    def _proposal_context(self, context: EvidenceContext | None) -> EvidenceContext:
        resolved = context or self.default_context
        if resolved is None:
            raise ValueError("invalid_session: EvidenceContext is required for proposal")
        return resolved

    def _proposal_reject(
        self,
        *,
        context: EvidenceContext,
        reason_code: str,
        target: str,
        detail: str,
        query_id: str | None = None,
    ) -> None:
        try:
            store = LandingStore(self.db_path)
            evidence = EvidenceStore(store)
            evidence.insert_audit(
                GatewayAuditRecord(
                    event_id=f"evt_{secrets.token_hex(12)}",
                    created_at=self._iso_now(),
                    principal=context.principal,
                    session_id=context.session_id,
                    channel=context.channel,
                    source=self.source,
                    operation="propose_action",
                    target=target,
                    outcome="rejected",
                    reason_code=reason_code,
                    query_id=query_id,
                    detail_json=canonical_json_dumps({"detail": detail}),
                ),
                commit=True,
            )
            store.con.close()
        except Exception:
            pass

    def _validate_cited_query(
        self,
        *,
        context: EvidenceContext,
        target: str,
        index: int,
        item: dict,
    ) -> tuple[dict[str, object], list[str]]:
        if not isinstance(item, dict):
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail=f"evidence[{index}] 不是对象",
            )
            raise ValueError(f"invalid_params: evidence[{index}] 须为对象")
        claim = str(item.get("claim", "") or "")
        qid = str(item.get("query_id", "") or "")
        digest = str(item.get("result_digest", "") or "")
        if not claim or not qid or not digest:
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail=f"evidence[{index}] 缺少 claim/query_id/result_digest",
                query_id=qid or None,
            )
            raise ValueError(f"evidence[{index}] 须同时含 claim/query_id/result_digest")
        if len(claim) > 500:
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail=f"evidence[{index}] claim 超出长度限制",
                query_id=qid,
            )
            raise ValueError("invalid_params: evidence claim 超出长度限制")
        if not is_valid_digest(digest):
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail=f"evidence[{index}] result_digest 格式非法",
                query_id=qid,
            )
            raise ValueError("invalid_params: result_digest 格式非法")

        store = LandingStore(self.db_path)
        evidence = EvidenceStore(store)
        try:
            record = evidence.get_query(qid)
        finally:
            store.con.close()
        if record is None:
            self._proposal_reject(
                context=context,
                reason_code="query_expired",
                target=target,
                detail="proposal 引用不存在的 query evidence",
                query_id=qid,
            )
            raise ValueError("query_expired: query evidence 不存在")
        if self._parse_iso(record.expires_at) <= datetime.now(UTC):
            self._proposal_reject(
                context=context,
                reason_code="query_expired",
                target=target,
                detail="proposal 引用已过期 query evidence",
                query_id=qid,
            )
            raise ValueError("query_expired: query evidence 已过期")
        if record.principal != context.principal:
            self._proposal_reject(
                context=context,
                reason_code="evidence_principal_mismatch",
                target=target,
                detail="proposal principal 与 query evidence 不一致",
            )
            raise ValueError("evidence_principal_mismatch: query evidence 属于其他主体")
        if record.session_id != context.session_id:
            self._proposal_reject(
                context=context,
                reason_code="evidence_session_mismatch",
                target=target,
                detail="proposal session 与 query evidence 不一致",
                query_id=qid,
            )
            raise ValueError("evidence_session_mismatch: query evidence 不属于当前会话")
        if record.source != self.source:
            self._proposal_reject(
                context=context,
                reason_code="evidence_source_mismatch",
                target=target,
                detail="proposal source 与 query evidence 不一致",
                query_id=qid,
            )
            raise ValueError("evidence_source_mismatch: query evidence source 不一致")
        if not record.dataset_version:
            self._proposal_reject(
                context=context,
                reason_code="dataset_version_mismatch",
                target=target,
                detail="proposal 引用的 query evidence 缺少 dataset version",
                query_id=qid,
            )
            raise ValueError("dataset_version_mismatch: query evidence 缺少 dataset_version")
        if record.evidence_schema_version != 1:
            self._proposal_reject(
                context=context,
                reason_code="evidence_integrity_failed",
                target=target,
                detail="query evidence schema version 非法",
                query_id=qid,
            )
            raise ValueError("evidence_integrity_failed: unsupported evidence schema version")
        if not (
            is_valid_digest(record.result_digest)
            and constant_time_digest_equal(record.result_digest, digest)
        ):
            self._proposal_reject(
                context=context,
                reason_code="result_digest_mismatch",
                target=target,
                detail="proposal digest 与 query evidence 不一致",
                query_id=qid,
            )
            raise ValueError("result_digest_mismatch: result_digest 与 query evidence 不一致")
        try:
            normalized_query = json.loads(record.normalized_query_json)
            binding_hashes = json.loads(record.binding_hashes_json)
            result_summary = json.loads(record.result_summary_json)
            warnings = list(json.loads(record.warnings_json))
        except Exception as exc:
            self._proposal_reject(
                context=context,
                reason_code="evidence_integrity_failed",
                target=target,
                detail="query evidence JSON 无法解析",
                query_id=qid,
            )
            raise ValueError("evidence_integrity_failed: query evidence JSON 无法解析") from exc
        return (
            {
                "claim": claim,
                "query_id": record.query_id,
                "source": record.source,
                "tool": record.tool,
                "target": record.target,
                "normalized_query": normalized_query,
                "dataset_version": record.dataset_version,
                "template_version": record.template_version,
                "binding_hashes": binding_hashes,
                "result_digest": record.result_digest,
                "result_summary": result_summary,
                "warnings": warnings,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
            },
            warnings,
        )

    @contextmanager
    def _published_tx(self) -> Iterator[tuple[LandingStore, PublishedDatasetSnapshot]]:
        """同一只读事务内解析并持有 published 快照。"""
        store = LandingStore.open_readonly(self.db_path)
        try:
            with published_read_tx(store):
                try:
                    snap = resolve_published_snapshot(store, self.source)
                except PublishedSnapshotError as e:
                    raise ValueError(f"{e.reason_code}: {e.detail}") from None
                yield store, snap
        finally:
            store.con.close()

    def _public_meta(
        self, *, tool: str, target: str, query_id: str | None,
        row_count: int | None, duration_ms: int,
        masked_fields: list[str] | None = None,
        warnings: list[str] | None = None,
        session_id: str | None = None,
        result_digest: str | None = None,
        result_summary: dict | None = None,
        created_at: str | None = None,
        expires_at: str | None = None,
        **extra: object,
    ) -> dict:
        """Console/MCP Lab 公共 meta;保留额外兼容字段(source/binding_status 等)。"""
        meta = {
            "query_id": query_id,
            "tool": tool,
            "target": target,
            "row_count": row_count,
            "duration_ms": max(0, int(duration_ms)),
            "masked_fields": list(masked_fields or []),
            "warnings": [w for w in (warnings or []) if w],
            "evidence_scope": "principal_session",
            "session_id": session_id,
            "result_digest": result_digest,
            "result_summary": result_summary,
            "created_at": created_at,
            "expires_at": expires_at,
        }
        meta.update(extra)
        return meta

    # ---- query_objects ----

    @staticmethod
    def _require_scalar_filter_value(name: str, val: object) -> None:
        if isinstance(val, (dict, list)):
            raise ValueError(f"filters['{name}'] 须为标量值,不能为对象或数组")

    @staticmethod
    def _require_int_limit(limit: object) -> int:
        # bool 是 int 子类,必须排除
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit 须为整数")
        return limit

    def probe_objects(self, object: str, *, limit: int = 1) -> dict:
        """执行一次不可引用、无持久副作用的已发布对象查询。

        供运行时健康/验收探测使用：查询路径、published snapshot、字段脱敏与
        ``query_objects`` 完全一致，但不签发 query_id、不写 M5 evidence/audit，
        因而不会把健康检查变成用户可引用的业务查询记录。
        """
        started = time.perf_counter()
        if not isinstance(object, str) or not object:
            raise ValueError("object 须为非空字符串")
        limit = self._require_int_limit(limit)
        limit = max(1, min(limit, 20))
        with self._published_tx() as (store, snap):
            tpl = next((o for o in snap.template_pack.objects if o.object == object), None)
            entry = snap.objects.get(object)
            if tpl is None or entry is None:
                raise ValueError("not_published: 对象未包含在已发布数据集中")
            binding = next(
                (b for b in tpl.bindings if b.source == self.source and b.enabled),
                None,
            )
            physical = validate_build_table(entry.physical_table)
            sql, params = self._object_sql(tpl, None, None, False, limit, physical)
            rows = [dict(row) for row in _safe_execute(store.con, sql, params)]
            sensitive = {p.name for p in tpl.properties if p.sensitive}
            dataset_version = snap.dataset_version
            template_version = snap.template_version
            binding_hashes = {object: entry.binding_hash}
            binding_status = binding.status if binding is not None else "published"
            display_name = tpl.display_name
            quarantined = self._quarantine_count(store.con, object)
        for row in rows:
            for prop in sensitive:
                if row.get(prop) is not None:
                    row[prop] = MASK
        note = (
            "binding 为 draft:字段映射按参考表形构造,口径未经现场校准"
            if binding is not None and binding.status == "draft" else ""
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "object": object,
            "display_name": display_name,
            "rows": rows,
            "meta": self._public_meta(
                tool="query_objects", target=object, query_id=None,
                row_count=len(rows), duration_ms=duration_ms,
                masked_fields=sorted(sensitive), warnings=[note] if note else [],
                source=self.source, binding_status=binding_status,
                quarantined=quarantined, dataset_version=dataset_version,
                template_version=template_version, binding_hashes=binding_hashes,
                probe=True,
            ),
        }

    def query_objects(
        self,
        object: str | None = None,
        filters: dict | None = None,
        order_by: str | None = None,
        desc: bool = False,
        limit: int = 20,
        *,
        context: EvidenceContext | None = None,
    ) -> dict:
        started = time.perf_counter()
        if object is not None and not isinstance(object, str):
            raise ValueError("object 须为字符串")
        if filters is not None:
            if not isinstance(filters, dict):
                raise ValueError("filters 须为对象(属性→值映射),不能为数组或其他类型")
            for name, val in filters.items():
                if not isinstance(name, str):
                    raise ValueError("filters 键须为字符串")
                self._require_scalar_filter_value(name, val)
        if order_by is not None and not isinstance(order_by, str):
            raise ValueError("order_by 须为字符串")
        if not isinstance(desc, bool):
            raise ValueError("desc 须为布尔值")
        limit = self._require_int_limit(limit)
        if object is None:
            return self._object_catalog(started)

        # 实际数据读取以 published 冻结快照为准;磁盘模板仅用于目录展示。
        with self._published_tx() as (store, snap):
            tpl = next((o for o in snap.template_pack.objects if o.object == object), None)
            entry = snap.objects.get(object)
            if tpl is None or entry is None:
                known = set(self.pack.object_names()) | set(
                    snap.template_pack.object_names()
                )
                if object not in known:
                    raise ValueError(
                        f"未知对象 '{object}',可用:{sorted(known)}"
                    )
                raise ValueError("not_published: 对象未包含在已发布数据集中")
            binding = next(
                (b for b in tpl.bindings if b.source == self.source and b.enabled),
                None,
            )
            physical = validate_build_table(entry.physical_table)
            sql, params = self._object_sql(tpl, filters, order_by, desc, limit, physical)
            rows = [dict(r) for r in _safe_execute(store.con, sql, params)]
            quarantined = self._quarantine_count(store.con, object)
            dataset_version = snap.dataset_version
            template_version = snap.template_version
            binding_hashes = {object: entry.binding_hash}
            binding_status = binding.status if binding is not None else "published"
            display_name = tpl.display_name
            sensitive = {p.name for p in tpl.properties if p.sensitive}

        for row in rows:
            for prop in sensitive:
                if row.get(prop) is not None:
                    row[prop] = MASK
        context = self._resolve_context(context)
        note = ("binding 为 draft:字段映射按参考表形构造,口径未经现场校准"
                if binding is not None and binding.status == "draft" else "")
        warnings = [note] if note else []
        persisted = self._persist_query_evidence(
            context=context,
            tool="query_objects",
            target=object,
            normalized_query=normalize_query_objects(
                source=self.source,
                object_name=object,
                filters=filters,
                order_by=order_by,
                desc=desc,
                limit=limit,
            ),
            response_payload={
                "object": object,
                "display_name": display_name,
                "columns": [p.name for p in tpl.properties],
                "rows": rows,
                "meta": {
                    "source": self.source,
                    "binding_status": binding_status,
                    "quarantined": quarantined,
                    "note": note,
                    "dataset_version": dataset_version,
                    "template_version": template_version,
                    "binding_hashes": binding_hashes,
                },
            },
            dataset_version=dataset_version,
            template_version=template_version,
            binding_hashes=binding_hashes,
            warnings=warnings,
            row_count=len(rows),
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "object": object,
            "display_name": display_name,
            "rows": rows,
            "meta": self._public_meta(
                tool="query_objects", target=object, query_id=str(persisted["query_id"]),
                row_count=len(rows), duration_ms=duration_ms,
                session_id=str(persisted["session_id"]),
                result_digest=str(persisted["result_digest"]),
                result_summary=dict(persisted["result_summary"]),
                created_at=str(persisted["created_at"]),
                expires_at=str(persisted["expires_at"]),
                masked_fields=sorted(sensitive), warnings=warnings,
                source=self.source, binding_status=binding_status,
                quarantined=quarantined, note=note,
                dataset_version=dataset_version,
                template_version=template_version,
                binding_hashes=binding_hashes,
            ),
        }

    def _object_sql(self, tpl: ObjectTemplate, filters: dict | None,
                    order_by: str | None, desc: bool, limit: int,
                    physical_table: str) -> tuple[str, list]:
        props = {p.name: p for p in tpl.properties}
        cols = ", ".join('"{}"'.format(n) for n in props)
        where, params = [], []
        for name, val in (filters or {}).items():
            prop = props.get(name)
            if prop is None:
                raise ValueError(f"未知筛选字段 '{name}',可用:{sorted(props)}")
            if prop.type == "enum" and val not in prop.enum_values:
                raise ValueError(f"'{name}' 取值须为 {sorted(prop.enum_values)},got '{val}'")
            where.append(f'"{name}" = ?')
            params.append(val)
        order = ""
        if order_by:
            if order_by not in props:
                raise ValueError(f"未知排序字段 '{order_by}',可用:{sorted(props)}")
            order = f' ORDER BY "{order_by}" {"DESC" if desc else "ASC"}'
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        limit = max(1, min(int(limit), 200))
        table = _quote_ident(validate_build_table(physical_table))
        return (f'SELECT {cols} FROM {table}{where_sql}{order} LIMIT {limit}',
                params)

    def _quarantine_count(self, con: sqlite3.Connection, object_name: str) -> int:
        try:
            (n,) = con.execute(
                "SELECT COUNT(*) FROM d2a_quarantine "
                "WHERE source = ? AND object = ? AND resolved_at IS NULL",
                (self.source, object_name)).fetchone()
            return n
        except sqlite3.OperationalError:
            return 0

    def _object_catalog(self, started: float | None = None) -> dict:
        t0 = time.perf_counter() if started is None else started
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "objects": [
                {
                    "object": o.object,
                    "display_name": o.display_name,
                    "domain": o.domain,
                    "description": o.description,
                    "keys": o.keys,
                    "properties": [
                        {"name": p.name, "type": p.type, "desc": p.desc,
                         **({"sensitive": True} if p.sensitive else {})}
                        for p in o.properties
                    ],
                    "states": o.states,
                    "sources": [b.source for b in o.bindings],
                }
                for o in self.pack.objects
            ],
            "meta": self._public_meta(
                tool="query_objects", target="", query_id=None,
                row_count=None, duration_ms=duration_ms,
                active_source=self.source,
                usage="带 object 参数查询数据;filters 为 属性→值 等值筛选",
            ),
        }

    # ---- propose_action("说"档)----

    def propose_action(
        self,
        object: str,
        action: str,
        conclusion: str,
        evidence: list[dict],
        *,
        context: EvidenceContext | None = None,
    ) -> dict:
        """生成结构化建议卡。不落任何写操作;每条依据须引用 query_id + result_digest。"""
        context = self._proposal_context(context)
        target = f"{object}.{action}"
        tpl = next((o for o in self.pack.objects if o.object == object), None)
        if tpl is None:
            self._proposal_reject(
                context=context,
                reason_code="unknown_target",
                target=target,
                detail="proposal object 未声明",
            )
            raise ValueError(f"未知对象 '{object}',可用:{sorted(self.pack.object_names())}")
        act = next((a for a in tpl.actions if a.name == action), None)
        if act is None:
            available = [f"{a.name}(档位:{a.tier})" for a in tpl.actions]
            self._proposal_reject(
                context=context,
                reason_code="unknown_target",
                target=target,
                detail="proposal action 未声明",
            )
            raise ValueError(f"{object} 未声明动作 '{action}',可用:{available or '(无)'}")
        if TIER_ORDER[act.tier] > TIER_ORDER[self.max_tier]:
            self._proposal_reject(
                context=context,
                reason_code="tier_forbidden",
                target=target,
                detail="proposal action 超出当前档位上限",
            )
            raise ValueError(
                f"动作 '{action}' 档位为「{act.tier}」,超出本网关档位上限「{self.max_tier}」;"
                "「做」档需审批流治理,不在开源网关范围")
        if not isinstance(conclusion, str) or not conclusion.strip():
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail="proposal conclusion 为空或类型非法",
            )
            raise ValueError("conclusion 不能为空:建议卡必须有明确结论")
        if not isinstance(evidence, list) or not evidence:
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail="proposal evidence 为空或类型非法",
            )
            raise ValueError(
                "evidence 不能为空:先调用 query_objects / query_metrics 取数,"
                "再以 [{claim, query_id, result_digest}] 引用其 meta")
        if len(evidence) > MAX_PROPOSAL_EVIDENCE_ITEMS:
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail="proposal evidence 条目数超出上限",
            )
            raise ValueError("invalid_params: evidence 条目数超出上限")
        if len(conclusion.strip()) > 1000:
            self._proposal_reject(
                context=context,
                reason_code="invalid_params",
                target=target,
                detail="proposal conclusion 超出长度限制",
            )
            raise ValueError("invalid_params: conclusion 超出长度限制")

        cited: list[dict[str, object]] = []
        caveats: list[str] = []
        dataset_versions: set[str] = set()
        for i, item in enumerate(evidence):
            validated, warnings = self._validate_cited_query(
                context=context, target=target, index=i, item=item,
            )
            dataset_versions.add(str(validated["dataset_version"]))
            cited.append(
                {
                    "claim": validated["claim"],
                    "query": {
                        "query_id": validated["query_id"],
                        "source": validated["source"],
                        "tool": validated["tool"],
                        "target": validated["target"],
                        "normalized_query": validated["normalized_query"],
                        "dataset_version": validated["dataset_version"],
                        "template_version": validated["template_version"],
                        "binding_hashes": validated["binding_hashes"],
                        "result_digest": validated["result_digest"],
                        "result_summary": validated["result_summary"],
                        "warnings": validated["warnings"],
                        "created_at": validated["created_at"],
                        "expires_at": validated["expires_at"],
                    },
                }
            )
            caveats.extend(warnings)

        if len(dataset_versions) != 1:
            self._proposal_reject(
                context=context,
                reason_code="dataset_version_mismatch",
                target=target,
                detail="proposal evidence 混用多个 dataset version",
            )
            raise ValueError("dataset_version_mismatch: proposal evidence 混用多个 dataset version")

        proposal_id = f"prp_{secrets.token_hex(12)}"
        at = self._iso_now()
        dataset_version = next(iter(dataset_versions))
        governance = "「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理"
        proposal_record = ProposalRecord(
            proposal_id=proposal_id,
            principal=context.principal,
            session_id=context.session_id,
            channel=context.channel,
            source=self.source,
            object=object,
            action=act.name,
            action_desc=act.desc,
            tier=act.tier,
            conclusion=conclusion.strip(),
            governance=governance,
            dataset_version=dataset_version,
            created_at=at,
        )
        snapshot_rows = [
            ProposalEvidenceRecord(
                proposal_id=proposal_id,
                evidence_ordinal=i,
                claim=str(item["claim"]),
                query_id=str(item["query"]["query_id"]),
                query_tool=str(item["query"]["tool"]),
                query_target=str(item["query"]["target"]),
                normalized_query_json=canonical_json_dumps(item["query"]["normalized_query"]),
                dataset_version=str(item["query"]["dataset_version"]),
                template_version=(
                    None if item["query"]["template_version"] is None
                    else str(item["query"]["template_version"])
                ),
                binding_hashes_json=canonical_json_dumps(item["query"]["binding_hashes"]),
                result_digest=str(item["query"]["result_digest"]),
                result_summary_json=canonical_json_dumps(item["query"]["result_summary"]),
                warnings_json=canonical_json_dumps(item["query"]["warnings"]),
                query_created_at=str(item["query"]["created_at"]),
            )
            for i, item in enumerate(cited)
        ]
        audit = GatewayAuditRecord(
            event_id=f"evt_{secrets.token_hex(12)}",
            created_at=at,
            principal=context.principal,
            session_id=context.session_id,
            channel=context.channel,
            source=self.source,
            operation="propose_action",
            target=target,
            outcome="ok",
            reason_code="ok",
            proposal_id=proposal_id,
            dataset_version=dataset_version,
            detail_json=canonical_json_dumps({"evidence_count": len(cited)}),
        )

        store = LandingStore(self.db_path)
        ev_store = EvidenceStore(store)
        try:
            store.con.execute("BEGIN IMMEDIATE")
            ev_store.insert_proposal(proposal_record, commit=False)
            ev_store.insert_proposal_evidence(snapshot_rows, commit=False)
            ev_store.insert_audit(audit, commit=False)
            store.con.commit()
        except sqlite3.Error as exc:
            store.con.rollback()
            raise ValueError(
                "evidence_store_unavailable: proposal evidence persist failed"
            ) from exc
        finally:
            store.con.close()

        self._audit({
            "tool": "propose_action", "proposal_id": proposal_id,
            "target": target, "detail": f"evidence={len(cited)}",
            "at": at,
        })
        return {
            "proposal_id": proposal_id,
            "at": at,
            "session_id": context.session_id,
            "source": self.source,
            "dataset_version": dataset_version,
            "object": object,
            "action": act.name,
            "action_desc": act.desc,
            "tier": act.tier,
            "conclusion": conclusion.strip(),
            "evidence": cited,
            "caveats": sorted({c for c in caveats if c}),
            "governance": governance,
        }

    # ---- query_metrics ----

    def query_metrics(
        self,
        metric: str | None = None,
        group_by: str | None = None,
        limit: int = 24,
        *,
        context: EvidenceContext | None = None,
    ) -> dict:
        started = time.perf_counter()
        if metric is not None and not isinstance(metric, str):
            raise ValueError("metric 须为字符串")
        if group_by is not None and not isinstance(group_by, str):
            raise ValueError("group_by 须为字符串")
        limit = self._require_int_limit(limit)
        if metric is None:
            return self._metric_catalog(started)

        # 实际指标读取以 published 冻结快照为准;磁盘模板仅用于目录展示。
        with self._published_tx() as (store, snap):
            mdef = next(
                (m for m in snap.template_pack.metrics if m.metric == metric), None,
            )
            if mdef is None:
                known = {m.metric for m in self.pack.metrics} | {
                    m.metric for m in snap.template_pack.metrics
                }
                if metric not in known:
                    raise ValueError(
                        f"未知指标 '{metric}',可用:{sorted(known)}"
                    )
                raise ValueError("not_published: 指标未包含在已发布数据集中")
            impl = self.metrics.get(metric)
            if impl is None:
                duration_ms = int((time.perf_counter() - started) * 1000)
                return {
                    "metric": mdef.metric, "display_name": mdef.display_name,
                    "status": mdef.status, "formula": mdef.formula,
                    "grain": mdef.grain, "caveats": mdef.caveats,
                    "freshness_sla": mdef.freshness_sla,
                    "implemented": False,
                    "reason": "依赖应收 / 回款对象,不在首批对象内;口径定义保留,待对象补齐后实现",
                    "meta": self._public_meta(
                        tool="query_metrics", target=metric, query_id=None,
                        row_count=None, duration_ms=duration_ms,
                        warnings=["指标尚未实现"],
                        dataset_version=snap.dataset_version,
                        template_version=snap.template_version,
                    ),
                }

            dim = group_by or impl.default_dim
            if dim not in impl.dims:
                raise ValueError(
                    f"'{metric}' 支持的 group_by:{sorted(impl.dims)},got '{dim}'"
                )
            order = '"group" DESC' if dim == "月" else "value DESC"
            limit_n = max(1, min(int(limit), 200))
            tables = {
                name: snap.objects[name].physical_table
                for name in impl.depends_on
                if name in snap.objects
            }
            sql = impl.render_sql(tables, dim=impl.dims[dim], order=order)
            rows = [dict(r) for r in _safe_execute(store.con, sql, (limit_n,))]
            dataset_version = snap.dataset_version
            template_version = snap.template_version
            binding_hashes = {
                name: snap.objects[name].binding_hash for name in impl.depends_on
            }
            definition = {
                "metric": mdef.metric, "display_name": mdef.display_name,
                "status": mdef.status, "formula": mdef.formula,
                "grain": mdef.grain, "caveats": mdef.caveats,
                "freshness_sla": mdef.freshness_sla,
            }
            status = mdef.status
            caveats = mdef.caveats
            dim_out = dim

        warning = "口径为 draft(未经校准),数值仅供演示环境参考" if status != "certified" else ""
        warnings = [w for w in (warning, caveats) if w]
        context = self._resolve_context(context)
        persisted = self._persist_query_evidence(
            context=context,
            tool="query_metrics",
            target=metric,
            normalized_query=normalize_query_metrics(
                source=self.source,
                metric=metric,
                group_by=dim_out,
                limit=limit,
            ),
            response_payload={
                **definition,
                "implemented": True,
                "unit": impl.unit,
                "group_by": dim_out,
                "rows": rows,
            },
            dataset_version=dataset_version,
            template_version=template_version,
            binding_hashes=binding_hashes,
            warnings=warnings,
            row_count=len(rows),
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            **definition, "implemented": True, "unit": impl.unit,
            "group_by": dim_out, "rows": rows,
            "meta": self._public_meta(
                tool="query_metrics", target=metric, query_id=str(persisted["query_id"]),
                row_count=len(rows), duration_ms=duration_ms,
                session_id=str(persisted["session_id"]),
                result_digest=str(persisted["result_digest"]),
                result_summary=dict(persisted["result_summary"]),
                created_at=str(persisted["created_at"]),
                expires_at=str(persisted["expires_at"]),
                warnings=warnings, warning=warning,
                dataset_version=dataset_version,
                template_version=template_version,
                binding_hashes=binding_hashes,
            ),
        }

    def _metric_catalog(self, started: float | None = None) -> dict:
        t0 = time.perf_counter() if started is None else started
        duration_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "metrics": [
                {
                    "metric": m.metric, "display_name": m.display_name, "status": m.status,
                    "formula": m.formula, "grain": m.grain, "caveats": m.caveats,
                    "implemented": self.metrics.get(m.metric) is not None,
                    **({"group_by_options": sorted(self.metrics[m.metric].dims)}
                       if self.metrics.get(m.metric) else {}),
                }
                for m in self.pack.metrics
            ],
            "meta": self._public_meta(
                tool="query_metrics", target="", query_id=None,
                row_count=None, duration_ms=duration_ms,
                usage="带 metric 参数取数;group_by 见各指标 group_by_options",
            ),
        }
