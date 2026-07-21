"""映射 Preview:只读样本冻结 + 分析编排(M3-T03/T04)。

在显式 SQLite 读事务内按锚表 DDL 主键稳定排序、过滤软删/批次、冻结主键集合,
计算 sample_fingerprint;current/candidate 双跑共用同一 FrozenSample。

T04 在同一事务内完成草稿预检、evaluate 双跑、聚合、diff 与脱敏;不写业务表、
不落盘草稿。HTTP/鉴权由 T05 接入。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Mapping

from ..mapping import build_select, parse_field_expr
from ..metamodel.schema import (
    DeriveRule,
    DerivedField,
    ObjectTemplate,
    SourceBinding,
    TemplatePack,
)
from ..metamodel.versioning import binding_hash
from .landing import LandingStore, raw_table_name
from .mapping_transform import (
    DEFAULT_BREAKER_THRESHOLD,
    RowEvaluation,
    TransformEvaluation,
    TransformIssue,
    evaluate_object_rows,
    would_trip_breaker,
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAMPLE_LIMIT_MIN, _SAMPLE_LIMIT_MAX = 1, 200
_SAMPLE_OFFSET_MIN, _SAMPLE_OFFSET_MAX = 0, 10000
_ACTIVE_COL = "_d2a_deleted_at"
_BATCH_COL = "_d2a_batch_id"
_ROW_HASH_COL = "_d2a_row_hash"

MASKED = "***"

SAMPLE_DUPLICATE_WARNING = "业务键重复仅覆盖本次样本,样本外重复未检查"

PreviewSampleReason = str  # sample_batch_not_found | sample_invalid | raw_table_not_found | anchor_changed

PreviewReasonCode = Literal[
    "object_not_found",
    "source_not_found",
    "current_binding_unavailable",
    "draft_invalid",
    "anchor_changed",
    "sample_batch_not_found",
    "sample_invalid",
    "raw_table_not_found",
    "raw_unavailable",
]


class PreviewError(Exception):
    """Preview 失败;reason_code 供 T05 映射 HTTP 状态。"""

    def __init__(self, reason_code: PreviewReasonCode | str, detail: str):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


class PreviewSampleError(PreviewError):
    """样本冻结失败(T03);兼容旧导入名。"""


@dataclass(frozen=True)
class FrozenSample:
    source: str
    anchor_table: str
    pk_cols: tuple[str, ...]
    pk_tuples: tuple[tuple, ...]
    sample_batch_ids: tuple[str, ...]
    fingerprint: str
    sampled_rows: int
    offset: int
    limit: int
    requested_batch_id: str | None


@dataclass
class PreviewIssueView:
    reason_code: str
    field: str | None
    detail: str
    source_value: str | None = None


@dataclass
class PreviewRowView:
    sample_row_id: str
    status: Literal["mapped", "quarantined"]
    output: dict[str, Any] = field(default_factory=dict)
    issues: list[PreviewIssueView] = field(default_factory=list)


@dataclass
class PreviewSummaryView:
    total: int
    mapped: int
    quarantined: int
    quarantine_rate: float
    would_trip_breaker: bool


@dataclass
class PreviewEnumGapView:
    field: str
    source_value: str
    count: int


@dataclass
class PreviewBusinessKeyIssuesView:
    missing: int
    duplicate: int
    scope: Literal["sample"] = "sample"


@dataclass
class PreviewDerivedRuleHitView:
    index: int
    hit_count: int


@dataclass
class PreviewDerivedCoverageView:
    field: str
    eligible_rows: int
    matched_rows: int
    default_hits: int
    unmatched_rows: int
    row_coverage: float | None
    rules_total: int
    rules_hit: int
    rules: list[PreviewDerivedRuleHitView] = field(default_factory=list)


@dataclass
class PreviewEvaluationView:
    summary: PreviewSummaryView
    rows: list[PreviewRowView] = field(default_factory=list)
    enum_gaps: list[PreviewEnumGapView] = field(default_factory=list)
    business_key_issues: PreviewBusinessKeyIssuesView = field(
        default_factory=lambda: PreviewBusinessKeyIssuesView(0, 0),
    )
    derived_coverage: list[PreviewDerivedCoverageView] = field(default_factory=list)


@dataclass
class PreviewSampleInfoView:
    anchor_table: str
    offset: int
    limit: int
    requested_batch_id: str | None
    sample_batch_ids: list[str]
    sampled_rows: int
    sample_fingerprint: str


@dataclass
class PreviewDiffFieldView:
    field: str
    before: Any | None
    after: Any | None


@dataclass
class PreviewDiffRowView:
    sample_row_id: str
    status_before: Literal["mapped", "quarantined"] | None
    status_after: Literal["mapped", "quarantined"] | None
    fields: list[PreviewDiffFieldView] = field(default_factory=list)


@dataclass
class PreviewDiffSummaryView:
    rows_changed: int
    status_changed: int
    fields_changed: int


@dataclass
class PreviewDiffView:
    state: Literal["available", "unavailable"]
    reason: Literal["no_current_binding"] | None
    summary: PreviewDiffSummaryView
    rows: list[PreviewDiffRowView] = field(default_factory=list)


@dataclass
class PreviewResult:
    """对齐 MappingPreviewResponse 字段的核心出口(脱敏后)。"""

    object: str
    source: str
    mode: Literal["current", "draft"]
    template_version: str
    current_binding_hash: str | None
    candidate_binding_hash: str
    sample: PreviewSampleInfoView
    current: PreviewEvaluationView | None
    candidate: PreviewEvaluationView
    diff: PreviewDiffView
    warnings: list[str]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _require_ident(name: str, *, label: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise PreviewSampleError("sample_invalid", f"非法{label}")
    return name


def _require_preview_read_tx(store: LandingStore) -> None:
    if getattr(store, "_preview_read_depth", 0) < 1:
        raise PreviewSampleError(
            "sample_invalid",
            "freeze_sample/load_sample_rows 须在 preview_read_tx 内调用",
        )


def _validate_bounds(offset: int, limit: int) -> tuple[int, int]:
    # bool 是 int 子类;显式拒绝 True/False 避免当成 0/1。
    if isinstance(offset, bool) or isinstance(limit, bool):
        raise PreviewSampleError("sample_invalid", "offset/limit 须为整数")
    if type(offset) is not int or type(limit) is not int:
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
    _require_preview_read_tx(store)
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
        source=source,
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
    _require_preview_read_tx(store)
    source = _require_ident(source, label="source")
    if source != sample.source:
        raise PreviewSampleError("sample_invalid", "source 与冻结样本不一致")
    if not binding.tables or binding.tables[0] != sample.anchor_table:
        raise PreviewSampleError("anchor_changed", "binding 锚表与冻结样本不一致")
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

    missing = [pk for pk in sample.pk_tuples if pk not in by_pk]
    if missing:
        raise PreviewSampleError(
            "anchor_changed",
            "冻结主键在加载时缺失,样本已漂移",
        )
    return [by_pk[pk] for pk in sample.pk_tuples]


# ---- T04: 草稿预检 / 聚合 / diff / 脱敏 ----


def _sources_from_pack(pack: TemplatePack) -> set[str]:
    return {
        binding.source
        for tpl in pack.objects
        for binding in tpl.bindings
        if binding.enabled
    }


def _allowed_raw_tables(pack: TemplatePack, source: str) -> set[str]:
    return {
        table
        for tpl in pack.objects
        for binding in tpl.bindings
        if binding.enabled and binding.source == source
        for table in binding.tables
    }


def _find_object(pack: TemplatePack, object_name: str) -> ObjectTemplate:
    for tpl in pack.objects:
        if tpl.object == object_name:
            return tpl
    raise PreviewError("object_not_found", f"对象 '{object_name}' 不存在")


def _current_binding(tpl: ObjectTemplate, source: str) -> SourceBinding | None:
    return next(
        (b for b in tpl.bindings if b.source == source and b.enabled),
        None,
    )


def _coerce_draft_payload(draft: Any) -> dict[str, Any]:
    if draft is None:
        raise PreviewError("draft_invalid", "草稿不能为空")
    if hasattr(draft, "model_dump"):
        payload = draft.model_dump()
    elif isinstance(draft, Mapping):
        payload = dict(draft)
    else:
        raise PreviewError("draft_invalid", "草稿格式无效")
    if "status" in payload:
        raise PreviewError("draft_invalid", "草稿不得伪造 status")
    return payload


def _build_draft_derived(raw_derived: Any) -> dict[str, DerivedField]:
    if raw_derived is None:
        return {}
    if not isinstance(raw_derived, Mapping):
        raise PreviewError("draft_invalid", "derived 须为对象")
    out: dict[str, DerivedField] = {}
    try:
        for name, spec in raw_derived.items():
            if hasattr(spec, "model_dump"):
                spec = spec.model_dump()
            if not isinstance(spec, Mapping):
                raise PreviewError("draft_invalid", f"derived.{name} 格式无效")
            rules_in = spec.get("rules") or []
            rules: list[DeriveRule] = []
            for rule in rules_in:
                if hasattr(rule, "model_dump"):
                    rule = rule.model_dump()
                if not isinstance(rule, Mapping):
                    raise PreviewError("draft_invalid", f"derived.{name} 规则格式无效")
                rules.append(DeriveRule(when=dict(rule.get("when") or {}), value=rule["value"]))
            out[str(name)] = DerivedField(rules=rules, default=spec.get("default"))
    except PreviewError:
        raise
    except Exception as exc:
        raise PreviewError("draft_invalid", f"derived 校验失败: {exc}") from exc
    return out


def build_draft_binding(
    source: str,
    draft: Any,
    *,
    pack: TemplatePack,
    store: LandingStore,
    current: SourceBinding | None,
    template: ObjectTemplate,
) -> SourceBinding:
    """构造 status=draft 的一次性 SourceBinding 并做白名单/锚表/表达式预检。"""
    payload = _coerce_draft_payload(draft)
    tables = payload.get("tables")
    if not isinstance(tables, list) or not tables:
        raise PreviewError("draft_invalid", "草稿 tables 不能为空")
    if any(not isinstance(t, str) or not t for t in tables):
        raise PreviewError("draft_invalid", "草稿表名无效")

    whitelist = _allowed_raw_tables(pack, source)
    # 无任何模板声明表时,仍允许仅引用当前对象既有 binding 表(若有)。
    if current is not None:
        whitelist = set(whitelist) | set(current.tables)
    for table in tables:
        if table not in whitelist:
            raise PreviewError("draft_invalid", f"表 '{table}' 不在源白名单内")
        if not _table_exists(store, raw_table_name(source, table)):
            raise PreviewError("draft_invalid", f"表 '{table}' 尚未落地")

    if current is not None and tables[0] != current.tables[0]:
        raise PreviewError("anchor_changed", "草稿不得变更锚表 tables[0]")

    key_map = payload.get("key_map") or {}
    field_map = payload.get("field_map") or {}
    if not isinstance(key_map, Mapping) or not isinstance(field_map, Mapping):
        raise PreviewError("draft_invalid", "key_map/field_map 须为对象")

    table_set = set(tables)
    for label, mapping in (("key_map", key_map), ("field_map", field_map)):
        for prop, expr in mapping.items():
            if not isinstance(expr, str):
                raise PreviewError("draft_invalid", f"{label}.{prop} 须为字符串表达式")
            try:
                parsed = parse_field_expr(expr)
            except ValueError as exc:
                raise PreviewError("draft_invalid", str(exc)) from exc
            if parsed.table not in table_set:
                raise PreviewError(
                    "draft_invalid",
                    f"{label}.{prop} 引用表 '{parsed.table}' 不在草稿 tables 内",
                )
            if parsed.join_fk is not None and parsed.join_fk[0] not in table_set:
                raise PreviewError(
                    "draft_invalid",
                    f"{label}.{prop} join 表 '{parsed.join_fk[0]}' 不在草稿 tables 内",
                )

    derived = _build_draft_derived(payload.get("derived") or {})
    prop_names = {p.name for p in template.properties}
    for prop_name, spec in derived.items():
        if prop_name not in prop_names:
            raise PreviewError("draft_invalid", f"derived 属性 '{prop_name}' 不在对象属性中")
        prop = next(p for p in template.properties if p.name == prop_name)
        if prop.type == "enum":
            values = [r.value for r in spec.rules]
            if spec.default is not None:
                values.append(spec.default)
            bad = [v for v in values if v not in prop.enum_values]
            if bad:
                raise PreviewError(
                    "draft_invalid",
                    f"derived.{prop_name} 取值 {bad} 不在枚举内",
                )

    watermark = payload.get("watermark")
    if watermark is not None and not isinstance(watermark, str):
        raise PreviewError("draft_invalid", "watermark 须为字符串或 null")
    if isinstance(watermark, str) and watermark:
        try:
            parsed_wm = parse_field_expr(watermark)
        except ValueError as exc:
            raise PreviewError("draft_invalid", str(exc)) from exc
        if parsed_wm.table not in table_set:
            raise PreviewError("draft_invalid", "watermark 引用表不在草稿 tables 内")

    notes = payload.get("notes") or ""
    if not isinstance(notes, str):
        raise PreviewError("draft_invalid", "notes 须为字符串")

    try:
        return SourceBinding(
            source=source,
            tables=list(tables),
            status="draft",
            key_map={str(k): str(v) for k, v in key_map.items()},
            field_map={str(k): str(v) for k, v in field_map.items()},
            derived=derived,
            watermark=watermark,
            notes=notes,
        )
    except Exception as exc:
        raise PreviewError("draft_invalid", f"草稿 binding 无效: {exc}") from exc


def _sensitive_props(template: ObjectTemplate) -> set[str]:
    return {p.name for p in template.properties if p.sensitive}


def _template_prop_names(template: ObjectTemplate) -> list[str]:
    return [p.name for p in template.properties]


def _mask_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: MASKED for k in value}
    if isinstance(value, list):
        return [MASKED for _ in value]
    return MASKED


def _safe_detail(issue: TransformIssue, sensitive: set[str]) -> str:
    """对外 detail:敏感字段不嵌入原值。"""
    field = issue.field
    if field is not None and field in sensitive:
        code = issue.reason_code
        if code == "enum_unmapped":
            return f"{field}: 源码值未在 map 中声明"
        if code == "enum_invalid":
            return f"{field}: 取值不在枚举内"
        if code == "type_coercion":
            return f"{field}: 类型转换失败"
        if code == "derived_unmatched":
            return f"{field}: 派生规则无匹配"
        if code == "derived_invalid_enum":
            return f"{field}: 派生值不在枚举内"
        return f"{field}: 映射失败"
    if issue.reason_code in ("business_key_missing", "business_key_duplicate"):
        # 键字典可能含敏感属性:对敏感键遮罩后再渲染。
        src = issue.source_value
        if isinstance(src, dict):
            masked = {
                k: (MASKED if k in sensitive else v)
                for k, v in src.items()
            }
            prefix = "业务键缺失" if issue.reason_code == "business_key_missing" else "业务键重复"
            return f"{prefix}:{masked}"
    return issue.detail


def _issue_source_value_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_source_value(issue: TransformIssue, sensitive: set[str]) -> str | None:
    if issue.source_value is None:
        return None
    field = issue.field
    if field is not None and field in sensitive:
        return _issue_source_value_str(_mask_value(issue.source_value))
    if isinstance(issue.source_value, dict):
        if any(k in sensitive for k in issue.source_value):
            masked = {
                k: (MASKED if k in sensitive else v)
                for k, v in issue.source_value.items()
            }
            return _issue_source_value_str(masked)
    return _issue_source_value_str(issue.source_value)


def _mask_output(output: dict | None, prop_names: list[str], sensitive: set[str]) -> dict[str, Any]:
    if not output:
        return {}
    out: dict[str, Any] = {}
    for name in prop_names:
        if name not in output:
            continue
        value = output[name]
        out[name] = MASKED if name in sensitive else _json_norm(value)
    return out


def _json_norm(value: Any) -> Any:
    """规范化 JSON 类型便于 diff 比较与响应出口。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return MASKED
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _enum_gap_value(source_value: Any, *, sensitive: bool) -> str:
    if sensitive:
        return MASKED
    if source_value is None:
        return "null"
    if isinstance(source_value, str):
        return source_value
    return json.dumps(source_value, ensure_ascii=False, default=str)


