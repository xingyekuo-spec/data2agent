"""字段级血缘:规范对象键、key token 与值证据序列化。

M4-T01 先冻结 key token 与 ValueEvidence 口径;持久化与 published 查询在后续任务接入。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

_LINEAGE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_KEY_SCALARS = (type(None), str, int, float, bool)


class LineageKeyError(ValueError):
    """对象键或 key token 不合法。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


def is_valid_lineage_key_token(token: str) -> bool:
    """URL `{key}` 必须是规范 64 位小写 hex SHA-256。"""
    return isinstance(token, str) and bool(_LINEAGE_KEY_RE.fullmatch(token))


def _require_key_scalar(value: Any, *, label: str) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124
            raise LineageKeyError(
                "lineage_key_invalid", f"{label} 不能是非有限浮点")
        return value
    raise LineageKeyError(
        "lineage_key_invalid",
        f"{label} 只允许 null/string/number/bool,得到 {type(value).__name__}",
    )


def canonical_object_key_pairs(
    keys: Sequence[str],
    values: Mapping[str, Any] | Sequence[Any],
) -> list[list[Any]]:
    """按 frozen template `keys` 顺序生成规范化 pair 数组。

    `values` 可以是业务键映射,或与 `keys` 等长的值序列。
    """
    if not keys:
        raise LineageKeyError("lineage_key_invalid", "对象没有业务键")
    if isinstance(values, Mapping):
        pairs = []
        for name in keys:
            if name not in values:
                raise LineageKeyError(
                    "lineage_key_invalid", f"缺少业务键 {name}")
            pairs.append([name, _require_key_scalar(values[name], label=name)])
        return pairs
    if len(values) != len(keys):
        raise LineageKeyError(
            "lineage_key_invalid",
            f"业务键值数量不匹配:期望 {len(keys)},得到 {len(values)}",
        )
    return [
        [name, _require_key_scalar(value, label=name)]
        for name, value in zip(keys, values, strict=True)
    ]


def canonical_object_key_json(
    keys: Sequence[str],
    values: Mapping[str, Any] | Sequence[Any],
) -> str:
    """紧凑 UTF-8 JSON pair 数组,例如 [["order_no","SO-001"],["line_no",10]]。"""
    pairs = canonical_object_key_pairs(keys, values)
    return json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))


def object_key_token(
    keys: Sequence[str],
    values: Mapping[str, Any] | Sequence[Any],
) -> str:
    """完整 SHA-256 hex(64 位小写),用作 URL 不透明定位符。"""
    raw = canonical_object_key_json(keys, values).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def object_key_token_from_pairs(pairs: Sequence[Sequence[Any]]) -> str:
    """已规范化 pair 数组 → key token(仍校验标量类型)。"""
    normalized: list[list[Any]] = []
    for item in pairs:
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
            raise LineageKeyError("lineage_key_invalid", "pair 必须是 [name, value]")
        if len(item) != 2:
            raise LineageKeyError("lineage_key_invalid", "pair 必须是二元组")
        name, value = item[0], item[1]
        if not isinstance(name, str) or not name:
            raise LineageKeyError("lineage_key_invalid", "业务键名必须是非空字符串")
        normalized.append([name, _require_key_scalar(value, label=name)])
    raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def require_lineage_key_token(token: str) -> str:
    """校验 URL key token;非法时抛 LineageKeyError(lineage_key_invalid)。"""
    if not is_valid_lineage_key_token(token):
        raise LineageKeyError(
            "lineage_key_invalid",
            "key token 必须是规范 64 位小写十六进制 SHA-256",
        )
    return token


# ---- ValueEvidence / 持久化 DTO (M4-T04) ------------------------------------

LINEAGE_SCHEMA_VERSION = 1
VALUE_BUDGET_BYTES = 64 * 1024
VALUE_PREVIEW_CHARS = 512

TransformKind = Literal["direct", "derived", "unmapped"]


