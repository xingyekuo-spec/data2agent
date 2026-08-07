"""Build initial connect.yaml from browser setup form."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .home_layout import HomeLayout, resolve_templates


def build_middle_connect_yaml(
    home: HomeLayout,
    *,
    platform_url: str,
    sync_every: str = "30m",
    sync_start_at: str | None = None,
    start_date: str | None = None,
    lookback: str = "3d",
    batch_size: int = 5000,
    rows_per_second: int = 2000,
) -> dict:
    templates = str(resolve_templates(home))
    landing = str(home.data_dir / "middle.sqlite")
    source = {
        "adapter": "mssql_readonly",
        "dsn_env": "D2A_E10_DSN",
        "tables": {},
        "windows": [],
        "rate": {"batch_size": batch_size, "rows_per_second": rows_per_second},
        "lookback": lookback,
        "sync_every": sync_every,
        # 新部署默认每日做廉价 L1 对账；L2 deep 仍需按现场窗口显式开启。
        "reconcile_at": "05:30",
        "reconcile_deep_at": "03:30",
        "reconcile_deep_day_of_week": "sun",
        "sink": {
            "type": "http",
            "url": platform_url.rstrip("/"),
            "token_env": "D2A_INGEST_TOKEN",
        },
    }
    parsed = urlparse(platform_url)
    if parsed.scheme == "http" and parsed.hostname not in (
        "127.0.0.1", "::1", "localhost",
    ):
        # 浏览器表单中明确填写了非本机 HTTP 地址；把安全例外显式落盘，
        # 后续审核可见，避免隐式放宽全局默认。
        source["sink"]["allow_insecure_http"] = True
    if sync_start_at:
        source["sync_start_at"] = sync_start_at
    if start_date:
        source["start_date"] = start_date
    return {
        "templates": templates,
        "landing": landing,
        "sources": {"digiwin_e10": source},
    }


def build_platform_yaml(home: HomeLayout) -> dict:
    return {
        "templates": str(resolve_templates(home)),
        "landing": str(home.data_dir / "factory.sqlite"),
    }


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def build_odbc_dsn(
    *,
    server: str,
    database: str,
    user: str,
    password: str,
    port: int | None = 1433,
    driver: str = "ODBC Driver 18 for SQL Server",
) -> str:
    def value(raw: str) -> str:
        # ODBC 连接串值用花括号封装；内部 } 按 ODBC 规则双写。
        # 这不仅支持分号密码，也阻止表单值注入额外 DSN 属性。
        return "{" + str(raw).replace("}", "}}") + "}"

    if "\\" in server or (port is not None and port <= 0):
        server_part = server
    else:
        server_part = f"{server},{port or 1433}"
    return (
        f"DRIVER={value(driver)};SERVER={value(server_part)};"
        f"UID={value(user)};PWD={value(password)};"
        f"DATABASE={value(database)};Encrypt=yes;TrustServerCertificate=no"
    )
