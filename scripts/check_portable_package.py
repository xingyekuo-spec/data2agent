#!/usr/bin/env python3
"""Fail a portable middle build when shipped middle-admin pages are stale."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# 文件名/目录名不得出现在便携包任意位置(含 app、runtime、其他子包)。
_FORBIDDEN_PATH_PARTS = (
    "erp_profile",
    "erp_profiles",
    "erp-configs",
    "erp_configs",
    "table_candidates",
    "table-candidates",
    "showroom",
)

# 已安装 data2agent 包内不得再出现的旧切换符号。
_FORBIDDEN_CODE_SYMBOLS = (
    "whitelist_from_bindings",
    "extra_whitelist",
    "migrate_config_to_tables",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fail(message: str) -> None:
    raise SystemExit(f"portable package check failed: {message}")


def _site_packages(portable: Path) -> Path:
    # Windows embed: runtime/Lib/site-packages; allow Lib or lib
    for candidate in (
        portable / "runtime" / "Lib" / "site-packages",
        portable / "runtime" / "lib" / "site-packages",
    ):
        if candidate.is_dir():
            return candidate
    _fail("runtime site-packages missing")


def _check_no_legacy_erp_artifacts(portable: Path) -> None:
    """扫描整个便携包根，禁止旧 ERP profile / 候选清单路径名。"""
    for hit in portable.rglob("*"):
        folded = hit.name.casefold()
        for bad in _FORBIDDEN_PATH_PARTS:
            if bad in folded:
                rel = hit.relative_to(portable)
                _fail(f"forbidden ERP profile artifact in package: {rel}")

    d2a = _site_packages(portable) / "data2agent"
    if not d2a.is_dir():
        return
    if (d2a / "platform").exists():
        _fail("platform package must not ship: data2agent/platform present")
    for py in d2a.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for needle in _FORBIDDEN_CODE_SYMBOLS:
            if needle in text:
                rel = py.relative_to(portable)
                _fail(f"legacy symbol {needle!r} in {rel}")


def _check_middle_admin(portable: Path) -> None:
    data2agent_pkg = _site_packages(portable) / "data2agent"
    pkg = data2agent_pkg / "middle" / "admin"
    tpl = pkg / "templates"
    shared = data2agent_pkg / "shared" / "admin_templates"
    static = shared / "static"
    required = (
        "metadata.html", "tables.html", "config.html", "status.html",
        "recovery.html", "runs.html", "layout.html",
    )
    for name in required:
        path = tpl / name
        if not path.is_file():
            _fail(f"middle_admin template missing: {name}")
    meta = (tpl / "metadata.html").read_text(encoding="utf-8")
    tables = (tpl / "tables.html").read_text(encoding="utf-8")
    layout = (tpl / "layout.html").read_text(encoding="utf-8")
    shared_layout = shared / "layout.html"
    if not shared_layout.is_file():
        _fail("middle_admin shared layout missing")
    required_scripts = (
        "admin.js", "admin.css", "htmx.min.js", "middle-config.js", "middle-errors.js",
        "middle-logs.js", "middle-metadata.js", "middle-push-logs.js",
        "middle-actions.js", "middle-runs.js", "middle-shell.js", "middle-status.js",
        "middle-tables.js",
    )
    for name in required_scripts:
        if not (static / name).is_file():
            _fail(f"middle_admin static script missing: {name}")
    metadata_js = (static / "middle-metadata.js").read_text(encoding="utf-8")
    tables_js = (static / "middle-tables.js").read_text(encoding="utf-8")
    if "d2a_extraction_draft:" not in metadata_js:
        _fail("middle_admin metadata draft-key cleanup missing")
    if "saveTablesPlan" not in tables_js or "btn-batch-edit" not in tables:
        _fail("middle_admin tables page missing direct-save / batch edit")
    if "btn-draft-only" in tables or "preferDraft" in tables_js:
        _fail("middle_admin tables page still exposes draft save flow")
    if ("/api/extraction-tables" not in metadata_js
            or "/api/extraction-tables" not in tables_js):
        _fail("middle_admin pages missing /api/extraction-tables")
    if 'href="/metadata"' not in layout or 'href="/tables"' not in layout:
        _fail("middle_admin nav missing /metadata or /tables")
    for page in tpl.glob("*.html"):
        page_text = page.read_text(encoding="utf-8")
        if "<script>" in page_text or "onclick=" in page_text:
            _fail(f"middle_admin inline script/handler violates CSP: {page.name}")
    dependency_manifest = shared / "FRONTEND-DEPENDENCIES.md"
    if not dependency_manifest.is_file():
        _fail("middle_admin frontend dependency manifest missing")
    manifest = dependency_manifest.read_text(encoding="utf-8")
    if "HTMX 2.0.4" not in manifest or "MIT" not in manifest:
        _fail("middle_admin HTMX version/license manifest incomplete")
    if "Edge 120" not in manifest or "Chrome 120" not in manifest:
        _fail("middle_admin browser compatibility baseline missing")
    for script in ("安装开机自启.ps1", "卸载开机自启.ps1"):
        if not (portable / script).is_file():
            _fail(f"middle autostart script missing: {script}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portable", type=Path, required=True)
    parser.add_argument("--role", choices=("middle",), default="middle")
    args = parser.parse_args()

    portable = args.portable.resolve()
    build_info_path = portable / "BUILD-INFO.json"
    if not build_info_path.is_file():
        _fail("portable build label missing")
    try:
        build_info = json.loads(build_info_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _fail(f"BUILD-INFO.json invalid JSON: {e}")
    for key in ("application_version", "release_version", "role", "commit"):
        if key not in build_info:
            _fail(f"BUILD-INFO.json missing {key}")
    if build_info.get("role") != args.role:
        _fail(f"BUILD-INFO.json role={build_info.get('role')!r} != --role {args.role}")
    if "supported_ingest_protocol_versions" in build_info:
        _fail("middle BUILD-INFO must not declare supported_ingest_protocol_versions")
    send = build_info.get("send_ingest_protocol_version")
    if not isinstance(send, str) or not send:
        _fail("middle BUILD-INFO missing send_ingest_protocol_version")
    _check_no_legacy_erp_artifacts(portable)
    _check_middle_admin(portable)
    print("portable package check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