@dataclass(frozen=True)
class FieldLineageNode:
    dataset_version: str
    object_version: str
    object: str
    object_key_json: str
    object_key_hash: str
    property: str
    result_value_json: str
    trace_status: Literal["available", "unavailable"]
    unavailable_reason: str | None
    transform_kind: TransformKind
    transform_steps_json: str
    source: str
    map_batch_id: str
    binding_hash: str
    binding_status: str
    template_version: str


@dataclass(frozen=True)
class FieldLineageInputRow:
    dataset_version: str
    object: str
    object_key_json: str
    property: str
    input_ordinal: int
    role: Literal["value", "join_fk", "derived_condition"]
    source: str | None
    source_table: str | None
    source_pk_json: str | None
    source_column: str | None
    source_value_json: str | None
    extract_batch_id: str | None
    join_json: str | None


def encode_value_evidence(value: Any) -> dict[str, Any]:
    """稳定值证据 JSON(非 repr)。BLOB/超长文本只留摘要。"""
    if value is None:
        return {"kind": "null", "value": None, "preview": None, "sha256": None, "length": None}
    if isinstance(value, bool) or isinstance(value, (int, float)):
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))  # noqa: PLR0124
        ):
            text = str(value)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return {
                "kind": "truncated",
                "value": None,
                "preview": text[:VALUE_PREVIEW_CHARS],
                "sha256": digest,
                "length": len(text),
            }
        return {
            "kind": "scalar",
            "value": value,
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, datetime):
        text = value.isoformat()
        return {
            "kind": "scalar",
            "value": text,
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, date):
        return {
            "kind": "scalar",
            "value": value.isoformat(),
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, Decimal):
        return {
            "kind": "scalar",
            "value": float(value),
            "preview": None,
            "sha256": None,
            "length": None,
        }
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "kind": "bytes",
            "value": None,
            "preview": None,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "length": len(raw),
        }
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= VALUE_BUDGET_BYTES:
            return {
                "kind": "scalar",
                "value": value,
                "preview": None,
                "sha256": None,
                "length": None,
            }
        return {
            "kind": "truncated",
            "value": None,
            "preview": value[:VALUE_PREVIEW_CHARS],
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "length": len(encoded),
        }
    # 其它类型:安全字符串化并按预算截断
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= VALUE_BUDGET_BYTES:
        return {
            "kind": "scalar",
            "value": text,
            "preview": None,
            "sha256": None,
            "length": None,
        }
    return {
        "kind": "truncated",
        "value": None,
        "preview": text[:VALUE_PREVIEW_CHARS],
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "length": len(encoded),
    }