def _build_evaluation_view(
    *,
    template: ObjectTemplate,
    binding: SourceBinding,
    evaluation: TransformEvaluation,
    sample: FrozenSample,
    sensitive: set[str],
    breaker_threshold: float,
) -> PreviewEvaluationView:
    prop_names = _template_prop_names(template)
    rows_out: list[PreviewRowView] = []
    enum_counts: dict[tuple[str, str], int] = {}
    missing = 0
    duplicate = 0

    for ordinal, (pk, row_eval) in enumerate(zip(sample.pk_tuples, evaluation.rows)):
        sample_row_id = make_sample_row_id(ordinal, pk)
        issues_out: list[PreviewIssueView] = []
        for issue in row_eval.issues:
            issues_out.append(
                PreviewIssueView(
                    reason_code=issue.reason_code,
                    field=issue.field,
                    detail=_safe_detail(issue, sensitive),
                    source_value=_safe_source_value(issue, sensitive),
                )
            )
            if issue.reason_code == "enum_unmapped" and issue.field is not None:
                # 按原始源值聚合,出口再遮罩 — 避免不同源值合并成一条 ***。
                raw_key = _enum_gap_value(issue.source_value, sensitive=False)
                key = (issue.field, raw_key)
                enum_counts[key] = enum_counts.get(key, 0) + 1
            if issue.reason_code == "business_key_missing":
                missing += 1
            elif issue.reason_code == "business_key_duplicate":
                duplicate += 1

        rows_out.append(
            PreviewRowView(
                sample_row_id=sample_row_id,
                status=row_eval.status,
                output=_mask_output(row_eval.output, prop_names, sensitive),
                issues=issues_out,
            )
        )

    # 空样本:evaluation.rows 可能为空而 pk_tuples 也为空。
    if not sample.pk_tuples and not evaluation.rows:
        rows_out = []

    total = evaluation.total
    mapped = evaluation.mapped
    quarantined = evaluation.quarantined
    rate = (quarantined / total) if total else 0.0
    summary = PreviewSummaryView(
        total=total,
        mapped=mapped,
        quarantined=quarantined,
        quarantine_rate=rate,
        would_trip_breaker=would_trip_breaker(quarantined, total, breaker_threshold),
    )
    enum_gaps = [
        PreviewEnumGapView(
            field=field,
            source_value=MASKED if field in sensitive else value,
            count=count,
        )
        for (field, value), count in sorted(enum_counts.items())
    ]
    derived_coverage = _aggregate_derived_coverage(binding, evaluation.rows)
    return PreviewEvaluationView(
        summary=summary,
        rows=rows_out,
        enum_gaps=enum_gaps,
        business_key_issues=PreviewBusinessKeyIssuesView(
            missing=missing,
            duplicate=duplicate,
            scope="sample",
        ),
        derived_coverage=derived_coverage,
    )


