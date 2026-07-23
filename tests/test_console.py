"""运维控制台测试:只读/完整模式、视图、动作、Token 认证、窗口约束。"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.config import load_config  # noqa: E402
from data2agent.connect.dataset_publish import build_dataset  # noqa: E402
from data2agent.connect.increment import incremental_sync, watermarks_from_pack  # noqa: E402
from data2agent.connect.landing import LandingStore, raw_table_name  # noqa: E402
from data2agent.connect.sync import whitelist_from_pack  # noqa: E402
from data2agent.console.app import create_app  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.showroom.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def env(tmp_path):
    """seed 源库 + 完整管道后的落地库 + connect.yaml。"""
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
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: LAST_MODIFIED_DATE\n",
        encoding="utf-8")
    return landing, cfg_file


def test_readonly_mode_views_and_blocked_actions(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v1/"

    o = client.get("/api/overview").json()
    assert o["readonly"] is True
    assert {s["source"] for s in o["sources"]} == {SOURCE}
    by = {x["object"]: x for x in o["objects"]}
    assert by["Customer"]["rows"] == 24 and by["Quotation"]["rows"] == 180

    assert client.get("/api/runs").json()[0]["status"] == "ok"
    assert client.get("/api/audit").json(), "审计日志应有内容"

    r = client.post("/api/actions/sync", json={"source": SOURCE})
    assert r.status_code == 409 and "只读模式" in r.json()["detail"]


def test_full_mode_actions(env):
    landing, cfg_file = env
    client = TestClient(create_app("ignored", "ignored", load_config(cfg_file)))
    o = client.get("/api/overview").json()
    assert o["readonly"] is False

    r = client.post("/api/actions/sync", json={"source": SOURCE}).json()
    assert r["executed"] is True
    r = client.post("/api/actions/reconcile", json={"source": SOURCE, "deep": False}).json()
    assert r["executed"] is True
    r = client.post("/api/actions/apply", json={"source": SOURCE}).json()
    assert r["executed"] is True and not r["aborted"]

    r = client.post("/api/actions/sync", json={"source": "nope"})
    assert r.status_code == 404


def test_quarantine_view_and_retry(env):
    landing, cfg_file = env
    landing.con.execute(
        f'UPDATE "{raw_table_name(SOURCE, "QUOTATION")}" SET DOC_NO = NULL WHERE Id = 5')
    landing.con.commit()
    client = TestClient(create_app("ignored", "ignored", load_config(cfg_file)))

    r = client.post("/api/actions/retry", json={"source": SOURCE, "object": "Quotation"}).json()
    assert r["mapped"] == 179 and r["quarantined"] == 1
    q = client.get("/api/quarantine").json()
    assert len(q) == 1 and "映射失败" in q[0]["reason"]

    r = client.post("/api/actions/retry", json={"source": SOURCE})
    assert r.status_code == 422  # 缺 object


def test_window_blocks_console_actions(env, tmp_path):
    landing, cfg_file = env
    t2 = datetime.now() + timedelta(hours=2)
    t3 = datetime.now() + timedelta(hours=3)
    closed = tmp_path / "closed.yaml"
    closed.write_text(
        cfg_file.read_text(encoding="utf-8")
        + f'    windows: ["{t2:%H:%M}-{t3:%H:%M}"]\n', encoding="utf-8")
    client = TestClient(create_app("ignored", "ignored", load_config(closed)))
    r = client.post("/api/actions/sync", json={"source": SOURCE}).json()
    assert r["executed"] is False and "窗口" in r["note"], "窗口约束对控制台同样生效"


def test_token_auth(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates", token="s3cret"))
    assert client.get("/", follow_redirects=False).headers["location"] == "/v1/"
    assert client.get("/api/overview").status_code == 401
    ok = client.get("/api/overview", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_console_config_whitelist(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    client = TestClient(create_app(
        cfg.landing, cfg.templates, cfg, token="t",
        config_path=cfg_file, log_dir=Path(".")))
    h = {"Authorization": "Bearer t"}
    r = client.get("/api/config", headers=h)
    assert r.status_code == 200
    assert r.json()["app_version"] == "0.4.0"
    assert r.json()["build_version"] is None
    r2 = client.post("/api/config", headers=h, json={
        "landing": str(landing.db_path),
        "templates": str(ROOT / "templates"),
    })
    assert r2.json()["ok"] is True


def test_config_validate_without_save(env):
    landing, cfg_file = env
    cfg = load_config(cfg_file)
    client = TestClient(create_app(
        cfg.landing, cfg.templates, cfg, token="t",
        config_path=cfg_file, log_dir=Path(".")))
    h = {"Authorization": "Bearer t"}
    before = cfg_file.read_text(encoding="utf-8")
    r = client.post("/api/config/validate", headers=h, json={
        "landing": str(landing.db_path),
        "templates": str(ROOT / "templates"),
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert cfg_file.read_text(encoding="utf-8") == before


def test_legacy_html_routes_redirect_to_vue(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    expected = {
        "/": "/v1/",
        "/config": "/v1/settings",
        "/logs": "/v1/logs",
        "/debug": "/v1/mcp",
        "/v0": "/v1/",
    }
    for path, location in expected.items():
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == location
