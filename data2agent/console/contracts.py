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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

LEGACY_TIME_DESC = (
    "legacy local ISO text from SQLite; offset/timezone not guaranteed in M1"
)

TZ_TIME_DESC = (
    "timezone-aware ISO 8601 (v0.2 convention); implementing milestone must "
    "convert legacy local text to an offset-bearing value"
)

DEFAULT_BREAKER_THRESHOLD = 0.05
"""默认熔断阈值:单对象隔离率 >= 5% 触发熔断,保留旧数据并中止。"""

# 统一运行模型(M3 起):validation 属 v0.3,但进入同一 Run 模型,避免另造验收记录。
# M2 追加 publish/rollback；step 增加 dataset 以承载数据集级动作。
RunType = Literal[
    "sync", "apply", "reconcile", "ingest", "validation", "publish", "rollback",
]
RunStatus = Literal["running", "ok", "paused", "failed", "aborted"]
StepKind = Literal["table", "object", "segment", "batch", "dataset"]
StepsState = Literal["available", "legacy_unavailable"]


class JsonValue(RootModel[
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
]):
    """Recursive JSON value with an explicit anyOf schema for OpenAPI/TS.

    Do not use pydantic.JsonValue or Any here: those emit empty `{}` schemas that
    become `unknown` after openapi-typescript.
    """


JsonObject = dict[str, JsonValue]
"""Bounded JSON object (string keys, JsonValue values) for keys/params fields."""


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


class ApplyActionBody(BaseModel):
    """apply 专用请求体；publish 不得进入 sync/reconcile/retry 共用的 ActionBody。"""

    source: str = "digiwin_e10"
    publish: bool = True


class ConfigPatch(BaseModel):
    templates: str | None = None
    landing: str | None = None


class SetupBody(BaseModel):
    """浏览器首次配置(替代 setup-platform.ps1)。Token 写入 secrets.env。"""

    ingest_token: str
    console_token: str
    mcp_token: str | None = None


class McpCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: Literal["query_objects", "query_metrics"]
    params: dict[str, JsonValue] = Field(default_factory=dict)


McpLabReasonCode = Literal[
    "invalid_params",
    "invalid_session",
    "unknown_target",
    "not_materialized",
    "not_published",
    "query_expired",
    "evidence_not_found",
    "evidence_principal_mismatch",
    "evidence_session_mismatch",
    "evidence_source_mismatch",
    "dataset_version_mismatch",
    "result_digest_mismatch",
    "evidence_integrity_failed",
    "tier_forbidden",
    "rate_limited",
    "mcp_unavailable",
    "evidence_store_unavailable",
    "execution_failed",
]

EvidenceChannel = Literal["console", "mcp_stdio", "mcp_http", "demo"]


class McpQueryMeta(BaseModel):
    """查询公共元数据。M5 起升级为 principal+session 级 evidence 语义。

    目录/未实现指标等不可引用结果保持 query/digest/summary/created/expires 为 null。
    """

    query_id: str | None = Field(
        default=None,
        description="可被建议卡引用的查询 ID;目录查询或无可引用结果时为 null",
    )
    tool: Literal["query_objects", "query_metrics"]
    target: str = Field(description="对象名或指标名;目录查询可用空串")
    row_count: int | None = None
    duration_ms: int = Field(ge=0, description="服务端耗时毫秒")
    masked_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence_scope: Literal["principal_session"] = Field(
        default="principal_session",
        description="M5:query evidence 以 principal + session 为隔离域",
    )
    session_id: str | None = Field(
        default=None,
        description="同一 principal 下的 evidence session；目录/未引用结果为 null",
    )
    result_digest: str | None = Field(
        default=None,
        description="脱敏 canonical result 的完整性摘要；不可引用结果为 null",
    )
    result_summary: dict[str, JsonValue] | None = Field(
        default=None,
        description="有界脱敏结果摘要；不提供任意历史完整结果下载",
    )
    created_at: datetime | None = Field(
        default=None,
        description="query evidence 创建时间；目录/未引用结果为 null",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="query evidence 引用有效期；目录/未引用结果为 null",
    )
    dataset_version: str | None = Field(
        default=None,
        description="查询实际读取的 published dataset_version;无 published 时为 null",
    )
    template_version: str | None = Field(
        default=None,
        description="published 快照冻结的 template_version;无 published 时为 null",
    )
    binding_hashes: dict[str, str] = Field(
        default_factory=dict,
        description="查询涉及对象的 binding_hash 映射(object → hash)",
    )


class McpLabError(BaseModel):
    """MCP Lab 安全错误:前端按 status/reason_code 分支,不解析中文 detail。"""

    detail: str
    reason_code: McpLabReasonCode
    tool: str | None = None
    retryable: bool = False
    error_id: str | None = None


class McpObjectQueryResult(BaseModel):
    """query_objects 数据查询成功形状(目录查询另见宽表结果)。"""

    object: str
    display_name: str
    rows: list[dict[str, JsonValue]]
    meta: McpQueryMeta


class McpMetricsQueryResult(BaseModel):
    """query_metrics 数据查询成功形状。"""

    metric: str
    display_name: str
    status: str
    formula: str
    grain: list[str] = Field(default_factory=list)
    caveats: str = ""
    freshness_sla: str = ""
    implemented: bool
    unit: str | None = None
    group_by: str | None = None
    rows: list[dict[str, JsonValue]] = Field(default_factory=list)
    meta: McpQueryMeta


class EvidenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal: str = Field(min_length=1)
    session_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    channel: EvidenceChannel


class QueryEvidenceDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: str
    source: str
    tool: Literal["query_objects", "query_metrics"]
    target: str
    session_id: str
    evidence_scope: Literal["principal_session"] = "principal_session"
    normalized_query: dict[str, JsonValue]
    dataset_version: str | None = None
    template_version: str | None = None
    binding_hashes: dict[str, str] = Field(default_factory=dict)
    result_digest: str
    result_summary: dict[str, JsonValue]
    warnings: list[str] = Field(default_factory=list)
    row_count: int | None = None
    created_at: datetime = Field(description=TZ_TIME_DESC)
    expires_at: datetime = Field(description=TZ_TIME_DESC)


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
    app_version: str
    build_version: str | None = None
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
    sources: list[OverviewSource]
    objects: list[OverviewObject]
    needs_setup: bool = False
    # ---- M3 追加:Dashboard 观测聚合(旧字段保持 wire 兼容)----
    generated_at: datetime = Field(description=TZ_TIME_DESC)
    summary: OverviewSummary
    versions: OverviewVersions
    binding_summary: BindingSummary
    alerts: list[OverviewAlert]
    recent_runs: list[RecentRun] | None = Field(
        description="最近运行;查询失败为 null(不可检测),不返回空列表冒充从未运行")
    sync_trend: list[SyncTrendPoint] | None = Field(
        description="24h 趋势;查询失败为 null(不可检测),不返回空列表冒充无数据")
    count_notes: list[CountNote]


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    type: RunType | None = Field(
        default=None, description="结构化运行类型;历史记录为 NULL(类型未知),不回填猜测")
    status: RunStatus | None = None
    source: str
    started_at: datetime | None = Field(default=None, description=TZ_TIME_DESC)
    finished_at: datetime | None = Field(default=None, description=TZ_TIME_DESC)
    duration_ms: float | None = None
    tables: int | None = None
    rows: int | None = None
    quarantined: int | None = None
    dataset_version: str | None = Field(
        default=None, description="M2 写入实际发布版本;M1 固定 null")
    detail: str | None = Field(
        default=None, description="既有字段,安全截断,逐步弃用;状态判断不得解析它")
    error: str | None = None
    error_id: str | None = None


class QuarantineRecord(BaseModel):
    """隔离记录(列表视图)。M5 起 created_at 改为带时区 datetime。"""

    id: int
    source: str
    object: str
    keys_json: str | None = None  # legacy, keep for wire compatibility
    keys: JsonObject | None = None  # parsed keys, null + warning on parse failure
    reason: str  # must be sanitized (no traceback/SQL/sensitive values)
    batch_id: str | None = None
    created_at: datetime = Field(description=TZ_TIME_DESC)
    age_seconds: int | None = None
    warnings: list[str] = Field(default_factory=list)


class QuarantineDetail(QuarantineRecord):
    """隔离详情:仅由 GET /api/quarantine/{id} 返回(强制 Bearer auth)。"""

    raw: JsonObject | None = None  # sanitized, truncated, JSON-safe
    truncations: list[FieldTruncation] = Field(default_factory=list)
    request_id: str


class QuarantineGroup(BaseModel):
    """隔离分组摘要(按 source+object 聚合)。"""

    source: str
    object: str
    display_name: str | None = None
    pending: int
    latest_created_at: datetime | None = None
    latest_batch_id: str | None = None
    latest_reason: str | None = None  # sanitized
    quarantine_rate: float | None = None
    breaker_threshold: float  # DEFAULT_BREAKER_THRESHOLD (0.05) or config override
    rate_state: Literal["ok", "warning", "tripped", "unknown"]
    serving_state: Literal[
        "fresh", "stale", "not_materialized", "unavailable", "unknown"
    ]
    latest_apply_run_id: int | None = None
    object_rows: int | None = None
    mapped_at: datetime | None = None
    retry_allowed: bool = True
    retry_disabled_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class AuditRecord(BaseModel):
    id: int = Field(description="稳定主键(排序锚点)")
    ts: datetime = Field(description=TZ_TIME_DESC)
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
    dataset_version: str | None = None
    published: bool | None = None
    previous_dataset_version: str | None = None


class RetryActionResult(BaseModel):
    """重试成功响应(M5 起 status 收窄为 Literal["ok"])。"""

    executed: bool  # always True for success
    object: str
    total: int
    mapped: int
    quarantined: int
    status: Literal["ok"]
    run_id: int
    step_id: int
    detail_path: str
    dataset_version: str | None = None


class RetryActionError(BaseModel):
    """重试错误响应(409/500):结构化错误,不含 traceback/SQL/敏感值。"""

    detail: str  # safe summary, no traceback/SQL/sensitive values
    reason_code: Literal[
        "circuit_broken", "execution_failed", "observation_failed",
        "preflight_failed", "active_build", "empty_manifest", "empty_field_map",
    ]
    executed: bool  # whether apply_object started executing
    object: str
    total: int | None = None
    mapped: int | None = None
    quarantined: int | None = None
    status: Literal["aborted", "failed"]
    run_id: int | None = None
    step_id: int | None = None
    detail_path: str | None = None
    error_id: str | None = None


