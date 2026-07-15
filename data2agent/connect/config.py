"""connect.yaml 配置:解析与校验(pydantic),窗口与时长工具。

凭据纪律:配置文件里只允许放环境变量名(dsn_env),绝不放连接串本体;
sqlite 源(开发 / 展厅)例外地允许直接写路径(无凭据可泄露)。
"""

from __future__ import annotations

import re
from datetime import time as dtime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

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
    batch_size: int = 5000
    rows_per_second: int = 2000


class SinkConfig(BaseModel):
    """raw 落地出口(§12.3):local=写本地库(同机);http=推给平台(Pattern A 中间服务器)。"""

    type: Literal["local", "http"] = "local"
    url: str | None = None                # http:平台接收端点(如 https://平台:8850)
    token_env: str | None = None          # http:Token 所在环境变量(凭据不落配置)


class SourceConfig(BaseModel):
    adapter: str                          # sqlite_readonly / mssql_readonly
    dsn_env: str | None = None            # mssql:连接串所在环境变量
    path: str | None = None               # sqlite:源库路径
    whitelist_from_bindings: bool = True
    extra_whitelist: list[str] = []
    windows: list[str] = []               # 错峰窗口,空 = 不限
    rate: RateConfig = RateConfig()
    lookback: str = "3d"
    sync_every: str = "30m"
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

    def lookback_days(self) -> float:
        return parse_duration_seconds(self.lookback) / 86400

    def sync_every_seconds(self) -> float:
        return parse_duration_seconds(self.sync_every)


class ConnectConfig(BaseModel):
    templates: str = "templates"
    landing: str = "landing/factory.sqlite"
    sources: dict[str, SourceConfig]


def load_config(path: str | Path) -> ConnectConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = ConnectConfig(**data)
    for name, s in cfg.sources.items():
        if s.adapter == "mssql_readonly" and not s.dsn_env:
            raise ValueError(f"源 {name}: mssql_readonly 必须配 dsn_env(凭据不落配置文件)")
        if s.adapter == "sqlite_readonly" and not (s.path or s.dsn_env):
            raise ValueError(f"源 {name}: sqlite_readonly 须配 path 或 dsn_env")
        if s.sink.type == "http" and not s.sink.url:
            raise ValueError(f"源 {name}: sink.type=http 必须配 sink.url(平台接收端点)")
    return cfg
