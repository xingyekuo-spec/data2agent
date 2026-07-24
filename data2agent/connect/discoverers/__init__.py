"""元数据 discoverer 实现注册。"""

from __future__ import annotations

from .mssql import create_mssql_discoverer
from .sqlite import create_sqlite_discoverer
from ..metadata import register_discoverer

register_discoverer(
    "mssql_readonly", create_mssql_discoverer, default_schema="dbo")
register_discoverer(
    "sqlite_readonly", create_sqlite_discoverer, default_schema="main")
