"""ingest 协议常量与请求模型(M4 快照协议)。

应用版本(`data2agent.__version__`)与跨机推送契约分离:

- `INGEST_PROTOCOL_VERSION`:本进程**发送/首选**的协议号(当前中间机写出字段);
- `SUPPORTED_INGEST_PROTOCOL_VERSIONS`:本进程**接受**的协议号列表(平台健康声明)。

平台可在仍支持旧协议时单独升级;仅当从 supported 列表移除某版本时,才要求中间机升级。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 本端写出 / 首选的协议号(中间机发送;平台 active)。
INGEST_PROTOCOL_VERSION = "3"

# 本端接受的协议号(仅平台 health / POST 校验)。迁移期同时接受 v2/v3。
# 从列表移除现场仍可能在跑的协议前,须更新 deploy/ingest_protocol_compat.json。
SUPPORTED_INGEST_PROTOCOL_VERSIONS: tuple[str, ...] = ("2", "3")

# 供已发布中间机读取的历史 health 字段。旧版本只比较
# ``ingest_protocol_version``，因此只要 v2 仍被平台接受，这里就必须保留 v2；
# 新中间机则读取 supported 列表。移除 v2 后，兼容门禁会要求本值切换到下一
# 个仍受支持的现场基线协议。
LEGACY_HEALTH_INGEST_PROTOCOL_VERSION = "2"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SOURCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_PORTABLE_TYPES = {"int", "real", "text", "blob"}


def batch_payload_digest(payload: dict) -> str:
    """对批次业务载荷做稳定摘要；发送端与接收端必须得到相同结果。"""
    material = {
        key: payload.get(key)
        for key in (
            "source", "schema", "table", "mode", "columns", "pk",
            "snapshot_id", "generation_id", "table_run_id", "batch_id", "rows",
        )
    }
    raw = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _validate_columns(columns: list[list[str]], pk: list[str]) -> list[str]:
    names: list[str] = []
    for pair in columns:
        if len(pair) != 2:
            raise ValueError("columns 须为 [列名, 类型] 二元组")
        name, portable = pair
        if not _IDENT.fullmatch(name):
            raise ValueError(f"columns 含非法 SQL 标识符:{name!r}")
        if portable not in _PORTABLE_TYPES:
            raise ValueError(f"columns 含未知可移植类型:{portable!r}")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("columns 不得包含重复列")
    if not set(pk).issubset(names):
        raise ValueError("pk 必须是 columns 的子集")
    return names


def is_supported_protocol(version: str | None) -> bool:
    return version is not None and version in SUPPORTED_INGEST_PROTOCOL_VERSIONS


def health_protocol_fields() -> dict[str, object]:
    """平台 /ingest/health 的协议相关字段。

    `ingest_protocol_version` 是旧中间机的兼容字段，独立于 active；
    新中间机优先读 `supported_ingest_protocol_versions`。
    """
    return {
        "ingest_protocol_version": LEGACY_HEALTH_INGEST_PROTOCOL_VERSION,
        "active_ingest_protocol_version": INGEST_PROTOCOL_VERSION,
        "supported_ingest_protocol_versions": list(SUPPORTED_INGEST_PROTOCOL_VERSIONS),
        "reconcile_protocol_version": "1",
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
    source: str = Field(min_length=1, max_length=128)
    table: str = Field(min_length=1, max_length=128)
    mode: Literal["incremental", "full_refresh"]
    pk: list[str] = Field(default_factory=list)
    snapshot_id: str | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    generation_id: str | None = Field(default=None, min_length=1, max_length=128)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _protocol_and_mode(self):
        _require_protocol_version(self.ingest_protocol_version)
        if self.mode == "full_refresh" and not self.snapshot_id:
            raise ValueError("full_refresh 必须提供 snapshot_id")
        if self.mode == "incremental" and not self.pk:
            raise ValueError("incremental 必须提供非空 pk")
        if not _SOURCE.fullmatch(self.source):
            raise ValueError("source 仅允许字母数字及 _.-，且须以字母或下划线开头")
        for kind, value in (("table", self.table), ("schema", self.schema_name)):
            if value is not None and not _IDENT.fullmatch(value):
                raise ValueError(f"{kind} 不是安全 SQL 标识符:{value!r}")
        if len(self.pk) != len(set(self.pk)):
            raise ValueError("pk 不得包含重复列")
        if any(not _IDENT.fullmatch(c) for c in self.pk):
            raise ValueError("pk 含非法 SQL 标识符")
        return self


class TableBeginBody(_VersionedRequest):
    columns: list[list[str]] = Field(max_length=2048)

    @model_validator(mode="after")
    def _valid_columns(self):
        _validate_columns(self.columns, self.pk)
        return self


class BatchBody(_VersionedRequest):
    columns: list[list[str]] = Field(max_length=2048)
    batch_id: str = Field(min_length=1, max_length=128)
    table_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    rows: list[dict] = Field(max_length=50_000)

    @model_validator(mode="after")
    def _columns_and_rows(self):
        names = _validate_columns(self.columns, self.pk)
        allowed = set(names)
        if any(not set(row).issubset(allowed) for row in self.rows):
            raise ValueError("rows 包含 columns 未声明字段")
        return self


class TableCompleteBody(_VersionedRequest):
    columns: list[list[str]] = Field(max_length=2048)
    completion_id: str = Field(min_length=1, max_length=128)
    rows: int
    batches: int

    @model_validator(mode="after")
    def _non_negative(self):
        _validate_columns(self.columns, self.pk)
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


class RunBeginBody(BaseModel):
    ingest_protocol_version: str
    source: str = Field(min_length=1, max_length=128)
    generation_id: str = Field(min_length=1, max_length=128)
    tables: list[str] = Field(max_length=10_000)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _valid(self):
        _require_protocol_version(self.ingest_protocol_version)
        if not _SOURCE.fullmatch(self.source):
            raise ValueError("source 非法")
        if any(not _IDENT.fullmatch(t) for t in self.tables):
            raise ValueError("tables 含非法 SQL 标识符")
        if len(self.tables) != len(set(self.tables)):
            raise ValueError("tables 不得重复")
        return self


class RunCompleteBody(BaseModel):
    ingest_protocol_version: str
    source: str = Field(min_length=1, max_length=128)
    generation_id: str = Field(min_length=1, max_length=128)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _valid(self):
        _require_protocol_version(self.ingest_protocol_version)
        if not _SOURCE.fullmatch(self.source):
            raise ValueError("source 非法")
        return self


class ReconcileStatsBody(BaseModel):
    """E6b L1：中间机提交源侧分段统计所需的落地侧查询边界。"""

    ingest_protocol_version: str
    source: str = Field(min_length=1, max_length=128)
    table: str = Field(min_length=1, max_length=128)
    watermark_col: str | None = None
    start: str | None = None
    end: str | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _valid(self):
        _require_protocol_version(self.ingest_protocol_version)
        if not _SOURCE.fullmatch(self.source) or not _IDENT.fullmatch(self.table):
            raise ValueError("source/table 非法")
        if self.watermark_col is None:
            if self.start is not None or self.end is not None:
                raise ValueError("无 watermark_col 时不得提供分段边界")
        else:
            if not _IDENT.fullmatch(self.watermark_col):
                raise ValueError("watermark_col 非法")
            if (self.start is None) != (self.end is None):
                raise ValueError("start/end 必须同时提供或同时省略")
            if self.start is not None and self.start >= self.end:
                raise ValueError("分段对账必须提供 start < end")
        return self


class ReconcileRepairBeginBody(ReconcileStatsBody):
    """E6b L2：开启一次可重放的分段/全表修复。"""

    repair_id: str = Field(min_length=1, max_length=64)
    schema_name: str | None = Field(default=None, alias="schema")
    columns: list[list[str]] = Field(max_length=2048)
    pk: list[str] = Field(min_length=1, max_length=64)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _repair_valid(self):
        _validate_columns(self.columns, self.pk)
        if self.schema_name is not None and not _IDENT.fullmatch(self.schema_name):
            raise ValueError("schema 非法")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.repair_id):
            raise ValueError("repair_id 非法")
        return self


class ReconcileRepairBatchBody(BaseModel):
    ingest_protocol_version: str
    source: str = Field(min_length=1, max_length=128)
    table: str = Field(min_length=1, max_length=128)
    repair_id: str = Field(min_length=1, max_length=64)
    batch_id: str = Field(min_length=1, max_length=128)
    rows: list[dict] = Field(max_length=50_000)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _valid(self):
        _require_protocol_version(self.ingest_protocol_version)
        if not _SOURCE.fullmatch(self.source) or not _IDENT.fullmatch(self.table):
            raise ValueError("source/table 非法")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.repair_id):
            raise ValueError("repair_id 非法")
        return self


class ReconcileRepairCompleteBody(BaseModel):
    ingest_protocol_version: str
    source: str = Field(min_length=1, max_length=128)
    table: str = Field(min_length=1, max_length=128)
    repair_id: str = Field(min_length=1, max_length=64)
    rows: int = Field(ge=0)
    batches: int = Field(ge=0)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _valid(self):
        _require_protocol_version(self.ingest_protocol_version)
        if not _SOURCE.fullmatch(self.source) or not _IDENT.fullmatch(self.table):
            raise ValueError("source/table 非法")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.repair_id):
            raise ValueError("repair_id 非法")
        return self
