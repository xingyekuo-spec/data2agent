"""MSSQL 配置业务键覆盖 DB PK 集成测试(环境变量门控)。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.base import RuntimeKeyError, resolve_runtime_keys
from data2agent.middle.extract.adapters.mssql import MssqlReadOnlyAdapter
from data2agent.shared.config import SourceConfig, TableExtractConfig
from data2agent.middle.extract.metadata import build_discoverer
from data2agent.shared.metamodel.loader import load_pack
from tests.helpers import whitelist_from_pack

DSN = os.environ.get("D2A_IT_MSSQL_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="需要 MSSQL 集成环境(tests/integration/mssql/docker-compose.yml)")

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "digiwin_e10"
SOURCE_ENV = "D2A_IT_MSSQL_DSN"


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


def test_configured_key_overrides_database_pk(pack):
    adapter = MssqlReadOnlyAdapter(DSN, whitelist_from_pack(pack, SOURCE), batch_size=64)
    try:
        info = adapter.table_info("CUSTOMER")
        assert info.pk, "seed 库 CUSTOMER 应有数据库主键"
        configured = resolve_runtime_keys(info, ["CUSTOMER_CODE"])
        assert configured.pk == ["CUSTOMER_CODE"]
        assert configured.key_source == "configured"
    finally:
        adapter.con.close()


def test_configured_key_missing_column_fails(pack):
    adapter = MssqlReadOnlyAdapter(DSN, whitelist_from_pack(pack, SOURCE), batch_size=64)
    try:
        info = adapter.table_info("CUSTOMER")
        with pytest.raises(RuntimeKeyError, match="不存在"):
            resolve_runtime_keys(info, ["NO_SUCH_COLUMN"])
    finally:
        adapter.con.close()


def test_discoverer_check_key_accepts_business_key(monkeypatch):
    monkeypatch.setenv(SOURCE_ENV, DSN)
    scfg = SourceConfig(
        adapter="mssql_readonly",
        dsn_env=SOURCE_ENV,
        tables={
            "CUSTOMER": TableExtractConfig(
                mode="incremental",
                watermark="LAST_MODIFIED_DATE",
                schema="dbo",
                key_columns=["CUSTOMER_CODE"],
            ),
        },
    )
    disc = build_discoverer(scfg)
    try:
        result = disc.check_key("dbo", "CUSTOMER", ["CUSTOMER_CODE"])
        assert result.ok is True
    finally:
        disc.close()
