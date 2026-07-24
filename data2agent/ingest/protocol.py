"""ingest 协议常量与请求模型(M4 快照协议,无旧协议兼容)。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 平台与中间机必须完全一致;变更时同步 bump 两侧。
INGEST_PROTOCOL_VERSION = "2"


def _require_protocol_version(version: str | None) -> None:
    if version is None or version == "":
        raise ValueError("缺少 ingest_protocol_version")
    if version != INGEST_PROTOCOL_VERSION:
        raise ValueError(
            f"ingest_protocol_version 不匹配: want {INGEST_PROTOCOL_VERSION}, got {version!r}")


class _VersionedRequest(BaseModel):
    """写路径请求基类:强制协议版本,禁止旧客户端默认回退。"""

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
