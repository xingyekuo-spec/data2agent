"""Build initial connect.yaml / platform.yaml from browser setup form."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .home_layout import HomeLayout, resolve_templates


_ERP_TABLE_PROFILE = "digiwin_e10.yaml"


def resolve_erp_table_profile(home: HomeLayout) -> Path:
    """Return the independent ERP extraction profile used for first setup.

    Portable packages carry it in ``app/erp-configs``.  The repository fallback
    keeps source-tree development and tests independent from object templates.
    """
    portable = home.app / "erp-configs" / _ERP_TABLE_PROFILE
    if portable.is_file():
        return portable
    return Path(__file__).resolve().parents[2] / "erp-configs" / _ERP_TABLE_PROFILE


def load_default_erp_tables(home: HomeLayout) -> dict[str, dict[str, str]]:
    """Load the default E10 table policy without consulting business templates."""
    profile = resolve_erp_table_profile(home)
    try:
        raw: Any = yaml.safe_load(profile.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"ERP 配置模板不可读取: {profile}") from exc
    tables = raw.get("tables") if isinstance(raw, dict) else None
    if not isinstance(tables, dict) or not tables:
        raise RuntimeError(f"ERP 配置模板格式无效: {profile}")
    if not all(isinstance(name, str) and isinstance(spec, dict)
               for name, spec in tables.items()):
        raise RuntimeError(f"ERP 配置模板 tables 格式无效: {profile}")
    return deepcopy(tables)


def build_middle_connect_yaml(
    home: HomeLayout,
    *,
    platform_url: str,
    sync_every: str = "30m",
    lookback: str = "3d",
    batch_size: int = 5000,
    rows_per_second: int = 2000,
) -> dict:
    templates = str(resolve_templates(home))
    landing = str(home.data_dir / "middle.sqlite")
    return {
        "templates": templates,
        "landing": landing,
        "sources": {
            "digiwin_e10": {
                "adapter": "mssql_readonly",
                "dsn_env": "D2A_E10_DSN",
                "tables": load_default_erp_tables(home),
                "windows": [],
                "rate": {"batch_size": batch_size, "rows_per_second": rows_per_second},
                "lookback": lookback,
                "sync_every": sync_every,
                "sink": {
                    "type": "http",
                    "url": platform_url.rstrip("/"),
                    "token_env": "D2A_INGEST_TOKEN",
                },
            }
        },
    }


def build_platform_yaml(home: HomeLayout) -> dict:
    return {
        "templates": str(resolve_templates(home)),
        "landing": str(home.data_dir / "factory.sqlite"),
        "sources": {
            "digiwin_e10": {
                "adapter": "mssql_readonly",
                "dsn_env": "D2A_E10_DSN_PLACEHOLDER",
                "tables": {
                    "CUSTOMER": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                    "CURRENCY": {"mode": "full_refresh"},
                    "ITEM": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                    "QUOTATION": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                    "SALES_ORDER": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                    "SALES_ORDER_D": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
                },
            }
        },
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
    if "\\" in server or (port is not None and port <= 0):
        server_part = server
    else:
        server_part = f"{server},{port or 1433}"
    return (
        f"DRIVER={{{driver}}};SERVER={server_part};UID={user};PWD={password};"
        f"DATABASE={database};TrustServerCertificate=yes"
    )
