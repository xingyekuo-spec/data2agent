"""ERP 元数据发现:领域模型、协议、扫描缓存与 discoverer 工厂。

本模块不包含任何具体数据库系统表 SQL,也不按 adapter 名称拼接查询。
具体实现注册到 ``DISCOVERER_REGISTRY``,由 ``build_discoverer`` 解析。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, runtime_checkable

from ...shared.config import SourceConfig

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WATERMARK_NAME_HINTS = (
    "UPDATE_TIME", "UPDATED_AT", "MODIFIED_AT", "LAST_MODIFIED",
    "LAST_MODIFIED_DATE", "MODIFY_DATE", "MODIFIED_DATE", "CHANGE_DATE",
    "LAST_UPDATE", "ROWVERSION", "VERSION_STAMP",
)
_WATERMARK_TYPE_HINTS = (
    "datetime", "datetime2", "smalldatetime", "date", "timestamp",
    "rowversion", "time",
)

# 扫描任务约束(进程内)
DEFAULT_SCAN_TTL_SECONDS = 1800.0
DEFAULT_MAX_SCAN_RECORDS = 8
DEFAULT_MAX_ACTIVE_SCANS = 1
DEFAULT_SCAN_DEADLINE_SECONDS = 300.0
DEFAULT_SCAN_TABLE_LIMIT = 2000


class MetadataDiscoveryUnsupported(Exception):
    """当前 adapter 不支持元数据发现。"""

    code = "metadata_discovery_unsupported"
    suggestion = (
        "确认 connect.yaml 中 adapter 为已注册的元数据发现类型，"
        "或升级中间机包后再试"
    )


_DEFAULT_SUGGESTIONS: dict[str, str] = {
    "connection_failed": "检查 ERP 服务器地址、端口、账号密码，以及本机到数据库的网络连通",
    "timeout": "增大连接/查询超时，或在业务低峰重试；确认数据库未过载",
    "permission_denied": "为只读账号授予所需元数据/表 SELECT 与查看定义权限后重试",
    "scan_busy": "等待当前扫描完成后再发起新扫描",
    "not_found": "确认 schema/表名拼写，或先刷新元数据扫描",
    "table_missing": "确认 schema/表名拼写，或先刷新元数据扫描",
    "table_not_found": "确认 schema/表名拼写，或先刷新元数据扫描",
    "invalid_identifier": "仅使用字母、数字、下划线组成的合法 SQL 标识符",
    "dsn_missing": "在 secrets 或环境中配置对应 DSN 环境变量后重启中间机管理进程",
    "metadata_stale": "先 POST /api/metadata/scans 完成一次扫描后再查询表列表",
    "metadata_discovery_unsupported": (
        "确认 connect.yaml 中 adapter 为已注册的元数据发现类型，"
        "或升级中间机包后再试"
    ),
    "table_errors": "打开表详情查看单表错误，修复权限或连通性后重新扫描",
    "key_missing": "在抽取计划中填写存在的业务键/主键列后重试",
    "key_not_unique": "更换唯一键组合，或先清洗源表重复/空值",
    "key_check_failed": "查看日志中的脱敏错误，核对表权限与键列后重试",
    "watermark_missing": "选择存在的水位列（通常为更新时间类字段）",
    "watermark_invalid": "更换合适的水位列，或确认字段类型适合增量抽取",
}


class MetadataError(Exception):
    """元数据发现或校验失败(已脱敏)。"""

    def __init__(self, code: str, message: str, suggestion: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion or _DEFAULT_SUGGESTIONS.get(
            code, "请查看管理界面日志后重试")


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    ordinal: int
    sql_type: str
    nullable: bool


@dataclass(frozen=True)
class KeyMeta:
    name: str
    columns: tuple[str, ...]
    kind: str  # primary | unique_constraint | unique_index


@dataclass(frozen=True)
class ForeignKeyMeta:
    name: str
    columns: tuple[str, ...]
    referenced_schema: str | None
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSummary:
    schema: str
    name: str
    object_type: str  # table | view
    estimated_rows: int | None
    primary_key: tuple[str, ...]
    unique_keys: tuple[KeyMeta, ...]
    watermark_candidates: tuple[str, ...]
    error_code: str | None = None
    error_detail: str | None = None
    error_suggestion: str | None = None


@dataclass(frozen=True)
class TableDetail:
    schema: str
    name: str
    object_type: str
    columns: tuple[ColumnMeta, ...]
    primary_key: tuple[str, ...]
    unique_keys: tuple[KeyMeta, ...]
    foreign_keys: tuple[ForeignKeyMeta, ...]
    estimated_rows: int | None
    watermark_candidates: tuple[str, ...]
    schema_fingerprint: str
    scanned_at: str


@dataclass(frozen=True)
class KeyCheckResult:
    ok: bool
    code: str
    detail: str
    null_count: int | None = None
    duplicate_groups: int | None = None


@dataclass(frozen=True)
class WatermarkCheckResult:
    ok: bool
    code: str
    detail: str
    sql_type: str | None = None
    candidate: bool = False


@dataclass
class ScanRecord:
    scan_id: str
    source: str
    status: str  # running | completed | partial | failed | timeout
    # 单调时钟:仅用于 TTL / 驱逐
    created_mono: float
    finished_mono: float | None = None
    # UTC epoch 秒:仅用于 API 展示
    created_at_utc: float = 0.0
    finished_at_utc: float | None = None
    error_code: str | None = None
    error_detail: str | None = None
    error_suggestion: str | None = None
    table_errors: int = 0
    tables: list[TableSummary] = field(default_factory=list)
    details: dict[tuple[str, str], TableDetail] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "source": self.source,
            "status": self.status,
            "created_at": _iso_utc(self.created_at_utc),
            "finished_at": (
                _iso_utc(self.finished_at_utc) if self.finished_at_utc is not None else None
            ),
            "table_count": len(self.tables),
            "table_errors": self.table_errors,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "error_suggestion": self.error_suggestion,
            "suggestion": self.error_suggestion,
        }


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def schema_fingerprint(columns: list[ColumnMeta], primary_key: list[str],
                       unique_keys: list[KeyMeta]) -> str:
    parts = [
        f"{c.ordinal}:{c.name}:{c.sql_type}:{'N' if c.nullable else '1'}"
        for c in columns
    ]
    parts.append("pk=" + ",".join(primary_key))
    for key in unique_keys:
        parts.append(f"{key.kind}:{key.name}:" + ",".join(key.columns))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def suggest_watermark_candidates(columns: list[ColumnMeta]) -> list[str]:
    out: list[str] = []
    for col in columns:
        name_u = col.name.upper()
        type_l = col.sql_type.lower()
        name_hit = any(h in name_u for h in _WATERMARK_NAME_HINTS)
        type_hit = any(h in type_l for h in _WATERMARK_TYPE_HINTS)
        if name_hit or type_hit:
            out.append(col.name)
    return out


def validate_identifier(name: str, *, kind: str = "标识符") -> str:
    if not _IDENT_RE.match(name):
        raise MetadataError("invalid_identifier", f"非法{kind} '{name}'")
    return name


def normalize_schema(schema: str | None, *, default: str = "dbo") -> str:
    """规范化抽取计划 / 元数据比对用的 schema。"""
    if schema is None or not str(schema).strip():
        return default
    return str(schema).strip()


def extraction_plan_keys(
    tables: dict[str, Any] | None,
    *,
    default_schema: str = "dbo",
) -> set[tuple[str, str]]:
    """从 tables 配置生成 (schema, table) 集合。"""
    if not tables:
        return set()
    out: set[tuple[str, str]] = set()
    for name, spec in tables.items():
        schema = default_schema
        if hasattr(spec, "schema"):
            schema = normalize_schema(getattr(spec, "schema"), default=default_schema)
        elif isinstance(spec, dict):
            schema = normalize_schema(spec.get("schema"), default=default_schema)
        # sqlite 默认 schema 为 main
        out.add((schema, name))
    return out


def in_extraction_plan(
    schema: str,
    table: str,
    planned: set[tuple[str, str]],
    *,
    default_schema: str = "dbo",
) -> bool:
    return (normalize_schema(schema, default=default_schema), table) in planned


@runtime_checkable
class MetadataDiscoverer(Protocol):
    """与数据库类型无关的元数据发现协议。"""

    def default_schema(self) -> str:
        """该数据源配置缺省 schema 时使用的规范名。"""
        ...

    def list_schemas(self) -> list[str]:
        ...

    def list_tables(
        self,
        *,
        schema: str | None = None,
        q: str | None = None,
        object_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[TableSummary], int]:
        ...

    def get_table(self, schema: str, table: str) -> TableDetail:
        ...

    def check_key(self, schema: str, table: str, columns: list[str],
                  *, timeout_seconds: float = 30) -> KeyCheckResult:
        ...

    def check_watermark(self, schema: str, table: str, column: str) -> WatermarkCheckResult:
        ...

    def close(self) -> None:
        ...


DiscovererFactory = Callable[[SourceConfig], MetadataDiscoverer]


@dataclass(frozen=True)
class DiscovererCapability:
    """discoverer 注册能力;API 通过此表解析,不按 adapter 名硬编码行为。"""

    factory: DiscovererFactory
    default_schema: str


DISCOVERER_REGISTRY: dict[str, DiscovererCapability] = {}


def register_discoverer(
    adapter: str,
    factory: DiscovererFactory,
    *,
    default_schema: str,
) -> None:
    DISCOVERER_REGISTRY[adapter] = DiscovererCapability(
        factory=factory, default_schema=default_schema)


def discoverer_default_schema(adapter: str) -> str:
    """解析已注册 discoverer 的默认 schema,不在调用方写死 adapter 分支。"""
    _ensure_discoverers_registered()
    cap = DISCOVERER_REGISTRY.get(adapter)
    if cap is None:
        raise MetadataDiscoveryUnsupported(
            f"adapter '{adapter}' 不支持元数据发现")
    return cap.default_schema


def build_discoverer(scfg: SourceConfig) -> MetadataDiscoverer:
    """按已注册能力构造 discoverer;未注册则抛稳定错误码。"""
    _ensure_discoverers_registered()
    cap = DISCOVERER_REGISTRY.get(scfg.adapter)
    if cap is None:
        raise MetadataDiscoveryUnsupported(
            f"adapter '{scfg.adapter}' 不支持元数据发现")
    return cap.factory(scfg)


def _ensure_discoverers_registered() -> None:
    """懒加载注册表,避免调用方遗漏 ``import data2agent.middle.extract.discoverers``。"""
    if DISCOVERER_REGISTRY:
        return
    from . import discoverers as _discoverers  # noqa: F401


def is_odbc_timeout_message(message: str) -> bool:
    low = message.lower()
    return any(k in low for k in (
        "timeout", "timed out", "hyt00", "08s01",
        "登录超时", "连接超时", "查询超时", "操作过时", "等待的操作过时",
        "(258)", "error 258",
    ))


def map_odbc_error(exc: BaseException) -> MetadataError:
    """将 ODBC/驱动异常映射为稳定、脱敏的 MetadataError。

    对外消息不得包含 DSN、服务器地址、库名、账号或密码片段。
    """
    low = str(exc).lower()

    if any(k in low for k in ("login failed", "authentication", "password", "18456")):
        return MetadataError(
            "connection_failed", "数据库认证失败",
            "核对只读账号与密码，确认账号未被锁定且允许从中间机主机登录")
    if is_odbc_timeout_message(str(exc)):
        return MetadataError(
            "timeout", "数据库连接或查询超时",
            "检查网络与数据库负载，必要时增大超时并在低峰重试")
    if any(k in low for k in (
        "permission", "denied", "not authorized", "229", "230",
        "select permission", "view definition",
    )):
        return MetadataError(
            "permission_denied", "元数据权限不足",
            "为只读账号授予相关库的 VIEW DEFINITION / SELECT 权限后重试")
    if any(k in low for k in (
        "cannot open", "server does not exist", "network", "08001",
        "could not connect", "named pipes", "connection refused",
        "no such host", "unreachable",
    )):
        return MetadataError(
            "connection_failed", "无法连接数据库",
            "检查服务器地址、端口、防火墙与 SQL Server 是否允许远程连接")
    return MetadataError(
        "connection_failed", "数据库访问失败",
        "查看管理界面日志中的脱敏错误，核对连接配置后重试")


class ScanStore:
    """进程内扫描结果缓存:按 scan_id 隔离,带 TTL、活动槽位与总时限。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_SCAN_TTL_SECONDS,
        max_scans: int = DEFAULT_MAX_SCAN_RECORDS,
        max_active_scans: int = DEFAULT_MAX_ACTIVE_SCANS,
        scan_deadline_seconds: float = DEFAULT_SCAN_DEADLINE_SECONDS,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_scans = max_scans
        self.max_active_scans = max_active_scans
        self.scan_deadline_seconds = scan_deadline_seconds
        self._lock = threading.Lock()
        self._scans: dict[str, ScanRecord] = {}
        self._active: set[str] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_active_scans),
            thread_name_prefix="metadata-scan",
        )

    def purge_expired(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, rec in self._scans.items()
            if now - rec.created_mono > self.ttl_seconds and sid not in self._active
        ]
        for sid in expired:
            self._scans.pop(sid, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def try_begin(self, source: str) -> ScanRecord:
        """创建扫描记录并占用活动槽位;槽位满时抛 MetadataError(scan_busy)。"""
        with self._lock:
            self.purge_expired()
            if len(self._active) >= self.max_active_scans:
                raise MetadataError(
                    "scan_busy",
                    f"已有 {len(self._active)} 个扫描进行中,请等待完成后再试",
                    "等待当前扫描结束后再发起；勿并行打开多个扫描请求",
                )
            # 只驱逐非活动记录
            inactive = [
                r for r in self._scans.values() if r.scan_id not in self._active
            ]
            while len(self._scans) >= self.max_scans and inactive:
                oldest = min(inactive, key=lambda r: r.created_mono)
                self._scans.pop(oldest.scan_id, None)
                inactive = [
                    r for r in self._scans.values() if r.scan_id not in self._active
                ]
            if len(self._scans) >= self.max_scans:
                raise MetadataError("scan_busy", "扫描缓存已满且仍有活动任务")
            rec = ScanRecord(
                scan_id=uuid.uuid4().hex,
                source=source,
                status="running",
                created_mono=time.monotonic(),
                created_at_utc=_utc_now(),
            )
            self._scans[rec.scan_id] = rec
            self._active.add(rec.scan_id)
            return rec

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

    def get(self, scan_id: str) -> ScanRecord | None:
        with self._lock:
            self.purge_expired()
            return self._scans.get(scan_id)

    def latest_for_source(self, source: str) -> ScanRecord | None:
        """返回该源最近一条记录(含 running),供进度查询以外的兼容用途。"""
        with self._lock:
            self.purge_expired()
            matches = [r for r in self._scans.values() if r.source == source]
            if not matches:
                return None
            return max(matches, key=lambda r: r.created_mono)

    def latest_completed_for_source(self, source: str) -> ScanRecord | None:
        """返回最近一次可用缓存(completed 或 partial),不被 running 遮蔽。"""
        with self._lock:
            self.purge_expired()
            matches = [
                r for r in self._scans.values()
                if r.source == source and r.status in ("completed", "partial")
            ]
            if not matches:
                return None
            return max(matches, key=lambda r: r.created_mono)

    def complete(
        self,
        scan_id: str,
        tables: list[TableSummary],
        details: dict[tuple[str, str], TableDetail],
        *,
        status: str = "completed",
        table_errors: int = 0,
        error_code: str | None = None,
        error_detail: str | None = None,
        error_suggestion: str | None = None,
    ) -> None:
        with self._lock:
            rec = self._scans.get(scan_id)
            if rec is None:
                self._active.discard(scan_id)
                return
            rec.status = status
            rec.finished_mono = time.monotonic()
            rec.finished_at_utc = _utc_now()
            rec.tables = tables
            rec.details = details
            rec.table_errors = table_errors
            rec.error_code = error_code
            rec.error_detail = error_detail
            if error_code and not error_suggestion:
                error_suggestion = _DEFAULT_SUGGESTIONS.get(error_code)
            rec.error_suggestion = error_suggestion
            self._active.discard(scan_id)

    def fail(
        self,
        scan_id: str,
        code: str,
        detail: str,
        *,
        status: str = "failed",
        suggestion: str | None = None,
    ) -> None:
        with self._lock:
            rec = self._scans.get(scan_id)
            if rec is None:
                self._active.discard(scan_id)
                return
            rec.status = status
            rec.finished_mono = time.monotonic()
            rec.finished_at_utc = _utc_now()
            rec.error_code = code
            rec.error_detail = detail
            rec.error_suggestion = suggestion or _DEFAULT_SUGGESTIONS.get(
                code, "查看管理界面日志后重试")
            self._active.discard(scan_id)

    def clear(self) -> None:
        with self._lock:
            self._scans.clear()
            self._active.clear()