# ---- v0.2 契约桩(M2):schema 先行,运行时在所属里程碑实现前返回 501 ----

PipelineNodeStatus = Literal[
    "unknown", "idle", "running", "healthy", "warning", "failed", "stale"
]


class PipelineNode(BaseModel):
    """管道单节点。status=unknown 表示后端无法检测,前端不得显示为正常。"""

    node: str = Field(
        description="节点 ID:erp / extract / push / raw / mapping / objects / mcp")
    status: PipelineNodeStatus
    status_reason: str = Field(
        default="", description="状态原因(人话;截断,不含 Token/SQL/敏感行)")
    observed_at: datetime | None = Field(
        default=None, description="该节点证据的观测时间;" + TZ_TIME_DESC)
    last_success_at: datetime | None = Field(description=TZ_TIME_DESC)
    last_failure_at: datetime | None = Field(description=TZ_TIME_DESC)
    rows_in: int | None
    rows_out: int | None
    duration_ms: float | None
    error: str | None
    version: str | None = Field(
        description="数据或组件版本;dataset/object version 属 v0.3,当前可为空")
    run_id: str | None = Field(default=None, description="正在运行/最近一次运行的 ID")
    source: str | None = Field(default=None, description="节点归属数据源")
    detail_path: str | None = Field(
        default=None, description="详情入口;目标页未实现时为 null,不生死链")


class PipelineResponse(BaseModel):
    generated_at: datetime = Field(description=TZ_TIME_DESC)
    overall_status: PipelineNodeStatus = Field(
        default="unknown",
        description="按 failed>stale>warning>running>unknown>idle>healthy 折叠;"
        "存在 unknown 时不得为 healthy")
    nodes: list[PipelineNode]


class RunStep(BaseModel):
    id: int
    ordinal: int
    kind: StepKind
    name: str = Field(description="步骤对象:表名 / 对象名 / segment / batch")
    status: RunStatus
    started_at: datetime | None = Field(default=None, description=TZ_TIME_DESC)
    finished_at: datetime | None = Field(default=None, description=TZ_TIME_DESC)
    duration_ms: float | None = None
    batch_id: str | None = None
    rows_in: int | None
    rows_out: int | None
    quarantined: int | None
    repaired: int | None = None
    soft_deleted: int | None = None
    watermark_before: JsonValue | None = None
    watermark_after: JsonValue | None = None
    error: str | None = None
    error_id: str | None = None


class RunDetailResponse(RunSummary):
    """统一运行详情。steps_state=legacy_unavailable 表示历史运行缺少 step
    证据(不能显示为"处理 0 项");新运行确实没有工作单元时才允许
    available + steps=[]。"""

    steps_state: StepsState
    steps: list[RunStep]


# ---- M6:只读验收 Validation Run ----

ValidationCheckStatus = Literal["pass", "warning", "fail", "skipped"]
ValidationOverallStatus = Literal["pass", "warning", "fail"]


class ValidationRunRequest(BaseModel):
    """启动一次只读验收。

    不接收 source/path/SQL/忽略失败等调用方控制项；数据源和版本快照只从
    当前 Console 配置及已发布数据集解析。include_mcp_probe 保留为显式、
    安全的开关，当前 probe 仍只读取既有 M5 evidence，不执行写操作。
    """

    model_config = ConfigDict(extra="forbid")

    include_mcp_probe: bool = True


class ValidationEvidenceRef(BaseModel):
    """报告内可追溯的相对 Console API 链接；不暴露物理路径或敏感内容。"""

    kind: Literal["run", "dataset", "object", "lineage", "evidence", "config"]
    label: str = Field(min_length=1, max_length=160)
    href: str = Field(min_length=1, max_length=500, pattern=r"^/api/")


