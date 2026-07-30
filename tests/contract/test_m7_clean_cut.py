"""M7 干净切换审计:产品包无 showroom / Mock 运行模式符号。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD = ROOT / "data2agent"

# 允许出现在实施计划历史与本清单中的字样
_ALLOW_DOC_PREFIXES = (
    ROOT / "docs" / "superpowers" / "plans" / "2026-07-23-erp-metadata-extraction-management.md",
    ROOT / "docs" / "superpowers" / "plans" / "2026-07-24-m7-showroom-migration-checklist.md",
)

_FORBIDDEN = (
    "data2agent.showroom",
    "showroom-connect.yaml",
    "VITE_CONSOLE_MODE",
)


def _iter_scan_files() -> list[Path]:
    skip_dirs = {
        ".git", ".venv", "node_modules", "dist", "__pycache__",
        ".pytest_cache", ".worktrees", ".claude", ".cursor",
    }
    out: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        # 本审计文件与清单会字面列出禁用符号
        if path.name in {"test_m7_clean_cut.py", "2026-07-24-m7-showroom-migration-checklist.md"}:
            continue
        if path.suffix.lower() not in {
            ".py", ".md", ".yml", ".yaml", ".ts", ".tsx", ".mjs", ".js",
            ".json", ".toml", ".html", ".vue", ".ps1", ".tape",
        }:
            continue
        out.append(path)
    return out


def test_product_package_has_no_showroom_module():
    assert not (PROD / "showroom").exists()
    for path in PROD.rglob("*"):
        assert "showroom" not in path.parts, path


def test_repo_has_no_forbidden_product_symbols():
    allow = {p.resolve() for p in _ALLOW_DOC_PREFIXES}
    hits: list[str] = []
    for path in _iter_scan_files():
        if path.resolve() in allow:
            continue
        # fixtures 包名 seed_mssql 模块允许(tests.fixtures.e10.seed_mssql)
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in _FORBIDDEN:
            if needle in text:
                hits.append(f"{path.relative_to(ROOT)}: {needle}")
        # 旧产品入口 python -m data2agent.showroom
        if "python -m data2agent.showroom" in text:
            hits.append(f"{path.relative_to(ROOT)}: python -m data2agent.showroom")
        if "deploy/showroom-connect.yaml" in text and "plans/" not in str(path):
            hits.append(f"{path.relative_to(ROOT)}: deploy/showroom-connect.yaml")
    assert hits == [], "仍存在已删除的产品展厅/Mock 符号:\n" + "\n".join(hits)


def test_demo_assets_deleted():
    for rel in (
        "docker-compose.yml",
        "deploy/showroom-connect.yaml",
        "deploy/demo.tape",
        "deploy/render_hero_svg.py",
        "console-ui/public/mockServiceWorker.js",
        "console-ui/src/components/shared/ScenarioSwitcher.vue",
    ):
        assert not (ROOT / rel).exists(), rel


def test_console_mode_is_always_real():
    mode = (ROOT / "console-ui" / "src" / "config" / "mode.ts").read_text(encoding="utf-8")
    assert "ConsoleMode = 'real'" in mode or 'ConsoleMode = "real"' in mode
    assert "export const IS_MOCK = false" in mode
    assert "VITE_CONSOLE_MODE" not in mode


def test_e10_fixtures_importable():
    from tests.fixtures.e10.schema import TABLES
    from tests.fixtures.e10.seed import build, write_db

    assert "CUSTOMER" in TABLES
    assert callable(build) and callable(write_db)
