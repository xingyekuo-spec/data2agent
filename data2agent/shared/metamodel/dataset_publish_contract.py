"""v0.3 M2 数据集发布契约：状态机、物理表名与冲突语义。

纯函数层，不读写数据库。T05/T06 的编排器消费这些决策；HTTP 层只映射
outcome → 状态码，不在此泄漏表名或内部异常。
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .versioning import (
    DatasetStatus,
    DatasetVersionRecord,
    ObjectBuildStatus,
    ObjectVersionRecord,
    parse_object_manifest,
)

BUILD_TABLE_PREFIX = "objv_"
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_BUILD_TABLE_RE = re.compile(
    r"^objv_([0-9a-f]+)_([0-9a-f]+)_([0-9a-f]+)$"
)

# Digest lengths are fixed so generated identifiers stay compact and stable.
_SOURCE_DIGEST_LEN = 12
_OBJECT_DIGEST_LEN = 12

DATASET_TRANSITIONS: dict[DatasetStatus, frozenset[DatasetStatus]] = {
    "building": frozenset({"failed", "published"}),
    "published": frozenset({"retired"}),
    "retired": frozenset({"published"}),
    "failed": frozenset(),
}

OBJECT_TRANSITIONS: dict[ObjectBuildStatus, frozenset[ObjectBuildStatus]] = {
    "building": frozenset({"built", "failed"}),
    "built": frozenset({"published"}),
    "published": frozenset({"retired"}),
    "retired": frozenset({"published"}),
    "failed": frozenset(),
}

PublishOutcome = Literal["execute", "idempotent", "not_found", "conflict"]
ReasonCode = Literal[
    "not_found",
    "illegal_state",
    "not_ready",
    "stale_previous",
    "not_direct_previous",
    "failed_dataset",
    "missing_candidate",
]


class ActionDecision(BaseModel):
    """publish/rollback 前置决策；HTTP 层据此映射 200/404/409。"""

    model_config = ConfigDict(extra="forbid")

    outcome: PublishOutcome
    reason_code: ReasonCode | None = None
    http_status: Literal[404, 409] | None = None


def _digest(text: str, length: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def make_build_table(source: str, object_name: str, version_token: str) -> str:
    """生成不可变候选物理表名；version_token 必须为小写十六进制。"""
    if not version_token or not _HEX_RE.fullmatch(version_token):
        raise ValueError("version_token 必须为非空小写 ASCII 十六进制")
    name = (
        f"{BUILD_TABLE_PREFIX}"
        f"{_digest(source, _SOURCE_DIGEST_LEN)}_"
        f"{_digest(object_name, _OBJECT_DIGEST_LEN)}_"
        f"{version_token}"
    )
    return validate_build_table(name)


def is_valid_build_table(name: str) -> bool:
    """严格校验物理表名格式；拒绝遗留 obj_* 与任意注入字符。"""
    if not isinstance(name, str) or not name:
        return False
    return _BUILD_TABLE_RE.fullmatch(name) is not None


def validate_build_table(name: str) -> str:
    """返回已校验表名；非法则 fail-closed。"""
    if not is_valid_build_table(name):
        raise ValueError("非法物理构建表名")
    return name


def can_transition_dataset(frm: DatasetStatus, to: DatasetStatus) -> bool:
    return to in DATASET_TRANSITIONS.get(frm, frozenset())


def can_transition_object(frm: ObjectBuildStatus, to: ObjectBuildStatus) -> bool:
    return to in OBJECT_TRANSITIONS.get(frm, frozenset())


def is_dataset_ready(
    dataset: DatasetVersionRecord,
    objects: list[ObjectVersionRecord],
) -> bool:
    """ready = building 且冻结清单齐全、全部对象均为 built。不新增 ready 枚举。"""
    if dataset.status != "building":
        return False
    manifest = parse_object_manifest(dataset.object_manifest)
    if not manifest:
        return False
    by_name = {o.object: o for o in objects if o.dataset_version == dataset.dataset_version}
    if set(by_name) != set(manifest):
        return False
    return all(by_name[name].status == "built" for name in manifest)


def evaluate_publish(
    *,
    candidate: DatasetVersionRecord | None,
    objects: list[ObjectVersionRecord],
    current_published: DatasetVersionRecord | None,
) -> ActionDecision:
    """评估是否可原子发布候选版本。"""
    if candidate is None:
        return ActionDecision(
            outcome="not_found", reason_code="not_found", http_status=404,
        )

    if (
        current_published is not None
        and candidate.dataset_version == current_published.dataset_version
        and candidate.status == "published"
        and current_published.status == "published"
    ):
        return ActionDecision(outcome="idempotent")

    if candidate.status == "failed":
        return ActionDecision(
            outcome="conflict", reason_code="failed_dataset", http_status=409,
        )

    if candidate.status != "building":
        return ActionDecision(
            outcome="conflict", reason_code="illegal_state", http_status=409,
        )

    if not is_dataset_ready(candidate, objects):
        return ActionDecision(
            outcome="conflict", reason_code="not_ready", http_status=409,
        )

    expected_previous = (
        None if current_published is None else current_published.dataset_version
    )
    if candidate.previous_dataset_version != expected_previous:
        return ActionDecision(
            outcome="conflict", reason_code="stale_previous", http_status=409,
        )

    if not can_transition_dataset("building", "published"):
        return ActionDecision(
            outcome="conflict", reason_code="illegal_state", http_status=409,
        )

    return ActionDecision(outcome="execute")


def evaluate_rollback(
    *,
    target: DatasetVersionRecord | None,
    current_published: DatasetVersionRecord | None,
) -> ActionDecision:
    """评估是否可回滚到直接上一稳定版本。"""
    if target is None:
        return ActionDecision(
            outcome="not_found", reason_code="not_found", http_status=404,
        )

    if (
        current_published is not None
        and target.dataset_version == current_published.dataset_version
        and current_published.status == "published"
    ):
        return ActionDecision(outcome="idempotent")

    if current_published is None or current_published.status != "published":
        return ActionDecision(
            outcome="conflict", reason_code="illegal_state", http_status=409,
        )

    previous = current_published.previous_dataset_version
    if previous is None or target.dataset_version != previous:
        return ActionDecision(
            outcome="conflict",
            reason_code="not_direct_previous",
            http_status=409,
        )

    if target.status != "retired":
        return ActionDecision(
            outcome="conflict", reason_code="illegal_state", http_status=409,
        )

    if not can_transition_dataset("retired", "published"):
        return ActionDecision(
            outcome="conflict", reason_code="illegal_state", http_status=409,
        )

    return ActionDecision(outcome="execute")