class ValidationCheckResponse(BaseModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    title: str = Field(min_length=1, max_length=160)
    status: ValidationCheckStatus
    blocking: bool
    summary: str = Field(min_length=1, max_length=500)
    started_at: datetime = Field(description=TZ_TIME_DESC)
    finished_at: datetime = Field(description=TZ_TIME_DESC)
    detail: JsonObject = Field(default_factory=dict)
    evidence: list[ValidationEvidenceRef] = Field(default_factory=list)


class ValidationReportResponse(BaseModel):
    """持久、不可变的 M6 验收报告；detail 与 JSON 下载使用同一形状。"""

    report_schema_version: Literal[1] = 1
    run_id: int
    source: str
    overall_status: ValidationOverallStatus
    started_at: datetime = Field(description=TZ_TIME_DESC)
    finished_at: datetime = Field(description=TZ_TIME_DESC)
    deployment: JsonObject = Field(default_factory=dict)
    dataset_version: str | None = None
    template_version: str | None = None
    summary: JsonObject = Field(default_factory=dict)
    checks: list[ValidationCheckResponse]


class ValidationRunStartedResponse(BaseModel):
    run_id: int
    overall_status: ValidationOverallStatus
    report_path: str = Field(pattern=r"^/api/validation/runs/[0-9]+$")


class ValidationError(BaseModel):
    detail: str
    reason_code: Literal[
        "validation_in_progress", "validation_not_found", "validation_unavailable",
    ]
    retryable: bool = False


# ---- M4:数据浏览与安全口径 ----

ColumnRole = Literal["business_key", "data", "metadata"]
ColumnClassification = Literal["normal", "sensitive", "unknown"]

# 单值序列化预算(M4-T01 固定):超限值返回安全预览并在 truncations 列明
VALUE_BUDGET_BYTES = 64 * 1024


class ColumnMeta(BaseModel):
    """列元数据;unknown 分类持续警告,不能默认为安全。"""

    name: str
    data_type: str
    role: ColumnRole
    classification: ColumnClassification
    masked: bool = Field(description="服务端已脱敏为 ***;v0.2 不提供 unmask")
    searchable: bool


class FieldTruncation(BaseModel):
    """行级截断标记:预览不得当作完整值导出。"""

    row_index: int
    fields: list[str]


class RawDataPageResponse(BaseModel):
    source: str
    table: str = Field(description="逻辑表名;物理表名不作为用户输入")
    columns: list[ColumnMeta]
    rows: list[dict[str, JsonValue]]
    truncations: list[FieldTruncation]
    offset: int
    limit: int
    total: int
    sort: str = Field(description="排序来源说明,如 pk:CUSTOMER_CODE 或 rowid")
    query: str = Field(description="回显 q 参数(空串=未搜索)")
    searchable: bool
    warnings: list[str]
    generated_at: datetime = Field(description=TZ_TIME_DESC)


class ObjectSummary(BaseModel):
    object: str
    display_name: str
    domain: str | None
    rows: int | None = Field(description="尚未物化时为 null,不等于 0")
    mapped_at: datetime | None = Field(description=TZ_TIME_DESC)
    quarantined: int
    version: str | None = Field(
        description="published 快照中的 object_version;尚未发布时为 null")
    searchable: bool = False
    warning: str | None = None


class ObjectLineageRef(BaseModel):
    """对象行与 lineage key token 的对齐引用(与 rows 同序)。

    key_token 是不透明定位符;object_key 仍走 frozen template 敏感显示规则。
    """

    row_index: int = Field(ge=0)
    key_token: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="规范 64 位小写 hex SHA-256",
    )
    object_key: list[list[JsonValue]] = Field(
        description='规范化 pair 数组,如 [["order_no","SO-001"],["line_no",10]]',
    )


class ObjectRowsPageResponse(BaseModel):
    object: str
    columns: list[ColumnMeta]
    rows: list[dict[str, JsonValue]]
    truncations: list[FieldTruncation]
    offset: int
    limit: int
    total: int
    sort: str
    query: str
    searchable: bool
    warnings: list[str]
    generated_at: datetime = Field(description=TZ_TIME_DESC)
    lineage_refs: list[ObjectLineageRef] = Field(
        default_factory=list,
        description="与 rows 对齐的 lineage 引用;旧数据集可为空列表",
    )


class RawTableCatalogItem(BaseModel):
    source: str
    table: str = Field(description="逻辑表名")
    display_name: str
    rows: int | None = Field(description="计数失败为 null + 警告,不伪装 0")
    latest_batch_id: str | None
    extracted_at: datetime | None = Field(description=TZ_TIME_DESC)
    searchable: bool
    classification_warning: bool


class RawTableCatalogResponse(BaseModel):
    items: list[RawTableCatalogItem]
    warnings: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(description=TZ_TIME_DESC)


class AccessAuditItem(BaseModel):
    """控制台数据访问事实。严禁记录 Token、查询值原文、返回值或 traceback。"""

    id: int
    ts: datetime = Field(description=TZ_TIME_DESC)
    subject: str
    resource_type: Literal["raw", "object", "quarantine_raw"]
    source: str | None
    resource: str
    allowed: bool
    reason_code: str
    offset: int | None = None
    limit: int | None = None
    returned_rows: int | None = None
    request_id: str | None = None


class AccessAuditPage(BaseModel):
    items: list[AccessAuditItem]
    offset: int
    limit: int
    total: int
    generated_at: datetime = Field(description=TZ_TIME_DESC)


BindingStatus = Literal["draft", "verified", "disabled"]


class DeriveRule(BaseModel):
    """模板派生规则:when 条件匹配时使用 value。"""

    when: dict[str, str | None]  # condition: field -> value (None = "any")
    value: str


class DerivedField(BaseModel):
    """模板派生字段:有序规则列表 + 默认值。"""

    rules: list[DeriveRule] = Field(default_factory=list)
    default: str | None = None


class TemplateMaterialization(BaseModel):
    """模板对象物化状态。"""

    state: Literal["materialized", "not_materialized", "unknown"]
    source: str | None = None
    rows: int | None = None
    mapped_at: datetime | None = None
    batch_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TemplateProperty(BaseModel):
    name: str
    type: str
    desc: str | None = None
    sensitive: bool = False
    ref: str | None = None
    enum_values: list[str] = Field(default_factory=list)


class TemplateBinding(BaseModel):
    source: str
    tables: list[str]
    status: BindingStatus
    key_map: dict[str, str] = Field(default_factory=dict)
    field_map: dict[str, str] = Field(default_factory=dict)
    watermark: str | None = None
    notes: str | None = None
    enabled: bool = True
    enum_map: dict[str, dict[str, str]] = Field(default_factory=dict)
    derived: dict[str, DerivedField] = Field(default_factory=dict)


class TemplateRelation(BaseModel):
    name: str
    target: str
    cardinality: str
    desc: str | None = None