def _aggregate_derived_coverage(
    binding: SourceBinding,
    rows: list[RowEvaluation],
) -> list[PreviewDerivedCoverageView]:
    out: list[PreviewDerivedCoverageView] = []
    for field_name, spec in binding.derived.items():
        rules_total = len(spec.rules)
        hit_counts = [0] * rules_total
        eligible = matched = default_hits = unmatched = 0
        for row in rows:
            hits = [h for h in row.derived_hits if h.field == field_name]
            if not hits:
                continue
            eligible += 1
            hit = hits[0]
            if hit.outcome == "rule":
                matched += 1
                if hit.rule_index is not None and 0 <= hit.rule_index < rules_total:
                    hit_counts[hit.rule_index] += 1
            elif hit.outcome == "default":
                default_hits += 1
            else:
                unmatched += 1
        assert matched + default_hits + unmatched == eligible
        row_coverage = (
            (matched + default_hits) / eligible if eligible else None
        )
        rules_hit = sum(1 for c in hit_counts if c > 0)
        out.append(
            PreviewDerivedCoverageView(
                field=field_name,
                eligible_rows=eligible,
                matched_rows=matched,
                default_hits=default_hits,
                unmatched_rows=unmatched,
                row_coverage=row_coverage,
                rules_total=rules_total,
                rules_hit=rules_hit,
                rules=[
                    PreviewDerivedRuleHitView(index=i, hit_count=c)
                    for i, c in enumerate(hit_counts)
                ],
            )
        )
    return out


