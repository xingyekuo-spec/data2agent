"""控制台管理 API 的请求/响应/错误契约。

M1 目标:给现有 wire shape 增加可生成的 OpenAPI 类型,不改变成功响应最外层结构。
历史落库时间字段保持 str|None,并在描述中标明 legacy local ISO text(不保证 offset)。

M2 追加:v0.2 新端点(pipeline / run 详情 / 数据浏览 / 对象 / 模板 / 建议卡)的
契约桩模型。路由已注册进 OpenAPI,运行时在所属里程碑实现前一律返回 501,
不得返回伪造成功。新端点时间字段一律 datetime(带时区 ISO 8601,v0.2 口径),
由实现里程碑负责把 legacy 本地时间迁移为带时区值。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

LEGACY_TIME_DESC = (
    "legacy local ISO text from SQLite; offset/timezone not guaranteed in M1"
)


class JsonValue(RootModel[
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
]):
    """Recursive JSON value with an explicit anyOf schema for OpenAPI/TS.

    Do not use pydantic.JsonValue or Any here: those emit empty `{}` schemas that
    become `unknown` after openapi-typescript.
    """


class HttpError(BaseModel):
    """HTTPException-style error body: detail is always a string."""

    detail: str


class RequestValidationErrorItem(BaseModel):
    """FastAPI/Pydantic request validation error item (422)."""

    model_config = ConfigDict(extra="allow")

    loc: list[str | int]
    msg: str
    type: str


class RequestError(BaseModel):
    """422 body: either HTTPException string detail or FastAPI validation list."""

    detail: str | list[RequestValidationErrorItem]


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
    tool: Literal["query_objects", "query_metrics"]
    params: dict[str, JsonValue] = Field(default_factory=dict)


# ---- setup / config ----


class SetupStatusResponse(BaseModel):
    needs_setup: bool
    config_path: str | None = None
    home: str | None = None


class SetupSuccessResponse(BaseModel):
    # ok has no default so OpenAPI marks it required; TS can narrow on ok === true.
    ok: Literal[True]
    restart_required: bool = True
    message: str
    mcp_token_generated: bool = False


class SetupFailureResponse(BaseModel):
    ok: Literal[False]
    errors: list[FieldError]


# 普通 anyOf 联合(不加 discriminator):pydantic 生成的 mapping 键是 Python
# 风格 "True"/"False",openapi-typescript 会把 ok 重写为字符串枚举,破坏 TS
# 收窄;anyOf + const 字面量让 ok 生成 boolean 字面量 true/false,收窄正确。
SetupResponse = SetupSuccessResponse | SetupFailureResponse


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


# ---- v0.2 契约桩(M2):schema 先行,运行时在所属里程碑实现前返回 501 ----

TZ_TIME_DESC = (
    "timezone-aware ISO 8601 (v0.2 convention); implementing milestone must "
    "convert legacy local text to an offset-bearing value"
)

PipelineNodeStatus = Literal[
    "unknown", "idle", "running", "healthy", "warning", "failed", "stale"
]


class PipelineNode(BaseModel):
    """管道单节点。status=unknown 表示后端无法检测,前端不得显示为正常。"""

    node: str = Field(
        description="节点 ID:erp / extract / push / raw / mapping / objects / mcp")
    status: PipelineNodeStatus
    last_success_at: datetime | None = Field(description=TZ_TIME_DESC)
    last_failure_at: datetime | None = Field(description=TZ_TIME_DESC)
    rows_in: int | None
    rows_out: int | None
    duration_ms: float | None
    error: str | None
    version: str | None = Field(
        description="数据或组件版本;dataset/object version 属 v0.3,当前可为空")


class PipelineResponse(BaseModel):
    generated_at: datetime = Field(description=TZ_TIME_DESC)
    nodes: list[PipelineNode]


RunType = Literal["sync", "apply", "reconcile", "ingest"]
RunStatus = Literal["running", "ok", "paused", "failed", "aborted"]


class RunStep(BaseModel):
    name: str = Field(description="步骤对象:表名 / 对象名 / 批次 ID")
    rows_in: int | None
    rows_out: int | None
    quarantined: int | None
    watermark_before: str | None
    watermark_after: str | None
    error: str | None


class RunDetailResponse(BaseModel):
    """统一运行模型(v0.3 起 validation 复用同一 Run/steps 结构)。"""

    id: int
    type: RunType
    status: RunStatus
    source: str | None
    started_at: datetime = Field(description=TZ_TIME_DESC)
    finished_at: datetime | None = Field(description=TZ_TIME_DESC)
    duration_ms: float | None
    dataset_version: str | None = Field(description="dataset version 属 v0.3,当前为空")
    steps: list[RunStep]


class RawDataPageResponse(BaseModel):
    source: str
    table: str
    offset: int
    limit: int
    total: int
    rows: list[dict[str, JsonValue]]


class ObjectSummary(BaseModel):
    object: str
    display_name: str
    domain: str | None
    rows: int | None = Field(description="尚未物化时为 null,不等于 0")
    mapped_at: datetime | None = Field(description=TZ_TIME_DESC)
    quarantined: int
    version: str | None = Field(description="object version 属 v0.3,当前为空")


class ObjectRowsPageResponse(BaseModel):
    object: str
    offset: int
    limit: int
    total: int
    rows: list[dict[str, JsonValue]]


BindingStatus = Literal["draft", "verified", "disabled"]


class TemplateProperty(BaseModel):
    name: str
    type: str
    desc: str | None = None
    sensitive: bool = False


class TemplateBinding(BaseModel):
    source: str
    tables: list[str]
    status: BindingStatus
    key_map: dict[str, str] = Field(default_factory=dict)
    field_map: dict[str, str] = Field(default_factory=dict)
    watermark: str | None = None
    notes: str | None = None


class TemplateObject(BaseModel):
    object: str
    display_name: str
    description: str | None = None
    domain: str | None = None
    keys: list[str]
    properties: list[TemplateProperty]
    bindings: list[TemplateBinding]


class ProposalEvidenceInput(BaseModel):
    claim: str = Field(min_length=1)
    query_id: str = Field(min_length=1)


class ProposalRequest(BaseModel):
    """建议卡请求,语义与 MCP propose_action 一致:

    evidence 必须引用同会话已记录查询的 meta.query_id;不得凭空生成。
    """

    object: str
    action: str
    conclusion: str = Field(min_length=1)
    evidence: list[ProposalEvidenceInput] = Field(min_length=1)


class ProposalQueryRef(BaseModel):
    query_id: str
    tool: str
    target: str
    at: datetime = Field(description=TZ_TIME_DESC)


class ProposalEvidence(BaseModel):
    claim: str
    query: ProposalQueryRef


class ProposalResponse(BaseModel):
    proposal_id: str
    at: datetime = Field(description=TZ_TIME_DESC)
    object: str
    action: str
    action_desc: str
    tier: str
    conclusion: str
    evidence: list[ProposalEvidence]
    caveats: list[str]
    governance: str
