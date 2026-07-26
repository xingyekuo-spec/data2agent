"""TableExtractConfig 配置模型测试."""
import subprocess
import sys

import pytest
from pathlib import Path
from data2agent.connect.config import (
    TableExtractConfig, SourceConfig, ConnectConfig, load_config
)


class TestTableExtractConfig:
    def test_accepts_incremental_with_watermark(self):
        cfg = TableExtractConfig(mode="incremental", watermark="LAST_MODIFIED_DATE")
        assert cfg.mode == "incremental"
        assert cfg.watermark == "LAST_MODIFIED_DATE"

    def test_accepts_full_refresh_without_watermark(self):
        cfg = TableExtractConfig(mode="full_refresh")
        assert cfg.mode == "full_refresh"
        assert cfg.watermark is None

    def test_schema_alias_keeps_yaml_name(self):
        cfg = TableExtractConfig(mode="full_refresh", schema="dbo")
        assert cfg.schema == "dbo"
        assert cfg.schema_name == "dbo"
        dumped = cfg.model_dump(by_alias=True)
        assert dumped["schema"] == "dbo"
        assert "schema_name" not in dumped

    def test_import_has_no_schema_shadow_warning(self):
        code = (
            "from data2agent.connect.config import TableExtractConfig; "
            "print(TableExtractConfig(mode='full_refresh', schema='dbo').schema)"
        )
        proc = subprocess.run(
            [sys.executable, "-W", "error::UserWarning", "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        assert proc.stdout.strip() == "dbo"

    def test_rejects_incremental_without_watermark(self):
        with pytest.raises(ValueError, match="watermark"):
            TableExtractConfig(mode="incremental")

    def test_rejects_full_refresh_with_watermark(self):
        with pytest.raises(ValueError, match="watermark"):
            TableExtractConfig(mode="full_refresh", watermark="COL")

    def test_rejects_unknown_field(self):
        with pytest.raises(ValueError):
            TableExtractConfig(mode="incremental", watermark="X", enabled=True)

    def test_rejects_bad_watermark_identifier(self):
        with pytest.raises(ValueError, match="非法水位列名"):
            TableExtractConfig(mode="incremental", watermark="1bad_column")

    def test_rejects_duplicate_key_columns(self):
        with pytest.raises(ValueError, match="重复列"):
            TableExtractConfig(
                mode="incremental", watermark="UPD",
                key_columns=["CODE", "CODE"],
            )

    def test_accepts_distinct_key_columns(self):
        cfg = TableExtractConfig(
            mode="incremental", watermark="UPD",
            key_columns=["ITEM_ID", "WH_ID"],
        )
        assert cfg.key_columns == ["ITEM_ID", "WH_ID"]


class TestSourceConfigTables:
    def test_whitelist_from_tables(self):
        scfg = SourceConfig(
            adapter="sqlite_readonly", path="x",
            tables={
                "CUSTOMER": {"mode": "incremental", "watermark": "UPD"},
                "CURRENCY": {"mode": "full_refresh"},
            }
        )
        assert scfg.table_whitelist() == {"CUSTOMER", "CURRENCY"}

    def test_watermarks_from_tables(self):
        scfg = SourceConfig(
            adapter="sqlite_readonly", path="x",
            tables={
                "CUSTOMER": {"mode": "incremental", "watermark": "UPD"},
                "CURRENCY": {"mode": "full_refresh"},
            }
        )
        assert scfg.table_watermarks() == {"CUSTOMER": "UPD"}

    def test_allows_empty_tables(self):
        scfg = SourceConfig(adapter="sqlite_readonly", path="x", tables={})
        assert scfg.tables == {}
        assert scfg.table_whitelist() == set()
        assert scfg.table_watermarks() == {}

    def test_whitelist_none_tables_returns_empty(self):
        """当 tables 为 None 时(迁移前),whitelist 和 watermarks 返回空集合。"""
        scfg = SourceConfig(adapter="sqlite_readonly", path="x", tables=None)
        assert scfg.table_whitelist() == set()
        assert scfg.table_watermarks() == {}

    def test_rejects_duplicate_casefold_table(self):
        with pytest.raises(ValueError, match="大小写冲突"):
            SourceConfig(
                adapter="sqlite_readonly", path="x",
                tables={
                    "CUSTOMER": {"mode": "full_refresh"},
                    "customer": {"mode": "full_refresh"},
                }
            )

    def test_rejects_bad_identifier(self):
        with pytest.raises(ValueError, match="非法表名"):
            SourceConfig(
                adapter="sqlite_readonly", path="x",
                tables={"DROP TABLE": {"mode": "full_refresh"}}
            )


class TestLoadConfigTables:
    def test_load_minimal_tables_config(self, tmp_path):
        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "templates: t\nlanding: l.sqlite\n"
            "sources:\n"
            "  e10:\n"
            "    adapter: sqlite_readonly\n"
            "    path: s.sqlite\n"
            "    tables:\n"
            "      CUSTOMER:\n"
            "        mode: incremental\n"
            "        watermark: LAST_MODIFIED_DATE\n"
            "      CURRENCY:\n"
            "        mode: full_refresh\n",
            encoding="utf-8")
        cfg = load_config(cfg_file)
        s = cfg.sources["e10"]
        assert s.table_whitelist() == {"CUSTOMER", "CURRENCY"}
        assert s.table_watermarks() == {"CUSTOMER": "LAST_MODIFIED_DATE"}

    def test_rejects_missing_tables(self, tmp_path):
        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n",
            encoding="utf-8")
        with pytest.raises(ValueError, match="缺少 tables"):
            load_config(cfg_file)

    def test_rejects_old_whitelist_from_bindings(self, tmp_path):
        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
            "    whitelist_from_bindings: true\n",
            encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(cfg_file)

    def test_rejects_old_extra_whitelist(self, tmp_path):
        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
            "    extra_whitelist: [X]\n",
            encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(cfg_file)


class TestUnknownFields:
    def test_source_config_rejects_unknown_field(self, tmp_path):
        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
            "    tables:\n"
            "      CUSTOMER:\n"
            "        mode: incremental\n"
            "        watermark: UPD\n"
            "    lookbak: 30d\n",
            encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(cfg_file)

    def test_table_config_rejects_unknown_field(self):
        with pytest.raises(ValueError):
            TableExtractConfig(mode="incremental", watermark="X", enabled=True)