def _values_equal(a: Any, b: Any) -> bool:
    return _json_norm(a) == _json_norm(b)


def _build_diff(
    *,
    template: ObjectTemplate,
    before_eval: TransformEvaluation | None,
    after_eval: TransformEvaluation,
    sample: FrozenSample,
    sensitive: set[str],
) -> PreviewDiffView:
    empty_summary = PreviewDiffSummaryView(0, 0, 0)
    if before_eval is None:
        return PreviewDiffView(
            state="unavailable",
            reason="no_current_binding",
            summary=empty_summary,
            rows=[],
        )

    prop_names = _template_prop_names(template)
    diff_rows: list[PreviewDiffRowView] = []
    rows_changed = status_changed = fields_changed = 0

    for ordinal, pk in enumerate(sample.pk_tuples):
        before = before_eval.rows[ordinal]
        after = after_eval.rows[ordinal]
        sample_row_id = make_sample_row_id(ordinal, pk)
        field_diffs: list[PreviewDiffFieldView] = []
        before_out = before.output or {}
        after_out = after.output or {}
        for name in prop_names:
            bv = before_out.get(name) if before.status == "mapped" else None
            av = after_out.get(name) if after.status == "mapped" else None
            # 任一侧隔离则该属性视为缺失(None),与 mapped 输出比较。
            if before.status != "mapped":
                bv = None
            if after.status != "mapped":
                av = None
            if _values_equal(bv, av):
                continue
            field_diffs.append(
                PreviewDiffFieldView(
                    field=name,
                    before=MASKED if name in sensitive and bv is not None else _json_norm(bv),
                    after=MASKED if name in sensitive and av is not None else _json_norm(av),
                )
            )
        status_differs = before.status != after.status
        if not status_differs and not field_diffs:
            continue
        rows_changed += 1
        if status_differs:
            status_changed += 1
        fields_changed += len(field_diffs)
        diff_rows.append(
            PreviewDiffRowView(
                sample_row_id=sample_row_id,
                status_before=before.status,
                status_after=after.status,
                fields=field_diffs,
            )
        )

    return PreviewDiffView(
        state="available",
        reason=None,
        summary=PreviewDiffSummaryView(
            rows_changed=rows_changed,
            status_changed=status_changed,
            fields_changed=fields_changed,
        ),
        rows=diff_rows,
    )


