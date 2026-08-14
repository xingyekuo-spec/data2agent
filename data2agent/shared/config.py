"""connect.yaml 配置:解析与校验(pydantic),窗口与时长工具。

凭据纪律:配置文件里只允许放环境变量名(dsn_env),绝不放连接串本体;
sqlite 源(开发 / 参考链)例外地允许直接写路径(无凭据可泄露)。
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_seconds(raw: str | int | float) -> float:
    """'30m' / '3d' / 纯数字(秒)→ 秒。"""
    if isinstance(raw, (int, float)):
        return float(raw)
    m = _DURATION_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无法解析时长 '{raw}'(支持 30s / 30m / 2h / 3d 或纯秒数)")
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def parse_window(raw: str) -> tuple[dtime, dtime]:
    try:
        start, end = raw.split("-")
        return (dtime.fromisoformat(start.strip()), dtime.fromisoformat(end.strip()))
    except ValueError as e:
        raise ValueError(f"窗口格式须为 'HH:MM-HH:MM',got '{raw}'") from e


def in_window(now: dtime, windows: list[str]) -> bool:
    """错峰窗口判断;支持跨零点(如 22:00-06:30)。空列表 = 不限。"""
    if not windows:
        return True
    for raw in windows:
        start, end = parse_window(raw)
        if start <= end:
            if start <= now < end:
                return True
        elif now >= start or now < end:
            return True
    return False


class RateConfig(BaseModel):
    model_config = {"extra": "forbid"}
    # 平台协议单批上限为 50k；0/负数会导致死循环或节流数学错误。
    batch_size: int = Field(default=5000, ge=1, le=50_000)
    rows_per_second: int = Field(default=2000, ge=1, le=1_000_000)


class SinkConfig(BaseModel):
    """raw 落地出口(§12.3):local=写本地库(仅限内部开发/参考链/测试,非交付形态);
    http=推给平台(生产中间机唯一允许的形态)。"""

    model_config = {"extra": "forbid"}
    type: Literal["local", "http"] = "local"
    url: str | None = None                # http:平台接收端点(如 https://平台:8850)
    token_env: str | None = None          # http:Token 所在环境变量(凭据不落配置)
    allow_insecure_http: bool = False      # 仅显式开发/受控内网例外
    allow_unauthenticated: bool = False    # 仅显式开发例外
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    retries: int = Field(default=3, ge=1, le=10)
    ca_bundle: str | None = None            # 私有 CA PEM 路径；不禁用主机名校验


class SpoolConfig(BaseModel):
    """全量快照在中间机上的临时数据驻留策略。

    strict_stream 不写临时文件，但网络推送期间会继续持有源侧读取游标；
    encrypted_temp_volume 只允许写入已由现场确认具备静态加密的专用目录；
    temporary_file 保留旧行为，仅用于开发/测试。
    """

    model_config = {"extra": "forbid"}
    policy: Literal[
        "strict_stream", "encrypted_temp_volume", "temporary_file"
    ] = "temporary_file"
    directory: str | None = None
    encrypted_at_rest: bool = False

    @model_validator(mode="after")
    def policy_consistent(self):
        if self.policy == "encrypted_temp_volume":
            if not self.directory:
                raise ValueError(
                    "encrypted_temp_volume 必须配置 spool.directory")
            if not self.encrypted_at_rest:
                raise ValueError(
                    "encrypted_temp_volume 必须显式确认 encrypted_at_rest=true")
        if self.policy == "strict_stream":
            if self.directory:
                raise ValueError("strict_stream 不允许配置 spool.directory")
            if self.encrypted_at_rest:
                raise ValueError(
                    "strict_stream 不创建磁盘 spool，不允许配置 "
                    "spool.encrypted_at_rest")
        return self


class TableExtractConfig(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}
    mode: Literal["incremental", "full_refresh"]
    schema_name: str | None = Field(
        default=None,
        alias="schema",
        serialization_alias="schema",
    )                                      # SQL Server schema, 默认 dbo
    key_columns: list[str] | None = None   # 数据库 PK / 唯一索引 / 业务唯一键
    watermark: str | None = None           # incremental 必填, full_refresh 禁止
    start_date: str | None = None          # 抽取起始日期(仅 incremental;首轮从此日期起扫)
    schema_fingerprint: str | None = None  # 已确认字段结构摘要(sha256:...)
    validated_at: str | None = None        # 最近一次现场校验时间

    @property
    def schema(self) -> str | None:
        return self.schema_name

    @schema.setter
    def schema(self, value: str | None) -> None:
        self.schema_name = value

    @model_validator(mode="after")
    def _validate_mode_constraints(self):
        ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        if self.schema_name is not None and not ident.fullmatch(self.schema_name):
            raise ValueError(
                f"非法 schema '{self.schema_name}'(须为 SQL 标识符)")
        if self.mode == "incremental":
            if not self.watermark:
                raise ValueError("incremental 模式必须配置 watermark")
        if self.mode == "full_refresh" and self.watermark is not None:
            raise ValueError("full_refresh 模式不允许配置 watermark")
        if self.watermark is not None:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", self.watermark):
                raise ValueError(f"非法水位列名 '{self.watermark}'(须为 SQL 标识符)")
        if self.start_date is not None:
            if self.mode == "full_refresh":
                raise ValueError("full_refresh 模式不允许配置 start_date(无水位列可过滤)")
            if not re.match(
                r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$",
                self.start_date,
            ):
                raise ValueError(
                    f"非法 start_date '{self.start_date}'"
                    f"(格式:YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS])")
            try:
                datetime.fromisoformat(self.start_date.replace(" ", "T"))
            except ValueError as e:
                raise ValueError(
                    f"非法 start_date '{self.start_date}'") from e
        if self.key_columns is not None:
            for col in self.key_columns:
                if not ident.match(col):
                    raise ValueError(f"非法键列名 '{col}'(须为 SQL 标识符)")
            if len(self.key_columns) != len(set(self.key_columns)):
                raise ValueError("key_columns 不得包含重复列")
        return self


class SourceConfig(BaseModel):
    model_config = {"extra": "forbid"}
    adapter: str                          # sqlite_readonly / mssql_readonly
    dsn_env: str | None = None            # mssql:连接串所在环境变量
    path: str | None = None               # sqlite:源库路径
    tables: dict[str, TableExtractConfig] | None = None
    windows: list[str] = []               # 错峰窗口,空 = 不限
    rate: RateConfig = RateConfig()
    lookback: str = "3d"
    sync_every: str = "30m"
    sync_start_at: str | None = None       # "HH:MM",首轮自动抽取启动时间;None=服务启动即跑
    start_date: str | None = None          # 全局抽取起始日期(表级 start_date 未配置时的默认值)
    reconcile_at: str | None = None       # "HH:MM",每日 L1 对账;None 不排
    reconcile_deep_at: str | None = None  # "HH:MM",L2 修复
    reconcile_deep_day_of_week: str | None = None  # mon..sun;None=每日
    apply_after_sync: bool = True         # sink=http 时忽略(映射在平台侧)
    estimate_rows: bool = False           # COUNT 仅用于进度；生产默认关闭以减轻 ERP
    sink: SinkConfig = SinkConfig()
    spool: SpoolConfig = SpoolConfig()

    @field_validator("adapter")
    @classmethod
    def known_adapter(cls, v: str) -> str:
        if v not in ("sqlite_readonly", "mssql_readonly"):
            raise ValueError(f"未知适配器 '{v}'(可用:sqlite_readonly / mssql_readonly)")
        return v

    @field_validator("windows")
    @classmethod
    def windows_parse(cls, v: list[str]) -> list[str]:
        for w in v:
            start, end = parse_window(w)
            if start == end:
                raise ValueError(
                    f"窗口 '{w}' 起止相同，实际上永远不会运行")
        return v

    @field_validator("sync_every")
    @classmethod
    def sync_every_positive(cls, v: str) -> str:
        seconds = parse_duration_seconds(v)
        if seconds < 1:
            raise ValueError("sync_every 必须至少为 1 秒")
        return v

    @field_validator("lookback")
    @classmethod
    def lookback_nonnegative(cls, v: str) -> str:
        if parse_duration_seconds(v) < 0:
            raise ValueError("lookback 不能为负数")
        return v

    @field_validator("sync_start_at", mode="before")
    @classmethod
    def sync_start_at_parse(cls, v: str | None) -> str | None:
        if v is None:
            return None
        raw = str(v).strip()
        if not raw:
            return None
        if not re.match(r"^\d{2}:\d{2}$", raw):
            raise ValueError(f"sync_start_at 格式须为 'HH:MM',got '{v}'")
        try:
            dtime.fromisoformat(raw)
        except ValueError as e:
            raise ValueError(f"sync_start_at 格式须为 'HH:MM',got '{v}'") from e
        return raw

    @field_validator("reconcile_at", "reconcile_deep_at", mode="before")
    @classmethod
    def reconcile_time_parse(cls, v: str | None) -> str | None:
        if v is None:
            return None
        raw = str(v).strip()
        if not raw:
            return None
        try:
            parsed = dtime.fromisoformat(raw)
        except ValueError as e:
            raise ValueError(f"对账时间格式须为 'HH:MM',got '{v}'") from e
        if len(raw) != 5:
            raise ValueError(f"对账时间格式须为 'HH:MM',got '{v}'")
        return parsed.strftime("%H:%M")

    @field_validator("reconcile_deep_day_of_week", mode="before")
    @classmethod
    def reconcile_weekday_parse(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        raw = str(v).strip().lower()
        if raw not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
            raise ValueError("reconcile_deep_day_of_week 必须是 mon..sun")
        return raw

    @field_validator("start_date", mode="before")
    @classmethod
    def start_date_parse(cls, v: str | None) -> str | None:
        if v is None:
            return None
        raw = str(v).strip()
        if not raw:
            return None
        if not re.match(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$", raw):
            raise ValueError(
                f"非法 start_date '{v}'(格式:YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS])")
        try:
            datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError as e:
            raise ValueError(f"非法 start_date '{v}'") from e
        return raw

    @field_validator("tables")
    @classmethod
    def tables_valid_identifiers(cls, v):
        if v is None:
            return v
        import re
        ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
        folded: dict[str, str] = {}
        for name in v:
            if not ident.match(name):
                raise ValueError(f"非法表名 '{name}'(须为 SQL 标识符)")
            lower = name.casefold()
            if lower in folded:
                raise ValueError(
                    f"表名大小写冲突: '{name}' 与 '{folded[lower]}' 折叠后重复")
            folded[lower] = name
        return v

    @model_validator(mode="after")
    def reconcile_schedule_consistent(self):
        if self.reconcile_deep_day_of_week and not self.reconcile_deep_at:
            raise ValueError(
                "reconcile_deep_day_of_week 需要同时配置 reconcile_deep_at")
        return self

    def table_whitelist(self) -> set[str]:
        if self.tables is None:
            return set()
        return set(self.tables.keys())

    def table_watermarks(self) -> dict[str, str]:
        if self.tables is None:
            return {}
        return {
            table: spec.watermark
            for table, spec in self.tables.items()
            if spec.mode == "incremental" and spec.watermark
        }

    def table_key_columns(self) -> dict[str, list[str]]:
        if self.tables is None:
            return {}
        return {
            table: spec.key_columns
            for table, spec in self.tables.items()
            if spec.key_columns
        }

    def table_schemas(self) -> dict[str, str]:
        """表名 → schema；未显式配置时使用适配器默认值。"""
        if self.tables is None:
            return {}
        default = "main" if self.adapter == "sqlite_readonly" else "dbo"
        return {
            table: (spec.schema or default)
            for table, spec in self.tables.items()
        }

    def table_start_dates(self) -> dict[str, str]:
        """各表抽取起始日期(仅 incremental);表未配置 start_date 时回退到源级全局值。"""
        if self.tables is None:
            return {}
        out: dict[str, str] = {}
        for table, spec in self.tables.items():
            if spec.mode != "incremental":
                continue
            start = spec.start_date or self.start_date
            if start:
                out[table] = start
        return out

    def lookback_days(self) -> float:
        return parse_duration_seconds(self.lookback) / 86400

    def sync_every_seconds(self) -> float:
        return parse_duration_seconds(self.sync_every)

    def sync_start_datetime_after(self, now: datetime) -> datetime:
        """返回自动调度首轮时间。未配置时保持旧行为:服务启动即跑。"""
        if self.sync_start_at is None:
            return now
        target_time = dtime.fromisoformat(self.sync_start_at)
        target = datetime.combine(now.date(), target_time, tzinfo=now.tzinfo)
        if target < now:
            target += timedelta(days=1)
        return target


def is_loopback_url(url: str) -> bool:
    """sink.url 是否指向本机回环(127.0.0.0/8、::1、localhost)。"""
    host = urlparse(url or "").hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


class ConnectConfig(BaseModel):
    model_config = {"extra": "forbid"}
    templates: str = "templates"
    deployment_mode: Literal["production", "development", "test"] = "development"
    state_db: str = "state/middle-state.sqlite"
    sources: dict[str, SourceConfig]

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_landing_key(cls, value):
        """只读兼容旧 connect.yaml 的 landing；新写入统一使用 state_db。"""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy = data.pop("landing", None)
        current = data.get("state_db")
        if current is not None and legacy is not None and str(current) != str(legacy):
            raise ValueError("state_db 与旧 landing 同时存在且值不一致")
        if current is None and legacy is not None:
            data["state_db"] = legacy
        return data

    @property
    def landing(self) -> str:
        """迁移期内部兼容属性；新代码和配置应使用 state_db。"""
        return self.state_db

    def production_violations(self) -> list[str]:
        """返回生产数据驻留边界违规项；不在解析阶段抛错以便管理页展示。"""
        if self.deployment_mode != "production":
            return []
        violations: list[str] = []
        for name, source in self.sources.items():
            if source.sink.type != "http":
                violations.append(f"源 {name}:生产模式必须使用 sink.type=http")
            elif is_loopback_url(source.sink.url or ""):
                violations.append(
                    f"源 {name}:生产模式 sink.url 不得为本机回环地址——"
                    "中间机与平台应分机部署;单机调试请用 "
                    "deployment_mode: development")
            if source.spool.policy == "temporary_file":
                violations.append(
                    f"源 {name}:生产模式不得使用未受控 temporary_file spool")
        return violations


def assert_production_ready(cfg: ConnectConfig) -> None:
    """生产 connector 启动前 fail closed；管理 API 可先加载配置并展示违规项。"""
    violations = cfg.production_violations()
    if violations:
        raise ValueError("生产配置未就绪:" + ";".join(violations))


def config_revision(path: str | Path) -> str:
    """返回配置文件的 sha256 内容哈希，作为乐观锁修订号。"""
    content = Path(path).read_bytes()
    return "sha256:" + hashlib.sha256(content).hexdigest()


def load_config(path: str | Path) -> ConnectConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    cfg = ConnectConfig(**data)
    for name, s in cfg.sources.items():
        if s.adapter == "mssql_readonly" and not s.dsn_env:
            raise ValueError(f"源 {name}: mssql_readonly 必须配 dsn_env(凭据不落配置文件)")
        if s.adapter == "sqlite_readonly" and not (s.path or s.dsn_env):
            raise ValueError(f"源 {name}: sqlite_readonly 须配 path 或 dsn_env")
        if s.tables is None:
            raise ValueError(f"源 {name}: 缺少 tables 配置。")
        if s.sink.type == "http" and not s.sink.url:
            raise ValueError(f"源 {name}: sink.type=http 必须配 sink.url(平台接收端点)")
        if s.sink.type == "http":
            parsed = urlparse(s.sink.url or "")
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"源 {name}: sink.url 必须是有效 http(s) URL")
            loopback = is_loopback_url(s.sink.url or "")
            if (
                parsed.scheme != "https"
                and not loopback
                and not s.sink.allow_insecure_http
            ):
                raise ValueError(
                    f"源 {name}: 非本机 sink.url 必须使用 HTTPS；"
                    "受控开发环境可显式设 allow_insecure_http: true")
            if (
                not s.sink.token_env
                and not loopback
                and not s.sink.allow_unauthenticated
            ):
                raise ValueError(
                    f"源 {name}: 非本机 HTTP sink 必须配置 token_env；"
                    "开发环境可显式设 allow_unauthenticated: true")
    return cfg


class PlatformConfig(BaseModel):
    """平台配置模型:只包含平台职责字段,不含 ERP 连接或抽取计划。"""

    model_config = {"extra": "forbid"}
    templates: str = "templates"
    landing: str = "landing/factory.sqlite"
    # 可选:对外告知中间机的 ingest 端点(反代域名);缺省按访问主机推导
    ingest_url: str | None = None


def load_platform_config(path: str | Path) -> PlatformConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    # Reject sources block — platform must not carry ERP config
    if "sources" in data:
        raise ValueError("平台配置不得包含 sources 字段;抽取计划仅属于中间机 connect.yaml")
    return PlatformConfig(**data)
