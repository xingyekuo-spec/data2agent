"""MSSQL 元数据发现集成测试(环境变量门控)。

触发方式见同目录 docker-compose.yml。
"""

from __future__ import annotations

import os

import pytest

from data2agent.shared.config import SourceConfig, TableExtractConfig
from data2agent.middle.extract.metadata import build_discoverer

DSN = os.environ.get("D2A_IT_MSSQL_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="需要 MSSQL 集成环境(tests/integration/mssql/docker-compose.yml)")

SOURCE_ENV = "D2A_IT_MSSQL_DSN"


@pytest.fixture()
def scfg(monkeypatch):
    monkeypatch.setenv(SOURCE_ENV, DSN)
    return SourceConfig(
        adapter="mssql_readonly",
        dsn_env=SOURCE_ENV,
        tables={
            "CUSTOMER": TableExtractConfig(
                mode="incremental", watermark="LAST_MODIFIED_DATE", schema="dbo"),
            "CURRENCY": TableExtractConfig(mode="full_refresh", schema="dbo"),
        },
    )


def test_list_tables_includes_seed_baselines(scfg):
    disc = build_discoverer(scfg)
    try:
        rows, total = disc.list_tables(schema="dbo", limit=500)
        names = {t.name for t in rows}
        assert total >= 3
        assert {"CUSTOMER", "CURRENCY", "ITEM"}.issubset(names)
    finally:
        disc.close()


def test_get_table_returns_columns_and_pk(scfg):
    disc = build_discoverer(scfg)
    try:
        detail = disc.get_table("dbo", "CUSTOMER")
        cols = {c.name for c in detail.columns}
        assert "CUSTOMER_CODE" in cols
        assert detail.primary_key or detail.unique_keys
        assert detail.schema_fingerprint
    finally:
        disc.close()