def _run_binding_eval(
    store: LandingStore,
    template: ObjectTemplate,
    binding: SourceBinding,
    sample: FrozenSample,
    *,
    source: str,
) -> TransformEvaluation:
    """按冻结主键加载行并 evaluate;保留 __col 供 derived 使用。"""
    _require_preview_read_tx(store)
    if source != sample.source:
        raise PreviewSampleError("sample_invalid", "source 与冻结样本不一致")
    if not binding.tables or binding.tables[0] != sample.anchor_table:
        raise PreviewSampleError("anchor_changed", "binding 锚表与冻结样本不一致")
    if not sample.pk_tuples:
        # 仍解析 exprs,保持与非空路径一致的失败模式(非法表达式等)。
        _, _, exprs = build_select(
            template,
            binding,
            limit=None,
            physical=lambda t: raw_table_name(source, t),
            active_col=_ACTIVE_COL,
        )
        return evaluate_object_rows(template, binding, [], exprs)

    derive_cols = sorted({
        col
        for spec in binding.derived.values()
        for rule in spec.rules
        for col in rule.when
    })
    pk_extras = list(dict.fromkeys([*derive_cols, *sample.pk_cols]))
    sql, params, exprs = build_select(
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
        by_pk[pk] = row
    missing = [pk for pk in sample.pk_tuples if pk not in by_pk]
    if missing:
        raise PreviewSampleError("anchor_changed", "冻结主键在加载时缺失,样本已漂移")
    ordered = [by_pk[pk] for pk in sample.pk_tuples]
    return evaluate_object_rows(template, binding, ordered, exprs)


def preview_mapping(
    store: LandingStore,
    pack: TemplatePack,
    *,
    object_name: str,
    source: str,
    offset: int = 0,
    limit: int = 50,
    batch_id: str | None = None,
    draft_binding: Any | None = None,
    breaker_threshold: float = DEFAULT_BREAKER_THRESHOLD,
    allowed_sources: set[str] | list[str] | None = None,
) -> PreviewResult:
    """编排 Preview 分析:草稿预检、样本冻结、current/candidate 双跑、聚合与脱敏。

    须在可写或只读 LandingStore 上调用;内部使用 preview_read_tx。
    推荐 T05 以 LandingStore.open_readonly 打开连接。
    """
    tpl = _find_object(pack, object_name)
    allowed = set(allowed_sources) if allowed_sources is not None else _sources_from_pack(pack)
    if source not in allowed:
        raise PreviewError("source_not_found", f"未知或不允许的数据源 '{source}'")

    current = _current_binding(tpl, source)
    if draft_binding is None and current is None:
        raise PreviewError(
            "current_binding_unavailable",
            "无当前 binding 且未提供草稿",
        )

    with preview_read_tx(store):
        draft: SourceBinding | None = None
        if draft_binding is not None:
            draft = build_draft_binding(
                source,
                draft_binding,
                pack=pack,
                store=store,
                current=current,
                template=tpl,
            )

        candidate_binding = draft if draft is not None else current
        assert candidate_binding is not None
        mode: Literal["current", "draft"] = "draft" if draft is not None else "current"

        anchor = candidate_binding.tables[0] if current is None else current.tables[0]
        if not candidate_binding.tables:
            raise PreviewError("draft_invalid", "binding tables 不能为空")

        sample = freeze_sample(
            store,
            source=source,
            anchor_table=anchor,
            offset=offset,
            limit=limit,
            batch_id=batch_id,
        )

        sensitive = _sensitive_props(tpl)
        current_eval: TransformEvaluation | None = None
        if current is not None:
            current_eval = _run_binding_eval(
                store, tpl, current, sample, source=source,
            )

        if draft is not None:
            candidate_eval = _run_binding_eval(
                store, tpl, draft, sample, source=source,
            )
        else:
            assert current_eval is not None
            candidate_eval = current_eval

        current_view = (
            _build_evaluation_view(
                template=tpl,
                binding=current,
                evaluation=current_eval,
                sample=sample,
                sensitive=sensitive,
                breaker_threshold=breaker_threshold,
            )
            if current is not None and current_eval is not None
            else None
        )
        candidate_view = _build_evaluation_view(
            template=tpl,
            binding=candidate_binding,
            evaluation=candidate_eval,
            sample=sample,
            sensitive=sensitive,
            breaker_threshold=breaker_threshold,
        )
        diff = _build_diff(
            template=tpl,
            before_eval=current_eval,
            after_eval=candidate_eval,
            sample=sample,
            sensitive=sensitive,
        )

        warnings = [SAMPLE_DUPLICATE_WARNING]
        return PreviewResult(
            object=object_name,
            source=source,
            mode=mode,
            template_version=pack.version,
            current_binding_hash=binding_hash(current) if current is not None else None,
            candidate_binding_hash=binding_hash(candidate_binding),
            sample=PreviewSampleInfoView(
                anchor_table=sample.anchor_table,
                offset=sample.offset,
                limit=sample.limit,
                requested_batch_id=sample.requested_batch_id,
                sample_batch_ids=list(sample.sample_batch_ids),
                sampled_rows=sample.sampled_rows,
                sample_fingerprint=sample.fingerprint,
            ),
            current=current_view,
            candidate=candidate_view,
            diff=diff,
            warnings=warnings,
        )