class TemplateObject(BaseModel):
    object: str
    display_name: str
    description: str | None = None
    domain: str | None = None
    keys: list[str]
    properties: list[TemplateProperty]
    relations: list[TemplateRelation] = Field(default_factory=list)
    bindings: list[TemplateBinding]
    # ---- M5 追加 ----
    source_of_truth: str
    knowledge_refs: list[str] = Field(default_factory=list)
    materialized: TemplateMaterialization | None = None
    quarantine_pending: int = 0
    warnings: list[str] = Field(default_factory=list)


class TemplateMetric(BaseModel):
    """模板指标定义。"""

    metric: str
    display_name: str
    status: Literal["certified", "draft", "deprecated"]
    calibration_state: Literal["calibrated", "uncalibrated", "deprecated"] = "uncalibrated"
    formula: str
    grain: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    caveats: str = ""
    freshness_sla: str = "T+1"


class ProposalEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    result_digest: str = Field(min_length=1)


class ProposalRequest(BaseModel):
    """建议卡请求,语义与 MCP propose_action 一致:

    evidence 必须引用同会话已记录查询的 meta.query_id + result_digest;不得凭空生成。
    """

    model_config = ConfigDict(extra="forbid")

    object: str
    action: str
    conclusion: str = Field(min_length=1)
    evidence: list[ProposalEvidenceInput] = Field(min_length=1)


class ProposalQueryRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: str
    source: str = ""
    tool: str
    target: str
    normalized_query: dict[str, JsonValue] = Field(default_factory=dict)
    dataset_version: str | None = None
    template_version: str | None = None
    binding_hashes: dict[str, str] = Field(default_factory=dict)
    result_digest: str = ""
    result_summary: dict[str, JsonValue] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(description=TZ_TIME_DESC)
    expires_at: datetime | None = Field(default=None, description=TZ_TIME_DESC)


class ProposalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str
    query: ProposalQueryRef


class ProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    at: datetime = Field(description=TZ_TIME_DESC)
    session_id: str
    source: str
    dataset_version: str | None = None
    object: str
    action: str
    action_desc: str
    tier: str
    conclusion: str
    evidence: list[ProposalEvidence]
    caveats: list[str]
    governance: str


# ---- v0.2 M3:观测口径(总览聚合 / 管道扩展)----

AlertSeverity = Literal["info", "warning", "critical"]


class OverviewSummary(BaseModel):
    """Dashboard 摘要计数;任一聚合不可检测时为 null,不用 0 掩盖错误。"""

    raw_rows: int | None = Field(description="当前配置范围内 raw 活跃行数合计")
    object_rows: int | None = Field(
        description="当前 source published 快照物理表行数合计")
    materialized_objects: int = Field(description="覆盖率分子:已物化对象数")
    template_objects: int = Field(description="覆盖率分母:模板对象数")
    quarantine_pending: int | None = Field(description="未处理隔离(resolved 为空)")
    last_run_at: datetime | None = Field(description=TZ_TIME_DESC)
    data_updated_at: datetime | None = Field(description=TZ_TIME_DESC)


class OverviewVersions(BaseModel):
    """版本信息;无已发布数据集时 dataset/object 为 null(页面显示"尚未发布")。"""

    app: str | None = Field(description="安装包元数据版本;取不到为 null(unknown)")
    template: str | None = Field(description="模板 pack 结构化 version")
    dataset: str | None = Field(
        description="当前 source 的 published dataset_version;无已发布则为 null")
    object: str | None = Field(
        description="已发布对象层标识(原子发布下等同 dataset_version);"
        "无已发布对象版本则为 null")


class BindingSummary(BaseModel):
    verified: int
    draft: int
    disabled: int


class OverviewAlert(BaseModel):
    """当前告警:由节点/服务/隔离/治理状态确定性聚合,非持久化实体。"""

    id: str = Field(description="kind+node/source/object 组成,刷新后稳定")
    severity: AlertSeverity
    title: str
    reason: str
    source: str | None
    observed_at: datetime | None = Field(description=TZ_TIME_DESC)
    detail_path: str | None = Field(description="详情入口;目标页未实现时为 null,不生死链")


class RecentRun(BaseModel):
    id: int
    run_type: RunType | None = Field(
        description="结构化运行类型;历史记录为 NULL(类型未知),不回填猜测")
    source: str
    status: str | None
    rows: int | None
    tables: int | None
    started_at: datetime | None = Field(description=TZ_TIME_DESC)
    finished_at: datetime | None = Field(description=TZ_TIME_DESC)


class SyncTrendPoint(BaseModel):
    bucket: datetime = Field(description="趋势桶起点(小时)," + TZ_TIME_DESC)
    rows: int
    runs: int


class CountNote(BaseModel):
    """数量口径说明:名称、口径、数据来源。"""

    name: str
    semantics: str
    source: str


# ---- v0.3 datasets 版本契约(M1 只读;M2-T01 冻结动作契约,引擎 T06 落地)----

DatasetStatus = Literal["building", "published", "failed", "retired"]
ObjectBuildStatus = Literal["building", "built", "failed", "published", "retired"]


