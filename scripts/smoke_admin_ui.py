#!/usr/bin/env python3
"""一键冒烟:中间机 middle_admin + 平台 console 管理界面(无需浏览器 / 真实 ERP)。

用法(仓库根或 worktree):
  pip install -e ".[dev,connect,middle_admin,console]"
  python scripts/smoke_admin_ui.py

退出码 0=全部通过,非 0=失败。CI 也可直接跑本脚本。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"OK  {msg}")


# 冒烟测试固定表配置:与 tests/fixtures/e10 seed 中的表对齐。
# 抽取范围只来自显式 tables 字段，不从模板 binding 推导。
_SMOKE_TABLES = """
    tables:
      CUSTOMER:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      CURRENCY:
        mode: full_refresh
      ITEM:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      ITEM_WAREHOUSE:
        mode: full_refresh
      QUOTATION:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      SALES_ORDER:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
      SALES_ORDER_D:
        mode: incremental
        watermark: LAST_MODIFIED_DATE
"""


def _prepare(tmp: Path) -> Path:
    from data2agent.connect.landing import LandingStore
    from tests.fixtures.e10.seed import build, write_db

    src = tmp / "e10.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    landing = tmp / "middle.sqlite"
    LandingStore(landing)
    (tmp / "logs").mkdir()
    (tmp / "logs" / "d2a-connector.log").write_text("INFO smoke line\n", encoding="utf-8")

    tables_block = _SMOKE_TABLES
    cfg = tmp / "connect.yaml"
    cfg.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        f"{tables_block}\n"
        "    windows: []\n"
        "    rate: { batch_size: 5000, rows_per_second: 2000 }\n"
        "    lookback: 3d\n"
        "    sync_every: 30m\n",
        encoding="utf-8",
    )
    return cfg


def smoke_middle(cfg: Path, log_path: Path) -> None:
    from fastapi.testclient import TestClient

    from data2agent.middle_admin.app import create_app

    token = "smoke-token"
    client = TestClient(create_app(cfg, token=token, log_path=log_path))
    h = {"Authorization": f"Bearer {token}"}

    if client.get("/api/config").status_code != 401:
        _fail("middle: missing token should 401")
    _ok("middle: unauthenticated API → 401")

    for path in ("/status", "/config", "/logs", "/metadata", "/tables"):
        r = client.get(path, headers=h)
        if r.status_code != 200:
            _fail(f"middle: GET {path} → {r.status_code}")
    _ok("middle: HTML pages /status /config /logs /metadata /tables → 200")

    nav = client.get("/status", headers=h).text
    if 'href="/metadata"' not in nav or 'href="/tables"' not in nav:
        _fail("middle: nav missing /metadata or /tables")
    _ok("middle: nav includes metadata + tables")

    r = client.get("/api/extraction-tables", headers=h)
    if r.status_code != 200:
        _fail(f"middle: GET /api/extraction-tables → {r.status_code}")
    et = r.json()
    if "tables" not in et or not isinstance(et["tables"], dict):
        _fail(f"middle: extraction-tables missing tables dict: {et}")
    if "revision" not in et or "source" not in et:
        _fail(f"middle: extraction-tables missing revision/source: {et}")
    _ok("middle: GET /api/extraction-tables (tables may be empty)")

    meta_page = client.get("/metadata", headers=h).text
    tables_page = client.get("/tables", headers=h).text
    if "d2a_extraction_draft:" not in meta_page:
        _fail("middle: metadata page missing draft-key cleanup helper")
    if "saveTablesPlan" not in tables_page or "btn-batch-edit" not in tables_page:
        _fail("middle: tables page missing direct-save / batch edit")
    if "btn-draft-only" in tables_page or "preferDraft" in tables_page:
        _fail("middle: tables page still exposes draft save flow")
    if "前往元数据" not in tables_page and 'href="/metadata"' not in tables_page:
        _fail("middle: tables page missing metadata guidance")
    _ok("middle: metadata/tables pages (direct save + batch edit)")

    r = client.get("/api/status", headers=h)
    if r.status_code != 200 or r.json().get("schedule_source") != "derived_from_yaml":
        _fail(f"middle: /api/status bad: {r.status_code} {r.text[:200]}")
    _ok("middle: /api/status schedule_source=derived_from_yaml")

    revision = client.get("/api/config", headers=h).json()["revision"]
    r = client.post(
        "/api/config",
        headers=h,
        json={"sources": {"digiwin_e10": {"sync_every": "15m", "dsn_env": "HACKED"}},
              "revision": revision},
    )
    body = r.json()
    if r.status_code != 200 or not body.get("ok"):
        _fail(f"middle: config save failed: {body}")
    text = cfg.read_text(encoding="utf-8")
    if "15m" not in text or "HACKED" in text:
        _fail("middle: whitelist merge failed (sync_every / dsn_env)")
    if not list(cfg.parent.glob("connect.yaml.bak*")):
        _fail("middle: missing yaml.bak backup")
    if not body.get("restart_required", True):
        _fail("middle: expected restart_required on save")
    _ok("middle: config whitelist save + backup + restart hint")

    r = client.get("/api/logs?lines=50", headers=h)
    if r.status_code != 200 or r.json().get("ok") is not True:
        _fail(f"middle: /api/logs: {r.text[:200]}")
    _ok("middle: /api/logs")

    r = client.post("/api/connection/test", headers=h)
    if (r.status_code != 200 or r.json().get("status") != "failed"
            or r.json().get("error") != "unsupported"):
        _fail(f"middle: connection/test: {r.text[:300]}")
    _ok("middle: /api/connection/test (sqlite 不支持纯 ODBC 探测)")

    # 改异步后:执行成功返回 executed=True + status="started" + run_id
    r = client.post("/api/actions/trigger", headers=h, json={"action": "sync"})
    if r.status_code != 200 or r.json().get("executed") is not True:
        _fail(f"middle: trigger sync: {r.text[:200]}")
    body = r.json()
    run_id = body.get("run_id")
    _ok(f"middle: trigger sync (async, run_id={run_id})")

    r = client.post("/api/actions/trigger", headers=h, json={"action": "reconcile"})
    if r.status_code != 400:
        _fail(f"middle: reconcile should 400, got {r.status_code}")
    _ok("middle: reconcile rejected (400)")


def smoke_console(cfg: Path, log_dir: Path) -> None:
    from fastapi.testclient import TestClient

    from data2agent.connect.config import load_config, PlatformConfig
    from data2agent.console.app import create_app

    token = "smoke-token"
    loaded = load_config(cfg)
    platform_cfg = PlatformConfig(templates=loaded.templates, landing=loaded.landing)
    import yaml as _yaml
    platform_yaml = cfg.parent / "platform.yaml"
    platform_yaml.write_text(
        _yaml.safe_dump({"templates": loaded.templates, "landing": loaded.landing}),
        encoding="utf-8")
    client = TestClient(
        create_app(
            loaded.landing,
            loaded.templates,
            platform_cfg,
            token=token,
            config_path=platform_yaml,
            log_dir=log_dir,
        )
    )
    h = {"Authorization": f"Bearer {token}"}

    redirects = {
        "/config": "/settings",
        "/debug": "/mcp",
        "/v0": "/",
        "/v1/": "/",
    }
    for path, location in redirects.items():
        r = client.get(path, headers=h, follow_redirects=False)
        if r.status_code != 302 or r.headers.get("location") != location:
            _fail(f"console: GET {path} redirect unexpected: {r.status_code} {r.headers.get('location')}")
    _ok("console: legacy HTML routes redirect to Vue")

    r = client.get("/api/config", headers=h)
    if r.status_code != 200:
        _fail(f"console: GET /api/config → {r.status_code}")
    r = client.post(
        "/api/config/validate",
        headers=h,
        json={"templates": str(ROOT / "templates"), "landing": loaded.landing},
    )
    if r.status_code != 200 or r.json().get("ok") is not True:
        _fail(f"console: validate: {r.text[:200]}")
    _ok("console: /api/config + validate")

    r = client.get("/api/services", headers=h)
    if r.status_code != 200 or "console" not in r.json():
        _fail(f"console: /api/services: {r.text[:200]}")
    _ok("console: /api/services")

    r = client.post(
        "/api/debug/mcp-call",
        headers=h,
        json={"tool": "propose_action", "arguments": {}},
    )
    if r.status_code not in (400, 403, 422):
        # whitelist rejection — accept any client error
        if r.status_code == 200 and r.json().get("ok") is True:
            _fail("console: mcp-call must reject propose_action")
    _ok("console: mcp-call whitelist rejects propose_action")


def main() -> int:
    print(f"ROOT={ROOT}")
    try:
        import fastapi  # noqa: F401
        import jinja2  # noqa: F401
    except ImportError as e:
        _fail(f"missing deps ({e}); run: pip install -e \".[dev,connect,middle_admin,console]\"")

    with tempfile.TemporaryDirectory(prefix="d2a-smoke-") as td:
        tmp = Path(td)
        cfg = _prepare(tmp)
        print(f"config={cfg}")
        smoke_middle(cfg, tmp / "logs" / "d2a-connector.log")
        smoke_console(cfg, tmp / "logs")

    print("\nSMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
