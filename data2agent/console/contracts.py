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
    tool: Literal["query_objects", "query_metrics"]
    params: dict[str, JsonValue] = Field(default_factory=dict)


McpLabReasonCode = Literal[
    "invalid_params",
    "unknown_target",
    "not_materialized",
    "not_published",
    "query_expired",
    "tier_forbidden",
    "rate_limited",
    "mcp_unavailable",
    "execution_failed",
]


class McpQueryMeta(BaseModel):
    """查询公共元数据。v0.2 仅承诺 Console 进程级 evidence_scope。"""

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
    evidence_scope: Literal["process"] = Field(
        default="process",
        description="v0.2:query ID 仅在当前 Console 进程/配置签名内有效",
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


class TemplateObject(BaseModel):
    object: str
    display_name: str
    description: str | None = None
    domain: str | None = None
    keys: list[str]
    properties: list[TemplateProperty]
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