class DatasetSummary(BaseModel):
    """数据集版本摘要;空元数据不得伪造版本号。"""

    dataset_version: str
    source: str
    template_version: str
    status: DatasetStatus
    built_at: datetime = Field(description=TZ_TIME_DESC)
    published_at: datetime | None = Field(
        default=None,
        description="已发布时必有可解析时间;库内为 null 表示尚未发布;"
        "损坏时间不得伪装成 null,应 500",
    )
    previous_dataset_version: str | None = None
    error: str | None = Field(
        default=None,
        description="安全摘要(已脱敏);原始内部错误不得出网",
    )
    error_id: str | None = Field(
        default=None,
        description="有内部错误时的稳定短标识,便于对照日志;无错误为 null",
    )
    object_manifest: list[str] | None = Field(
        default=None,
        description="构建时冻结的对象名清单;损坏/缺失为 null(完整性 fail-closed)",
    )


class ObjectVersionSummary(BaseModel):
    """数据集内单个对象构建版本。"""

    object: str
    object_version: str
    binding_hash: str
    row_count: int = Field(ge=0)
    batch_id: str | None = None
    build_table: str | None = None
    status: ObjectBuildStatus
    built_at: datetime = Field(description=TZ_TIME_DESC)
    published_at: datetime | None = Field(
        default=None,
        description="已发布时必有可解析时间;库内为 null 表示尚未发布;"
        "损坏时间不得伪装成 null,应 500",
    )


class DatasetDetail(DatasetSummary):
    """数据集版本详情,含对象版本列表。"""

    objects: list[ObjectVersionSummary] = Field(default_factory=list)


class DatasetActionResult(BaseModel):
    """publish/rollback 成功形状(M2-T06)。"""

    executed: bool
    dataset_version: str
    note: str = ""


# ---- v0.3 M3: mapping preview 契约(T01 冻结;T05 接入运行时)----

# 请求体放大上限:拒绝超大草稿制造 CPU/响应放大。
_PREVIEW_MAP_ENTRY_MAX = 512
_PREVIEW_MAP_SIZE_MAX = 128
_PREVIEW_TABLES_MAX = 16
_PREVIEW_DERIVED_FIELDS_MAX = 64
_PREVIEW_DERIVED_RULES_MAX = 64
_PREVIEW_NOTES_MAX = 2000
_PREVIEW_BATCH_ID_MAX = 128
_PREVIEW_SOURCE_MAX = 128

PreviewMapStr = Annotated[
    str,
    Field(max_length=_PREVIEW_MAP_ENTRY_MAX),
]
"""Preview 草稿 map 键/值字符串上限(512)。"""

PreviewTableStr = Annotated[
    str,
    Field(min_length=1, max_length=_PREVIEW_MAP_ENTRY_MAX),
]
"""草稿表名:非空且 ≤512。"""


MappingPreviewIssueReasonCode = Literal[
    "enum_unmapped",
    "enum_invalid",
    "type_coercion",
    "derived_unmatched",
    "derived_invalid_enum",
    "business_key_missing",
    "business_key_duplicate",
]

MappingPreviewErrorReasonCode = Literal[
    "unauthorized",
    "token_not_configured",
    "object_not_found",
    "source_not_found",
    "raw_table_not_found",
    "sample_batch_not_found",
    "current_binding_unavailable",
    "raw_unavailable",
    "draft_invalid",
    "sample_invalid",
    "anchor_changed",
    "preview_failed",
]

MappingPreviewRowStatus = Literal["mapped", "quarantined"]
MappingPreviewMode = Literal["current", "draft"]
MappingPreviewDiffState = Literal["available", "unavailable"]
MappingPreviewDiffReason = Literal["no_current_binding"]


class MappingPreviewSample(BaseModel):
    """样本选择参数:limit/offset 有界;可选精确 batch_id。"""

    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="样本行数上限,1..200,默认 50",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=10000,
        description="样本偏移,0..10000,默认 0",
    )
    batch_id: str | None = Field(
        default=None,
        max_length=_PREVIEW_BATCH_ID_MAX,
        description="可选;按锚表 _d2a_batch_id 精确筛选,最长 128",
    )


class MappingPreviewDeriveRule(BaseModel):
    """Preview 草稿派生规则;形状与 DeriveRule 一致,附加放大上限。"""

    when: dict[PreviewMapStr, PreviewMapStr | None] = Field(
        default_factory=dict,
        max_length=32,
        description="条件:字段→值(None=any);最多 32 项,键/值各 ≤512",
    )
    value: PreviewMapStr


class MappingPreviewDerivedField(BaseModel):
    """Preview 草稿派生字段;形状与 DerivedField 一致,规则最多 64 条。"""

    rules: list[MappingPreviewDeriveRule] = Field(
        default_factory=list,
        max_length=_PREVIEW_DERIVED_RULES_MAX,
        description="有序规则列表,最多 64 条",
    )
    default: PreviewMapStr | None = None


class MappingPreviewDraftBinding(BaseModel):
    """一次性草稿 binding:draft-only,拒绝客户端伪造 status。

    服务端后续任务构造 status=draft 的 SourceBinding;本模型不含 status。
    """

    model_config = ConfigDict(extra="forbid")

    tables: list[PreviewTableStr] = Field(
        min_length=1,
        max_length=_PREVIEW_TABLES_MAX,
        description="raw 表白名单引用,1..16;tables[0] 为锚表;表名非空",
    )
    key_map: dict[PreviewMapStr, PreviewMapStr] = Field(
        default_factory=dict,
        max_length=_PREVIEW_MAP_SIZE_MAX,
        description="业务键映射,最多 128 项;键/值各 ≤512",
    )
    field_map: dict[PreviewMapStr, PreviewMapStr] = Field(
        default_factory=dict,
        max_length=_PREVIEW_MAP_SIZE_MAX,
        description="字段映射,最多 128 项;键/值各 ≤512",
    )
    derived: dict[PreviewMapStr, MappingPreviewDerivedField] = Field(
        default_factory=dict,
        max_length=_PREVIEW_DERIVED_FIELDS_MAX,
        description="派生字段,最多 64 个;每字段规则最多 64",
    )
    watermark: PreviewMapStr | None = None
    notes: str = Field(
        default="",
        max_length=_PREVIEW_NOTES_MAX,
        description="草稿备注,最长 2000",
    )


