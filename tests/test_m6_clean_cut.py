"""M6 干净切换审计:旧配置/CLI/协议与中间机页面不得回退。"""

from __future__ import annotations

import re
from pathlib import Path

from data2agent.ingest.protocol import INGEST_PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[1]
PROD = ROOT / "data2agent"
TEMPLATES = PROD / "middle_admin" / "templates"

# 允许出现在测试断言文案中的旧字段名;生产包禁止。
_FORBIDDEN_PROD = (
    "migrate-config",
    "migrate_config_to_tables",
    "def full_sync",
    "whitelist_from_bindings",
    "extra_whitelist",
)


def _iter_prod_py() -> list[Path]:
    return sorted(p for p in PROD.rglob("*.py") if p.is_file())


def test_ingest_protocol_version_is_v2():
    assert INGEST_PROTOCOL_VERSION == "2"
    from data2agent.ingest.protocol import SUPPORTED_INGEST_PROTOCOL_VERSIONS

    assert SUPPORTED_INGEST_PROTOCOL_VERSIONS == ("2",)
    sink = (PROD / "connect" / "sink.py").read_text(encoding="utf-8")
    ingest_app = (PROD / "ingest" / "app.py").read_text(encoding="utf-8")
    assert "ensure_protocol" in sink
    assert "supported_ingest_protocol_versions" in sink
    assert "health_protocol_fields" in ingest_app
    proto = (PROD / "ingest" / "protocol.py").read_text(encoding="utf-8")
    assert 'INGEST_PROTOCOL_VERSION = "2"' in proto
    assert "SUPPORTED_INGEST_PROTOCOL_VERSIONS" in proto


def test_production_code_has_no_legacy_cutover_symbols():
    hits: list[str] = []
    for path in _iter_prod_py():
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN_PROD:
            if needle in text:
                hits.append(f"{path.relative_to(ROOT)}: {needle}")
    assert hits == [], "生产包仍含旧切换符号:\n" + "\n".join(hits)


def test_cli_has_no_full_or_migrate_flags():
    main = (PROD / "connect" / "__main__.py").read_text(encoding="utf-8")
    assert "--full" not in main
    assert "migrate-config" not in main
    assert "full_sync" not in main


def test_deleted_explicit_table_config_plan_absent():
    stale = ROOT / "docs" / "superpowers" / "plans" / "2026-07-23-explicit-table-config.md"
    assert not stale.exists()


def test_middle_admin_pages_and_nav_present():
    for name in ("metadata.html", "tables.html", "config.html", "status.html", "layout.html"):
        assert (TEMPLATES / name).is_file(), name
    layout = (TEMPLATES / "layout.html").read_text(encoding="utf-8")
    assert 'href="/metadata"' in layout
    assert 'href="/tables"' in layout
    meta = (TEMPLATES / "metadata.html").read_text(encoding="utf-8")
    tables = (TEMPLATES / "tables.html").read_text(encoding="utf-8")
    assert "d2a_extraction_draft:" in meta
    assert "d2a_extraction_draft:" in tables
    assert "/api/extraction-tables" in meta
    assert "/api/extraction-tables" in tables


def test_connect_example_defaults_to_empty_tables():
    text = (ROOT / "connect.example.yaml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*tables:\s*\{\}\s*$", text), (
        "connect.example.yaml 必须默认 tables: {}，新安装不访问 ERP 业务表"
    )