def dumps_value_evidence(value: Any) -> str:
    return json.dumps(
        encode_value_evidence(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---- apply 原子写入 (M4-T05) --------------------------------------------------

from collections.abc import Sequence as _Seq  # noqa: E402


@dataclass(frozen=True)
class ApplyVersionContext:
    """正式 apply 版本上下文;由 build_dataset 创建并传入 apply_object。"""

    dataset_version: str
    object_version: str
    map_batch_id: str
    template_version: str
    binding_hash: str
    binding_status: str


def classify_transform_kind(steps: _Seq) -> TransformKind:
    """从 TransformStep 列表推断 direct / derived / unmapped。"""
    kinds = set()
    for s in steps:
        k = s.kind if hasattr(s, "kind") else s
        kinds.add(k)
    if "derived_rule" in kinds or "derived_default" in kinds:
        return "derived"
    if not kinds or kinds <= {"read"}:
        # 无步骤或仅 read → 检查是否 property_unmapped(由调用方在 trace 中标记)
        return "direct"
    return "direct"


def build_lineage_for_row(
    *,
    evaluation: object,
    provenance: dict[str, object],
    template_keys: _Seq[str],
    properties: _Seq,
    context: ApplyVersionContext,
    source: str,
    anchor_table: str,
    plan_provenance: _Seq,
) -> tuple[list[FieldLineageNode], list[FieldLineageInputRow]]:
    """将一个 mapped RowEvaluation 的 field_traces 与 provenance 合并为持久化 DTO。

    quarantined 行不进入对象表,也不写正式 lineage。
    """
    assert evaluation.status == "mapped"  # noqa: S101
    assert evaluation.output is not None  # noqa: S101
    output = evaluation.output
    key_values = {k: output.get(k) for k in template_keys}
    key_json = canonical_object_key_json(template_keys, key_values)
    key_hash = object_key_token(template_keys, key_values)

    # 按 provenance projection 分组
    anchor_pks: dict[str, object] = {}
    anchor_batch: str | None = None
    join_fks: dict[tuple[str, str], object] = {}  # (target, fk_col) → value
    join_pks: dict[tuple[str, str], dict[str, object]] = {}  # (target, fk) → {col: val}
    join_batches: dict[tuple[str, str], str | None] = {}
    derived_conds: dict[str, object] = {}

    for proj in plan_provenance:
        alias = proj.alias
        val = provenance.get(alias)
        role = proj.role
        if role == "anchor_pk":
            anchor_pks[proj.column] = val
        elif role == "extract_batch" and proj.join_key is None:
            anchor_batch = val if isinstance(val, str) else None
        elif role == "join_fk" and proj.join_key is not None:
            join_fks[proj.join_key] = val
        elif role == "join_pk" and proj.join_key is not None:
            join_pks.setdefault(proj.join_key, {})[proj.column] = val
        elif role == "extract_batch" and proj.join_key is not None:
            join_batches[proj.join_key] = val if isinstance(val, str) else None
        elif role == "derived_condition":
            derived_conds[proj.column] = val

    anchor_pk_json = dumps_json(anchor_pks) if anchor_pks else None

    nodes: list[FieldLineageNode] = []
    inputs: list[FieldLineageInputRow] = []

    for trace in evaluation.field_traces:
        prop_name = trace.property
        result_val = output.get(prop_name)
        t_status: str = trace.status
        t_reason: str | None = trace.unavailable_reason
        if t_status == "unavailable" and t_reason is None:
            t_reason = "source_evidence_unavailable"

        kind = classify_transform_kind(trace.steps)
        if t_reason == "property_unmapped":
            kind = "unmapped"

        steps_json = dumps_json([
            {
                "kind": s.kind,
                "before": _safe_step_value(s.before),
                "after": _safe_step_value(s.after),
                **({"map_hit": s.map_hit} if s.map_hit is not None else {}),
                **({"coerce_type": s.coerce_type} if s.coerce_type else {}),
                **(
                    {"derived_rule_index": s.derived_rule_index}
                    if s.derived_rule_index is not None
                    else {}
                ),
                **(
                    {"derived_when": s.derived_when}
                    if s.derived_when is not None
                    else {}
                ),
            }
            for s in trace.steps
        ])

        nodes.append(FieldLineageNode(
            dataset_version=context.dataset_version,
            object_version=context.object_version,
            object=_extract_object_name(context),
            object_key_json=key_json,
            object_key_hash=key_hash,
            property=prop_name,
            result_value_json=dumps_value_evidence(result_val),
            trace_status=t_status,
            unavailable_reason=t_reason,
            transform_kind=kind,
            transform_steps_json=steps_json,
            source=source,
            map_batch_id=context.map_batch_id,
            binding_hash=context.binding_hash,
            binding_status=context.binding_status,
            template_version=context.template_version,
        ))

        # 构建输入边
        ordinal = 0
        # 找到该属性对应的 FieldExpr(如果是 direct 字段)
        field_expr = None
        for proj in plan_provenance:
            if prop_name in getattr(proj, "property_names", ()):
                field_expr = proj
                break

        if "value" in trace.input_roles and anchor_pk_json:
            # 确定源表和列
            src_table = anchor_table
            src_col = None
            src_val = trace.raw_value
            # 检查是否是 join 字段
            is_join = any(s.kind == "join" for s in trace.steps)
            if is_join:
                # join 字段:找到对应的 join_key
                for step in trace.steps:
                    if step.kind == "join" and step.detail:
                        # 从 join_fk provenance 找
                        for jk, fk_val in join_fks.items():
                            if prop_name in _props_for_join(plan_provenance, jk):
                                src_table = jk[0]  # target table
                                jpk = join_pks.get(jk, {})
                                inputs.append(FieldLineageInputRow(
                                    dataset_version=context.dataset_version,
                                    object=_extract_object_name(context),
                                    object_key_json=key_json,
                                    property=prop_name,
                                    input_ordinal=ordinal,
                                    role="join_fk",
                                    source=source,
                                    source_table=anchor_table,
                                    source_pk_json=anchor_pk_json,
                                    source_column=jk[1],
                                    source_value_json=dumps_value_evidence(fk_val),
                                    extract_batch_id=anchor_batch,
                                    join_json=dumps_json({
                                        "target_table": jk[0],
                                        "fk_column": jk[1],
                                    }),
                                ))
                                ordinal += 1
                                # join target value input
                                inputs.append(FieldLineageInputRow(
                                    dataset_version=context.dataset_version,
                                    object=_extract_object_name(context),
                                    object_key_json=key_json,
                                    property=prop_name,
                                    input_ordinal=ordinal,
                                    role="value",
                                    source=source,
                                    source_table=jk[0],
                                    source_pk_json=dumps_json(jpk) if jpk else None,
                                    source_column=_extract_join_source_col(trace),
                                    source_value_json=dumps_value_evidence(trace.raw_value),
                                    extract_batch_id=join_batches.get(jk),
                                    join_json=None,
                                ))
                                ordinal += 1
                                break
                        break
                else:
                    # fallback: anchor value
                    inputs.append(FieldLineageInputRow(
                        dataset_version=context.dataset_version,
                        object=_extract_object_name(context),
                        object_key_json=key_json,
                        property=prop_name,
                        input_ordinal=ordinal,
                        role="value",
                        source=source,
                        source_table=anchor_table,
                        source_pk_json=anchor_pk_json,
                        source_column=None,
                        source_value_json=dumps_value_evidence(src_val),
                        extract_batch_id=anchor_batch,
                        join_json=None,
                    ))
                    ordinal += 1
            else:
                inputs.append(FieldLineageInputRow(
                    dataset_version=context.dataset_version,
                    object=_extract_object_name(context),
                    object_key_json=key_json,
                    property=prop_name,
                    input_ordinal=ordinal,
                    role="value",
                    source=source,
                    source_table=anchor_table,
                    source_pk_json=anchor_pk_json,
                    source_column=None,
                    source_value_json=dumps_value_evidence(src_val),
                    extract_batch_id=anchor_batch,
                    join_json=None,
                ))
                ordinal += 1

        if "derived_condition" in trace.input_roles and derived_conds:
            for col, val in sorted(derived_conds.items()):
                inputs.append(FieldLineageInputRow(
                    dataset_version=context.dataset_version,
                    object=_extract_object_name(context),
                    object_key_json=key_json,
                    property=prop_name,
                    input_ordinal=ordinal,
                    role="derived_condition",
                    source=source,
                    source_table=anchor_table,
                    source_pk_json=anchor_pk_json,
                    source_column=col,
                    source_value_json=dumps_value_evidence(val),
                    extract_batch_id=anchor_batch,
                    join_json=None,
                ))
                ordinal += 1

    return nodes, inputs


def _safe_step_value(val: object) -> object:
    """步骤 before/after 安全序列化:不内嵌完整 ValueEvidence,只保留标量。"""
    if val is None or isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, (date, datetime)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    return str(val)


def _extract_object_name(context: ApplyVersionContext) -> str:
    """从 object_version 提取对象名不适用;由调用方在 build 时传入。

    实际 object name 由 build_lineage_nodes_and_inputs 传入。
    此函数仅为 FieldLineageNode 构造占位;正式路径使用 build_lineage_nodes_and_inputs。
    """
    # 这个方法不应该被调用;正式路径使用 build_lineage_nodes_and_inputs
    raise NotImplementedError("use build_lineage_nodes_and_inputs")


def _extract_join_source_col(trace: object) -> str | None:
    """从 join 步骤 detail 中提取目标列名(最佳努力)。"""
    for s in trace.steps:  # type: ignore[attr-defined]
        if s.kind == "join" and s.detail:
            # detail 格式: "join TABLE.COL → TABLE.COL"
            parts = s.detail.split("→")
            if len(parts) == 2:
                right = parts[1].strip()
                if "." in right:
                    return right.split(".", 1)[1]
    return None


def _props_for_join(
    plan_provenance: _Seq, join_key: tuple[str, str],
) -> set[str]:
    result: set[str] = set()
    for proj in plan_provenance:
        if (
            getattr(proj, "join_key", None) == join_key
            and getattr(proj, "property_names", ())
        ):
            result.update(proj.property_names)
    return result


def build_lineage_nodes_and_inputs(
    *,
    evaluations: _Seq,
    provenance_rows: _Seq[dict[str, object]],
    template_keys: _Seq[str],
    object_name: str,
    context: ApplyVersionContext,
    source: str,
    anchor_table: str,
    plan_provenance: _Seq,
) -> tuple[list[FieldLineageNode], list[FieldLineageInputRow]]:
    """为所有 mapped 行构建 lineage 节点和输入边。

    evaluations 与 provenance_rows 必须一一对应(同一 SQL 结果行)。
    quarantined 行被跳过,不写正式 lineage。
    """
    all_nodes: list[FieldLineageNode] = []
    all_inputs: list[FieldLineageInputRow] = []

    for evaluation, prov in zip(evaluations, provenance_rows, strict=True):
        if evaluation.status != "mapped":
            continue
        assert evaluation.output is not None  # noqa: S101
        output = evaluation.output
        key_values = {k: output.get(k) for k in template_keys}
        key_json = canonical_object_key_json(template_keys, key_values)
        key_hash = object_key_token(template_keys, key_values)

        # 按 provenance projection 分组
        anchor_pks: dict[str, object] = {}
        anchor_batch: str | None = None
        join_fks: dict[tuple[str, str], object] = {}
        join_pks: dict[tuple[str, str], dict[str, object]] = {}
        join_batches: dict[tuple[str, str], str | None] = {}
        derived_conds: dict[str, object] = {}

        for proj in plan_provenance:
            alias = proj.alias
            val = prov.get(alias)
            role = proj.role
            if role == "anchor_pk":
                anchor_pks[proj.column] = val
            elif role == "extract_batch" and proj.join_key is None:
                anchor_batch = val if isinstance(val, str) else None
            elif role == "join_fk" and proj.join_key is not None:
                join_fks[proj.join_key] = val
            elif role == "join_pk" and proj.join_key is not None:
                join_pks.setdefault(proj.join_key, {})[proj.column] = val
            elif role == "extract_batch" and proj.join_key is not None:
                join_batches[proj.join_key] = (
                    val if isinstance(val, str) else None
                )
            elif role == "derived_condition":
                derived_conds[proj.column] = val

        anchor_pk_json = dumps_json(anchor_pks) if anchor_pks else None

        for trace in evaluation.field_traces:
            prop_name = trace.property
            result_val = output.get(prop_name)
            t_status: str = trace.status
            t_reason: str | None = trace.unavailable_reason
            if t_status == "unavailable" and t_reason is None:
                t_reason = "source_evidence_unavailable"

            kind = classify_transform_kind(trace.steps)
            if t_reason == "property_unmapped":
                kind = "unmapped"

            steps_json = dumps_json([
                {
                    "kind": s.kind,
                    "before": _safe_step_value(s.before),
                    "after": _safe_step_value(s.after),
                    **({"map_hit": s.map_hit} if s.map_hit is not None else {}),
                    **({"coerce_type": s.coerce_type} if s.coerce_type else {}),
                    **(
                        {"derived_rule_index": s.derived_rule_index}
                        if s.derived_rule_index is not None
                        else {}
                    ),
                    **(
                        {"derived_when": s.derived_when}
                        if s.derived_when is not None
                        else {}
                    ),
                }
                for s in trace.steps
            ])

            all_nodes.append(FieldLineageNode(
                dataset_version=context.dataset_version,
                object_version=context.object_version,
                object=object_name,
                object_key_json=key_json,
                object_key_hash=key_hash,
                property=prop_name,
                result_value_json=dumps_value_evidence(result_val),
                trace_status=t_status,
                unavailable_reason=t_reason,
                transform_kind=kind,
                transform_steps_json=steps_json,
                source=source,
                map_batch_id=context.map_batch_id,
                binding_hash=context.binding_hash,
                binding_status=context.binding_status,
                template_version=context.template_version,
            ))

            # 构建输入边
            ordinal = 0
            is_join = any(s.kind == "join" for s in trace.steps)

            if "value" in trace.input_roles and anchor_pk_json:
                if is_join:
                    for step in trace.steps:
                        if step.kind != "join":
                            continue
                        for jk, fk_val in join_fks.items():
                            if prop_name not in _props_for_join(
                                plan_provenance, jk,
                            ):
                                continue
                            jpk = join_pks.get(jk, {})
                            all_inputs.append(FieldLineageInputRow(
                                dataset_version=context.dataset_version,
                                object=object_name,
                                object_key_json=key_json,
                                property=prop_name,
                                input_ordinal=ordinal,
                                role="join_fk",
                                source=source,
                                source_table=anchor_table,
                                source_pk_json=anchor_pk_json,
                                source_column=jk[1],
                                source_value_json=dumps_value_evidence(fk_val),
                                extract_batch_id=anchor_batch,
                                join_json=dumps_json({
                                    "target_table": jk[0],
                                    "fk_column": jk[1],
                                }),
                            ))
                            ordinal += 1
                            all_inputs.append(FieldLineageInputRow(
                                dataset_version=context.dataset_version,
                                object=object_name,
                                object_key_json=key_json,
                                property=prop_name,
                                input_ordinal=ordinal,
                                role="value",
                                source=source,
                                source_table=jk[0],
                                source_pk_json=(
                                    dumps_json(jpk) if jpk else None
                                ),
                                source_column=_extract_join_source_col(trace),
                                source_value_json=dumps_value_evidence(
                                    trace.raw_value,
                                ),
                                extract_batch_id=join_batches.get(jk),
                                join_json=None,
                            ))
                            ordinal += 1
                            break
                        break
                else:
                    all_inputs.append(FieldLineageInputRow(
                        dataset_version=context.dataset_version,
                        object=object_name,
                        object_key_json=key_json,
                        property=prop_name,
                        input_ordinal=ordinal,
                        role="value",
                        source=source,
                        source_table=anchor_table,
                        source_pk_json=anchor_pk_json,
                        source_column=None,
                        source_value_json=dumps_value_evidence(
                            trace.raw_value,
                        ),
                        extract_batch_id=anchor_batch,
                        join_json=None,
                    ))
                    ordinal += 1

            if "derived_condition" in trace.input_roles and derived_conds:
                for col, val in sorted(derived_conds.items()):
                    all_inputs.append(FieldLineageInputRow(
                        dataset_version=context.dataset_version,
                        object=object_name,
                        object_key_json=key_json,
                        property=prop_name,
                        input_ordinal=ordinal,
                        role="derived_condition",
                        source=source,
                        source_table=anchor_table,
                        source_pk_json=anchor_pk_json,
                        source_column=col,
                        source_value_json=dumps_value_evidence(val),
                        extract_batch_id=anchor_batch,
                        join_json=None,
                    ))
                    ordinal += 1

    return all_nodes, all_inputs