class MappingPreviewRequest(BaseModel):
    """POST /api/mappings/{object}/preview 请求体。"""

    source: str = Field(
        max_length=_PREVIEW_SOURCE_MAX,
        description="数据源标识,最长 128",
    )
    sample: MappingPreviewSample = Field(default_factory=MappingPreviewSample)
    draft_binding: MappingPreviewDraftBinding | None = None


class MappingPreviewError(BaseModel):
    """Preview 安全错误:前端按 status/reason_code 分支,不解析中文 detail。"""

    status: int = Field(description="HTTP 状态码")
    reason_code: MappingPreviewErrorReasonCode
    detail: str
    error_id: str | None = Field(
        description="内部失败时的稳定短标识;非 500 为 null(必填可空)",
    )


class MappingPreviewIssue(BaseModel):
    reason_code: MappingPreviewIssueReasonCode
    field: str | None = Field(
        description="问题字段;无具体字段时为 null(必填可空)",
    )
    detail: str = Field(description="安全摘要;不含 SQL/物理表/traceback/未脱敏原值")
    source_value: str | None = None


class MappingPreviewRow(BaseModel):
    sample_row_id: str = Field(description="样本内 ordinal+摘要;不泄露业务键")
    status: MappingPreviewRowStatus
    output: dict[str, JsonValue] = Field(default_factory=dict)
    issues: list[MappingPreviewIssue] = Field(default_factory=list)


class MappingPreviewSummary(BaseModel):
    total: int = Field(ge=0)
    mapped: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    quarantine_rate: float = Field(ge=0, le=1)
    would_trip_breaker: bool = Field(
        description="仅报告;Preview 不抛熔断、不写数据",
    )


class MappingPreviewEnumGap(BaseModel):
    field: str
    source_value: str
    count: int = Field(ge=0)


class MappingPreviewBusinessKeyIssues(BaseModel):
    missing: int = Field(ge=0)
    duplicate: int = Field(ge=0)
    scope: Literal["sample"] = Field(
        default="sample",
        description="重复检测仅覆盖本次样本,样本外未检查",
    )


class MappingPreviewDerivedRuleHit(BaseModel):
    index: int = Field(ge=0)
    hit_count: int = Field(ge=0)


class MappingPreviewDerivedCoverage(BaseModel):
    field: str
    eligible_rows: int = Field(ge=0)
    matched_rows: int = Field(ge=0)
    default_hits: int = Field(ge=0)
    unmatched_rows: int = Field(ge=0)
    row_coverage: float | None = Field(
        description="分母为零时为 null,不伪装为 0% 或 100%(必填可空)",
    )
    rules_total: int = Field(ge=0)
    rules_hit: int = Field(ge=0)
    rules: list[MappingPreviewDerivedRuleHit] = Field(default_factory=list)


class MappingPreviewEvaluation(BaseModel):
    """current 或 candidate 一侧的评估结果。"""

    summary: MappingPreviewSummary
    rows: list[MappingPreviewRow] = Field(default_factory=list)
    enum_gaps: list[MappingPreviewEnumGap] = Field(default_factory=list)
    business_key_issues: MappingPreviewBusinessKeyIssues
    derived_coverage: list[MappingPreviewDerivedCoverage] = Field(default_factory=list)


class MappingPreviewSampleInfo(BaseModel):
    """响应中的样本证据块。"""

    anchor_table: str
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    requested_batch_id: str | None = Field(
        description="请求的 batch_id;未指定时为 null(必填可空)",
    )
    sample_batch_ids: list[str] = Field(default_factory=list)
    sampled_rows: int = Field(ge=0)
    sample_fingerprint: str


class MappingPreviewDiffField(BaseModel):
    field: str
    before: JsonValue | None = None
    after: JsonValue | None = None


class MappingPreviewDiffRow(BaseModel):
    sample_row_id: str
    status_before: MappingPreviewRowStatus | None = None
    status_after: MappingPreviewRowStatus | None = None
    fields: list[MappingPreviewDiffField] = Field(default_factory=list)


class MappingPreviewDiffSummary(BaseModel):
    rows_changed: int = Field(ge=0)
    status_changed: int = Field(ge=0)
    fields_changed: int = Field(ge=0)


class MappingPreviewDiff(BaseModel):
    state: MappingPreviewDiffState
    reason: MappingPreviewDiffReason | None = Field(
        description="unavailable 时为 no_current_binding;available 时为 null(必填可空)",
    )
    summary: MappingPreviewDiffSummary
    rows: list[MappingPreviewDiffRow] = Field(default_factory=list)


