"""ingest 协议常量与请求模型(M4 快照协议)。

应用版本(`data2agent.__version__`)与跨机推送契约分离:

- `INGEST_PROTOCOL_VERSION`:本进程**发送/首选**的协议号(当前中间机写出字段);
- `SUPPORTED_INGEST_PROTOCOL_VERSIONS`:本进程**接受**的协议号列表(平台健康声明)。

平台可在仍支持旧协议时单独升级;仅当从 supported 列表移除某版本时,才要求中间机升级。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 本端写出 / 首选的协议号。
INGEST_PROTOCOL_VERSION = "2"

# 本端接受的协议号(平台 health 与 POST 校验共用)。当前仅 v2。
# 移除某版本前须走破坏性发布门禁(见 scripts/check_ingest_compat.py)。
SUPPORTED_INGEST_PROTOCOL_VERSIONS: tuple[str, ...] = ("2",)

# 历史基线:曾正式发布且现场仍可能在跑的中间机协议。平台若不再支持其中某号,
# 必须显式声明破坏性发布。
BASELINE_INGEST_PROTOCOL_VERSIONS: tuple[str, ...] = ("2",)


def is_supported_protocol(version: str | None) -> bool:
    return version is not None and version in SUPPORTED_INGEST_PROTOCOL_VERSIONS


def health_protocol_fields() -> dict[str, object]:
    """平台 /ingest/health 的协议相关字段。

    保留 `ingest_protocol_version`(= active)供旧中间机精确比对;
    新中间机优先读 `supported_ingest_protocol_versions`。
    """
    return {
        "ingest_protocol_version": INGEST_PROTOCOL_VERSION,
        "active_ingest_protocol_version": INGEST_PROTOCOL_VERSION,
        "supported_ingest_protocol_versions": list(SUPPORTED_INGEST_PROTOCOL_VERSIONS),
    }


def _require_protocol_version(version: str | None) -> None:
    if version is None or version == "":
        raise ValueError("缺少 ingest_protocol_version")
    if not is_supported_protocol(version):
        raise ValueError(
            "ingest_protocol_version 不支持: "
            f"got {version!r}, supported={list(SUPPORTED_INGEST_PROTOCOL_VERSIONS)}"
        )


class _VersionedRequest(BaseModel):
    """写路径请求基类:协议须在平台 supported 列表内。"""

    ingest_protocol_version: str
    source: str
    table: str
    mode: Literal["incremental", "full_refresh"]
    pk: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _protocol_and_mode(self):
        _require_protocol_version(self.ingest_protocol_version)
        if self.mode == "full_refresh" and not self.snapshot_id:
            raise ValueError("full_refresh 必须提供 snapshot_id")
        if self.mode == "incremental" and not self.pk:
            raise ValueError("incremental 必须提供非空 pk")
        return self


class TableBeginBody(_VersionedRequest):
    columns: list[list[str]]


class BatchBody(_VersionedRequest):
    columns: list[list[str]]
    batch_id: str
    rows: list[dict]


class TableCompleteBody(_VersionedRequest):
    columns: list[list[str]]
    completion_id: str
    rows: int
    batches: int

    @model_validator(mode="after")
    def _non_negative(self):
        if self.rows < 0 or self.batches < 0:
            raise ValueError("rows 和 batches 不能为负数")
        return self


class TableAbortBody(_VersionedRequest):
    """清理未发布的 full_refresh snapshot staging。"""

    columns: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_full_refresh_abort(self):
        if self.mode != "full_refresh":
            raise ValueError("table-abort 仅用于 full_refresh")
        return self
