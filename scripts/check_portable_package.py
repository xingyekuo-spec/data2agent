#!/usr/bin/env python3
"""Fail a portable build when shipped UI/templates or middle-admin pages are stale."""

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


def _check_templates(portable: Path, expected: Path) -> None:
    shipped = portable / "app" / "templates"
    for source in expected.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(expected)
        target = shipped / relative
        if not target.is_file():
            _fail(f"template missing: {relative}")
        if _digest(source) != _digest(target):
            _fail(f"template differs from build source: {relative}")
    for required in (
        Path("metrics/dead_stock.yaml"),
        Path("objects/dead_stock_item.yaml"),
        Path("objects/dead_stock_attribution.yaml"),
    ):
        if not (shipped / required).is_file():
            _fail(f"dead-stock template missing: {required}")


def _site_packages(portable: Path) -> Path:
    # Windows embed: runtime/Lib/site-packages; allow Lib or lib
    for candidate in (
        portable / "runtime" / "Lib" / "site-packages",
        portable / "runtime" / "lib" / "site-packages",
    ):
        if candidate.is_dir():
            return candidate
    _fail("runtime site-packages missing")


def _check_platform_entry(portable: Path) -> None:
    if not (portable / "app" / "console-ui" / "dist" / "index.html").is_file():
        _fail("Vue Console dist/index.html missing")
    bat = portable / "升级.bat"
    start_bat = portable / "启动平台.bat"
    if not bat.is_file():
        _fail("platform 升级入口 升级.bat missing")
    if not start_bat.is_file():
        _fail("platform 稳定启动入口 启动平台.bat missing")
    # bat 必须与 updater 模块内模板一致(单一事实来源,防止两份脚本漂移)。
    # 本脚本由便携包 runtime 的 python 执行,import 到的即被检 wheel 本身。
    from data2agent.updater.apply_script import START_PLATFORM_BAT, UPDATE_BAT
    if bat.read_text(encoding="utf-8") != UPDATE_BAT:
        _fail("升级.bat 与 updater.apply_script.UPDATE_BAT 不一致")
    if start_bat.read_text(encoding="utf-8") != START_PLATFORM_BAT:
        _fail("启动平台.bat 与 updater.apply_script.START_PLATFORM_BAT 不一致")
    app_py = _site_packages(portable) / "data2agent" / "console" / "app.py"
    if not app_py.is_file():
        _fail("installed platform console module missing")
    app_code = app_py.read_text(encoding="utf-8")
    if 'app.mount("/assets", StaticFiles(directory=assets_dir), name="vue-assets")' not in app_code:
        _fail("installed platform wheel does not mount root Vue assets")
    if 'def legacy_v1_index()' not in app_code:
        _fail("installed platform wheel does not retain /v1 compatibility redirect")


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
    for py in d2a.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        for needle in _FORBIDDEN_CODE_SYMBOLS:
            if needle in text:
                rel = py.relative_to(portable)
                _fail(f"legacy symbol {needle!r} in {rel}")


def _check_middle_admin(portable: Path) -> None:
    pkg = _site_packages(portable) / "data2agent" / "middle_admin"
    tpl = pkg / "templates"
    required = ("metadata.html", "tables.html", "config.html", "status.html", "layout.html")
    for name in required:
        path = tpl / name
        if not path.is_file():
            _fail(f"middle_admin template missing: {name}")
    meta = (tpl / "metadata.html").read_text(encoding="utf-8")
    tables = (tpl / "tables.html").read_text(encoding="utf-8")
    layout = (tpl / "layout.html").read_text(encoding="utf-8")
    if "d2a_extraction_draft:" not in meta:
        _fail("middle_admin metadata draft-key cleanup missing")
    if "saveTablesPlan" not in tables or "btn-batch-edit" not in tables:
        _fail("middle_admin tables page missing direct-save / batch edit")
    if "btn-draft-only" in tables or "preferDraft" in tables:
        _fail("middle_admin tables page still exposes draft save flow")
    if "/api/extraction-tables" not in meta or "/api/extraction-tables" not in tables:
        _fail("middle_admin pages missing /api/extraction-tables")
    if 'href="/metadata"' not in layout or 'href="/tables"' not in layout:
        _fail("middle_admin nav missing /metadata or /tables")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portable", type=Path, required=True)
    parser.add_argument("--role", choices=("middle", "platform"), required=True)
    parser.add_argument("--expected-templates", type=Path, required=True)
    args = parser.parse_args()

    portable = args.portable.resolve()
    _check_templates(portable, args.expected_templates.resolve())
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
    if args.role == "platform":
        if "send_ingest_protocol_version" in build_info:
            _fail("platform BUILD-INFO must not declare send_ingest_protocol_version")
        active = build_info.get("active_ingest_protocol_version")
        legacy_health = build_info.get("legacy_health_ingest_protocol_version")
        supported = build_info.get("supported_ingest_protocol_versions")
        if not isinstance(active, str) or not active:
            _fail("platform BUILD-INFO missing active_ingest_protocol_version")
        if not isinstance(legacy_health, str) or not legacy_health:
            _fail("platform BUILD-INFO missing legacy_health_ingest_protocol_version")
        if (
            not isinstance(supported, list)
            or not supported
            or not all(isinstance(p, str) and p for p in supported)
        ):
            _fail(
                "platform BUILD-INFO supported_ingest_protocol_versions "
                "must be a non-empty string list"
            )
        if active not in supported:
            _fail("platform active_ingest_protocol_version must be in supported list")
        if legacy_health not in supported:
            _fail("platform legacy_health_ingest_protocol_version must be in supported list")
    else:
        if "supported_ingest_protocol_versions" in build_info:
            _fail("middle BUILD-INFO must not declare supported_ingest_protocol_versions")
        send = build_info.get("send_ingest_protocol_version")
        if not isinstance(send, str) or not send:
            _fail("middle BUILD-INFO missing send_ingest_protocol_version")
    _check_no_legacy_erp_artifacts(portable)
    if args.role == "platform":
        _check_platform_entry(portable)
    else:
        _check_middle_admin(portable)
    print("portable package check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
