"""控制台管理 API 的请求/响应/错误契约。

M1 目标:给现有 wire shape 增加可生成的 OpenAPI 类型,不改变成功响应最外层结构。
历史落库时间字段保持 str|None,并在描述中标明 legacy local ISO text(不保证 offset)。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

LEGACY_TIME_DESC = (
    "legacy local ISO text from SQLite; offset/timezone not guaranteed in M1"
)

JsonPrimitive = str | int | float | bool | None
JsonValue = Annotated[
    JsonPrimitive | list[Any] | dict[str, Any],
    Field(description="Bounded JSON value (object/array/scalar); not free-form Any in docs"),
]


class HttpError(BaseModel):
    detail: str


class FieldError(BaseModel):
    field: str
    message: str


class ValidationResult(BaseModel):
    ok: bool
    errors: list[FieldError] = Field(default_factory=list)
    restart_required: bool | None = None


# ---- request bodies (moved from app.py) ----


class ActionBody(BaseModel):
    source: str = "digiwin_e10"
    object: str | None = None
    deep: bool = False


class ConfigPatch(BaseModel):
    templates: str | None = None
    landing: str | None = None


class SetupBody(BaseModel):
    """浏览器首次配置(替代 setup-platform.ps1)。Token 写入 secrets.env。"""

    ingest_token: str
    console_token: str
    mcp_token: str | None = None


class McpCallBody(BaseModel):
    tool: Literal["query_objects", "query_metrics"] | str
    params: dict[str, JsonValue] = Field(default_factory=dict)


# ---- setup / config ----


class SetupStatusResponse(BaseModel):
    needs_setup: bool
    config_path: str | None = None
    home: str | None = None


class SetupSuccessResponse(BaseModel):
    ok: Literal[True] = True
    restart_required: bool = True
    message: str
    mcp_token_generated: bool = False


class SetupFailureResponse(BaseModel):
    ok: Literal[False] = False
    errors: list[FieldError]


class SetupResponse(BaseModel):
    """POST /api/setup 成功或字段校验失败的联合形状。"""

    ok: bool
    errors: list[FieldError] = Field(default_factory=list)
    restart_required: bool | None = None
    message: str | None = None
    mcp_token_generated: bool | None = None


class ConfigViewResponse(BaseModel):
    needs_setup: bool
    templates: str = ""
    landing: str = ""


class ConfigSaveResponse(ValidationResult):
    restart_required: bool | None = True


# ---- overview / runs / quarantine / audit ----


class SyncStateRow(BaseModel):
    table_name: str
    watermark_col: str
    high_water: str | None = Field(default=None, description=LEGACY_TIME_DESC)
    last_run_at: str | None = Field(default=None, description=LEGACY_TIME_DESC)


class OverviewSource(BaseModel):
    source: str
    state: list[SyncStateRow]
    quarantined: int


class OverviewObject(BaseModel):
    object: str
    display_name: str
    rows: int | None = None
    mapped_at: str | None = Field(default=None, description=LEGACY_TIME_DESC)
    quarantined: int


class OverviewResponse(BaseModel):
    landing: str
    readonly: bool
    actions_sync_reconcile: bool
    sources: list[OverviewSource]
    objects: list[OverviewObject]
    needs_setup: bool = False


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    source: str
    started_at: str = Field(description=LEGACY_TIME_DESC)
    finished_at: str | None = Field(default=None, description=LEGACY_TIME_DESC)
    tables: int | None = None
    rows: int | None = None
    status: str | None = None
    detail: str | None = None


class QuarantineRecord(BaseModel):
    id: int
    source: str
    object: str
    keys_json: str | None = None
    reason: str
    created_at: str = Field(description=LEGACY_TIME_DESC)


class AuditRecord(BaseModel):
    ts: str = Field(description=LEGACY_TIME_DESC)
    source: str
    action: str
    sql: str
    rows: int | None = None
    duration_ms: float | None = None


# ---- services / logs / debug ----


class ServiceProbe(BaseModel):
    ok: bool
    method: str


class ServicesStatusResponse(BaseModel):
    ingest: ServiceProbe
    mcp: ServiceProbe
    apply: ServiceProbe
    console: ServiceProbe


class LogsResponse(BaseModel):
    ok: bool
    text: str = Field(description="Log tail text; failure must keep ok=false (not empty success)")


class RawTablePageResponse(BaseModel):
    table: str
    offset: int
    limit: int
    total: int
    rows: list[dict[str, JsonValue]]


class McpToolResult(RootModel[dict[str, JsonValue]]):
    """query_objects / query_metrics 白名单工具结果;结构随工具变化,用有界 JSON 对象表达。"""


# ---- actions ----


class ActionExecutionResult(BaseModel):
    executed: bool
    note: str = ""


class ObjectApplyResultModel(BaseModel):
    object: str
    total: int
    mapped: int
    quarantined: int
    status: str = "ok"


class ApplyActionResult(BaseModel):
    executed: bool
    results: list[ObjectApplyResultModel]
    aborted: list[str]


class RetryActionResult(BaseModel):
    executed: bool
    object: str
    total: int
    mapped: int
    quarantined: int
    status: str
