"""connect.yaml 配置:解析与校验(pydantic),窗口与时长工具。

凭据纪律:配置文件里只允许放环境变量名(dsn_env),绝不放连接串本体;
sqlite 源(开发 / 参考链)例外地允许直接写路径(无凭据可泄露)。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator, model_validator

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
    batch_size: int = 5000
    rows_per_second: int = 2000


class SinkConfig(BaseModel):
    """raw 落地出口(§12.3):local=写本地库(同机);http=推给平台(Pattern A 中间服务器)。"""

    model_config = {"extra": "forbid"}
    type: Literal["local", "http"] = "local"
    url: str | None = None                # http:平台接收端点(如 https://平台:8850)
    token_env: str | None = None          # http:Token 所在环境变量(凭据不落配置)


class TableExtractConfig(BaseModel):
    model_config = {"extra": "forbid"}
    mode: Literal["incremental", "full_refresh"]
    schema: str | None = None              # SQL Server schema, 默认 dbo
    key_columns: list[str] | None = None   # 数据库 PK / 唯一索引 / 业务唯一键
    watermark: str | None = None           # incremental 必填, full_refresh 禁止
    schema_fingerprint: str | None = None  # 已确认字段结构摘要(sha256:...)
    validated_at: str | None = None        # 最近一次现场校验时间

    @model_validator(mode="after")
    def _validate_mode_constraints(self):
        if self.mode == "incremental":
            if not self.watermark:
                raise ValueError("incremental 模式必须配置 watermark")
        if self.mode == "full_refresh" and self.watermark is not None:
            raise ValueError("full_refresh 模式不允许配置 watermark")
        if self.watermark is not None:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", self.watermark):
                raise ValueError(f"非法水位列名 '{self.watermark}'(须为 SQL 标识符)")
        if self.key_columns is not None:
            ident = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
    reconcile_at: str | None = None       # "HH:MM",每日 L1 对账;None 不排
    apply_after_sync: bool = True         # sink=http 时忽略(映射在平台侧)
    sink: SinkConfig = SinkConfig()

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
            parse_window(w)
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

    def lookback_days(self) -> float:
        return parse_duration_seconds(self.lookback) / 86400

    def sync_every_seconds(self) -> float:
        return parse_duration_seconds(self.sync_every)

    def sync_start_datetime_after(self, now: datetime) -> datetime:
        """返回自动调度首轮时间。未配置时保持旧行为:服务启动即跑。"""
        if self.sync_start_at is None:
            return now
        target_time = dtime.fromisoformat(self.sync_start_at)
        target = datetime.combine(now.date(), target_time)
        if target < now:
            target += timedelta(days=1)
        return target


class ConnectConfig(BaseModel):
    model_config = {"extra": "forbid"}
    templates: str = "templates"
    landing: str = "landing/factory.sqlite"
    sources: dict[str, SourceConfig]


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
        if s.sink.type == "http" and s.reconcile_at is not None:
            raise ValueError(
                f"源 {name}: 推送模式(sink.type=http)下不能配 reconcile_at —— "
                "跨机对账(E6b)尚未实现,中间机的 landing 只有水位、无 raw,"
                "本地对账会误判整库不一致。请移除 reconcile_at;对账待 E6b 落地后由中间驱动。")
    return cfg


class PlatformConfig(BaseModel):
    """平台配置模型:只包含平台职责字段,不含 ERP 连接或抽取计划。"""

    model_config = {"extra": "forbid"}
    templates: str = "templates"
    landing: str = "landing/factory.sqlite"


def load_platform_config(path: str | Path) -> PlatformConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    # Reject sources block — platform must not carry ERP config
    if "sources" in data:
        raise ValueError("平台配置不得包含 sources 字段;抽取计划仅属于中间机 connect.yaml")
    return PlatformConfig(**data)
