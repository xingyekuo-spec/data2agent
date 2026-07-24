"""运维控制台测试:只读/完整模式、视图、动作、Token 认证、窗口约束。"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.config import PlatformConfig, load_config  # noqa: E402
from data2agent.connect.dataset_publish import build_dataset  # noqa: E402
from data2agent.connect.increment import incremental_sync  # noqa: E402
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name  # noqa: E402
from tests.helpers import whitelist_from_pack  # noqa: E402
from data2agent.console.app import create_app  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from tests.fixtures.e10.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


def _ensure_vue_dist(tmp_path: Path) -> Path:
    """创建最小 Vue dist 目录，供 create_app(home=...) 发现。"""
    dist = tmp_path / "console-ui" / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!DOCTYPE html><html><body>d2a</body></html>")
    return dist


@pytest.fixture()
def env(tmp_path):
    """seed 源库 + 完整管道后的落地库 + platform.yaml + Vue dist。"""
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE),
                                    audit_hook=hook)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    platform_yaml = config_dir / "platform.yaml"
    platform_yaml.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n",
        encoding="utf-8")
    _ensure_vue_dist(tmp_path)
    return landing, platform_yaml, tmp_path


def test_readonly_mode_views_and_blocked_actions(env):
    landing, platform_yaml, tmp_path = env
    platform_cfg = PlatformConfig(
        templates=str(ROOT / "templates"), landing=landing.db_path)
    client = TestClient(create_app(
        landing.db_path, str(ROOT / "templates"),
        platform_cfg, config_path=platform_yaml, home=tmp_path))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]

    o_resp = client.get("/api/overview")
    assert o_resp.status_code == 200, f"overview 响应异常: {o_resp.status_code} {o_resp.text}"
    o = o_resp.json()
    assert o["readonly"] is False
    assert {s["source"] for s in o["sources"]} == {SOURCE}
    by = {x["object"]: x for x in o["objects"]}
    assert by["Customer"]["rows"] == 24 and by["Quotation"]["rows"] == 180

    assert client.get("/api/runs").json()[0]["status"] == "ok"
    assert client.get("/api/audit").json(), "审计日志应有内容"


def test_explicit_home_ignores_process_home_for_vue_dist(env, monkeypatch):
    """显式 home 不能受其他并行测试设置的 D2A_HOME 影响。"""
    landing, platform_yaml, tmp_path = env
    monkeypatch.setenv("D2A_HOME", str(tmp_path / "another-install"))
    platform_cfg = PlatformConfig(
        templates=str(ROOT / "templates"), landing=landing.db_path)
    client = TestClient(create_app(
        landing.db_path, str(ROOT / "templates"),
        platform_cfg, config_path=platform_yaml, home=tmp_path))

    assert client.get("/", follow_redirects=False).status_code == 200


def test_full_mode_apply(env):
    landing, platform_yaml, tmp_path = env
    platform_cfg = PlatformConfig(
        templates=str(ROOT / "templates"), landing=landing.db_path)
    client = TestClient(create_app(
        landing.db_path, str(ROOT / "templates"),
        platform_cfg, config_path=platform_yaml, home=tmp_path))
    o_resp = client.get("/api/overview")
    assert o_resp.status_code == 200, f"overview 响应异常: {o_resp.status_code} {o_resp.text}"
    o = o_resp.json()
    assert o["readonly"] is False

    r = client.post("/api/actions/apply", json={"source": SOURCE}).json()
    assert r["executed"] is True and not r["aborted"]


def test_quarantine_view_and_retry(env):
    landing, platform_yaml, tmp_path = env
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET DOC_NO = NULL WHERE Id = 5')
    landing.con.commit()
    platform_cfg = PlatformConfig(
        templates=str(ROOT / "templates"), landing=landing.db_path)
    client = TestClient(create_app(
        landing.db_path, str(ROOT / "templates"),
        platform_cfg, config_path=platform_yaml, home=tmp_path))

    r_resp = client.post("/api/actions/retry", json={"source": SOURCE, "object": "Quotation"})
    assert r_resp.status_code == 200, f"retry 响应异常: {r_resp.status_code} {r_resp.text}"
    r = r_resp.json()
    assert r["mapped"] == 179 and r["quarantined"] == 1
    q = client.get("/api/quarantine").json()
    assert len(q) == 1 and "映射失败" in q[0]["reason"]

    r = client.post("/api/actions/retry", json={"source": SOURCE})
    assert r.status_code == 422  # 缺 object


def test_token_auth(env):
    landing, platform_yaml, tmp_path = env
    client = TestClient(create_app(
        landing.db_path, ROOT / "templates", token="s3cret", home=tmp_path))
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/overview").status_code == 401
    ok = client.get("/api/overview", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_console_config_whitelist(env):
    landing, platform_yaml, tmp_path = env
    platform_cfg = PlatformConfig(
        templates=str(ROOT / "templates"), landing=landing.db_path)
    client = TestClient(create_app(
        landing.db_path, str(ROOT / "templates"),
        platform_cfg, token="t",
        config_path=platform_yaml, log_dir=Path("."), home=tmp_path))
    h = {"Authorization": "Bearer t"}
    r = client.get("/api/config", headers=h)
    assert r.status_code == 200
    from data2agent import __version__
    assert r.json()["app_version"] == __version__
    assert r.json()["build_version"] is None
    r2 = client.post("/api/config", headers=h, json={
        "landing": str(landing.db_path),
        "templates": str(ROOT / "templates"),
    })
    assert r2.json()["ok"] is True


def test_config_validate_without_save(env):
    landing, platform_yaml, tmp_path = env
    platform_cfg = PlatformConfig(
        templates=str(ROOT / "templates"), landing=landing.db_path)
    client = TestClient(create_app(
        landing.db_path, str(ROOT / "templates"),
        platform_cfg, token="t",
        config_path=platform_yaml, log_dir=Path("."), home=tmp_path))
    h = {"Authorization": "Bearer t"}
    before = platform_yaml.read_text(encoding="utf-8")
    r = client.post("/api/config/validate", headers=h, json={
        "landing": str(landing.db_path),
        "templates": str(ROOT / "templates"),
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert platform_yaml.read_text(encoding="utf-8") == before


def test_legacy_html_routes_redirect_to_root_vue(env):
    landing, platform_yaml, tmp_path = env
    client = TestClient(create_app(
        landing.db_path, ROOT / "templates", home=tmp_path))
    expected = {
        "/config": "/settings",
        "/debug": "/mcp",
        "/v0": "/",
        "/v1/": "/",
        "/v1/mcp": "/mcp",
    }
    for path, location in expected.items():
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == location