class MappingPreviewResponse(BaseModel):
    """POST /api/mappings/{object}/preview 成功形状。"""

    object: str
    source: str
    mode: MappingPreviewMode
    template_version: str
    current_binding_hash: str | None = Field(
        description="当前启用 binding 的 hash;无 current 时为 null(必填可空)",
    )
    candidate_binding_hash: str
    sample: MappingPreviewSampleInfo
    current: MappingPreviewEvaluation | None = Field(
        description="当前 binding 试算;无 current 时为 null(必填可空)",
    )
    candidate: MappingPreviewEvaluation
    diff: MappingPreviewDiff
    warnings: list[str]


# ---- v0.3 M4:字段级血缘契约(T01 冻结;查询实现见 T07)----

LineageTraceState = Literal["available", "unavailable"]

LineageUnavailableReason = Literal["lineage_not_recorded"]

LineageFieldUnavailableReason = Literal[
    "property_unmapped",
    "join_target_missing",
    "source_evidence_unavailable",
]

LineageInputRole = Literal["value", "join_fk", "derived_condition"]

LineageStepKind = Literal[
    "read",
    "join",
    "map",
    "coerce",
    "derived_rule",
    "derived_default",
]

LineageErrorReasonCode = Literal[
    "unauthorized",
    "token_not_configured",
    "object_not_found",
    "field_not_found",
    "record_not_found",
    "dataset_not_published",
    "snapshot_corrupt",
    "lineage_incomplete",
    "lineage_key_invalid",
    "lineage_query_failed",
]

ValueEvidenceKind = Literal["scalar", "null", "bytes", "truncated"]


class ValueEvidence(BaseModel):
    """稳定值证据:不使用 repr();BLOB/超长文本只留摘要。"""

    kind: ValueEvidenceKind
    value: JsonValue | None = Field(
        default=None,
        description="scalar/null 的 JSON 标量;bytes/truncated 时为 null",
    )
    preview: str | None = Field(
        default=None,
        description="bytes/truncated 的有界预览;scalar/null 时为 null",
    )
    sha256: str | None = Field(
        default=None,
        description="bytes/truncated 的完整 SHA-256 hex;其它为 null",
    )
    length: int | None = Field(
        default=None,
        ge=0,
        description="原值字节/字符长度;scalar/null 可为 null",
    )


class ObjectLineageStep(BaseModel):
    """实际执行过的有序转换步骤:read → join? → map? → coerce? → derived?。"""

    kind: LineageStepKind
    before: ValueEvidence | None = None
    after: ValueEvidence | None = None
    map_hit: bool | None = Field(
        default=None,
        description="map 步骤是否命中声明项;非 map 为 null",
    )
    coerce_type: str | None = Field(
        default=None,
        description="coerce 目标类型;非 coerce 为 null",
    )
    derived_rule_index: int | None = Field(
        default=None,
        ge=0,
        description="derived_rule 命中的声明顺序下标;其它为 null",
    )
    derived_when: dict[str, JsonValue] | None = Field(
        default=None,
        description="derived_rule 实际命中的条件映射;其它为 null",
    )
    detail: str | None = Field(
        default=None,
        description="安全摘要;不含 SQL/Token/未脱敏原值",
    )


class ObjectLineageInput(BaseModel):
    """字段输入边:raw 值、join 外键或 derived 条件。"""

    role: LineageInputRole
    source_table: str | None = None
    source_column: str | None = None
    source_pk: list[list[JsonValue]] | None = Field(
        default=None,
        description="源记录主键 pair 数组;未知时为 null",
    )
    source_value: ValueEvidence | None = None
    extract_batch_id: str | None = None
    join: dict[str, JsonValue] | None = Field(
        default=None,
        description="join 两端关系(锚 FK/目标 PK 等);非 join 为 null",
    )


class ObjectLineageField(BaseModel):
    property: str
    display_name: str
    final_value: ValueEvidence | None = Field(
        description="字段最终值证据;unavailable 时可为 null(必填可空)",
    )
    state: LineageTraceState
    reason_code: LineageFieldUnavailableReason | None = Field(
        description="unavailable 时的字段原因;available 时为 null(必填可空)",
    )
    steps: list[ObjectLineageStep] = Field(default_factory=list)
    inputs: list[ObjectLineageInput] = Field(default_factory=list)


class ObjectLineageResponse(BaseModel):
    """GET /api/objects/{object}/{key}/lineage 成功形状。

    旧 published(lineage_schema_version=NULL)返回 state=unavailable +
    reason_code=lineage_not_recorded,不伪造空字段。
    """

    state: LineageTraceState
    reason_code: LineageUnavailableReason | None = Field(
        description="记录级 unavailable 原因;available 时为 null(必填可空)",
    )
    source: str | None = Field(
        description="主数据源;unavailable 旧版可为 null(必填可空)",
    )
    object: str
    display_name: str
    object_key: list[list[JsonValue]]
    key_token: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    dataset_version: str | None = None
    object_version: str | None = None
    template_version: str | None = None
    binding_hash: str | None = None
    binding_status: BindingStatus | None = None
    map_batch_id: str | None = None
    fields: list[ObjectLineageField]
    warnings: list[str]
    generated_at: datetime = Field(description=TZ_TIME_DESC)


class ObjectLineageError(BaseModel):
    """字段血缘安全错误:前端按 status/reason_code 分支,不解析中文 detail。"""

    status: int = Field(description="HTTP 状态码")
    reason_code: LineageErrorReasonCode
    detail: str
    error_id: str | None = Field(
        description="内部失败时的稳定短标识;非 500 为 null(必填可空)",
    )
