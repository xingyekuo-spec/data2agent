"""TableExtractConfig 配置模型测试."""
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

    def test_rejects_incremental_without_watermark(self):
        with pytest.raises(ValueError, match="watermark"):
            TableExtractConfig(mode="incremental")

    def test_rejects_full_refresh_with_watermark(self):
        with pytest.raises(ValueError, match="watermark"):
            TableExtractConfig(mode="full_refresh", watermark="COL")

    def test_rejects_unknown_field(self):
        with pytest.raises(ValueError):
            TableExtractConfig(mode="incremental", watermark="X", enabled=True)


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

    def test_rejects_empty_tables(self):
        with pytest.raises(ValueError, match="不能为空"):
            SourceConfig(adapter="sqlite_readonly", path="x", tables={})

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
        with pytest.raises(ValueError, match="whitelist_from_bindings"):
            load_config(cfg_file)

    def test_rejects_old_extra_whitelist(self, tmp_path):
        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
            "    extra_whitelist: [X]\n",
            encoding="utf-8")
        with pytest.raises(ValueError, match="extra_whitelist"):
            load_config(cfg_file)


class TestMigration:
    def test_migrate_whitelist_from_bindings_true(self, tmp_path, monkeypatch):
        from data2agent.connect.sync import migrate_config_to_tables
        from data2agent.metamodel.loader import load_pack
        import yaml

        ROOT = Path(__file__).resolve().parents[1]
        pack = load_pack(ROOT / "templates")

        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "templates: t\nlanding: l.sqlite\n"
            "sources:\n"
            "  digiwin_e10:\n"
            "    adapter: sqlite_readonly\n"
            "    path: s.sqlite\n"
            "    whitelist_from_bindings: true\n"
            "    extra_whitelist: []\n",
            encoding="utf-8")

        bak, result = migrate_config_to_tables(str(cfg_file), pack)
        assert "digiwin_e10" in result
        tables = result["digiwin_e10"]
        assert "CUSTOMER" in tables
        assert Path(bak).exists()

        # Reload and verify tables are present, old fields gone
        new_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        sdata = new_data["sources"]["digiwin_e10"]
        assert "tables" in sdata
        assert "whitelist_from_bindings" not in sdata
        assert "extra_whitelist" not in sdata
        assert all("mode" in v for v in sdata["tables"].values())

    def test_migrate_idempotent(self, tmp_path):
        from data2agent.connect.sync import migrate_config_to_tables
        from data2agent.metamodel.loader import load_pack
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[1]
        pack = load_pack(ROOT / "templates")

        cfg_file = tmp_path / "connect.yaml"
        cfg_file.write_text(
            "templates: t\nlanding: l.sqlite\n"
            "sources:\n"
            "  digiwin_e10:\n"
            "    adapter: sqlite_readonly\n"
            "    path: s.sqlite\n"
            "    tables:\n"
            "      CUSTOMER:\n"
            "        mode: incremental\n"
            "        watermark: UPD\n",
            encoding="utf-8")

        with pytest.raises(RuntimeError, match="无需迁移"):
            migrate_config_to_tables(str(cfg_file), pack)
