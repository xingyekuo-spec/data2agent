"""Tests for home layout + secrets + setup yaml builders."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from data2agent.shared.admin.home_layout import HomeLayout, resolve_templates
from data2agent.shared.admin.secrets_file import apply_secrets_to_environ, load_secrets, save_secrets
from data2agent.shared.admin.setup_yaml import (
    build_middle_connect_yaml,
    build_odbc_dsn,
    write_yaml,
)
from data2agent.shared.config import load_config


def test_secrets_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "secrets.env"
    save_secrets(p, {"D2A_INGEST_TOKEN": "abc", "D2A_E10_DSN": "DRIVER={x};PWD=y"})
    data = load_secrets(p)
    assert data["D2A_INGEST_TOKEN"] == "abc"
    monkeypatch.delenv("D2A_INGEST_TOKEN", raising=False)
    apply_secrets_to_environ(p)
    assert os.environ["D2A_INGEST_TOKEN"] == "abc"


def test_secrets_roundtrip_preserves_backslashes_newlines_and_quotes(tmp_path):
    path = tmp_path / "secrets.env"
    values = {
        "D2A_E10_DSN": r"SERVER=HOST\INSTANCE;PWD=p;a}ss\\word",
        "D2A_INGEST_TOKEN": "line1\nline2\"quoted\"",
    }
    save_secrets(path, values)
    assert load_secrets(path) == values


def test_odbc_dsn_braces_form_values_to_prevent_attribute_injection():
    dsn = build_odbc_dsn(
        server="erp;TrustServerCertificate=yes",
        database="E10;Encrypt=no",
        user="ro;UID=admin",
        password="p;a}ss",
    )
    assert "SERVER={erp;TrustServerCertificate=yes,1433}" in dsn
    assert "DATABASE={E10;Encrypt=no}" in dsn
    assert "UID={ro;UID=admin}" in dsn
    assert "PWD={p;a}}ss}" in dsn


def test_build_middle_yaml_validates(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "app").mkdir()
    data = build_middle_connect_yaml(home, platform_url="http://10.0.0.1:8850")
    data["templates"] = str(root / "templates")
    path = home.connect_yaml
    write_yaml(path, data)
    cfg = load_config(path)
    assert cfg.deployment_mode == "production"
    assert cfg.state_db.endswith("middle-state.sqlite")
    assert cfg.sources["digiwin_e10"].sink.type == "http"
    assert cfg.sources["digiwin_e10"].spool.policy == "strict_stream"
    assert cfg.sources["digiwin_e10"].sink.url == "http://10.0.0.1:8850"
    assert cfg.sources["digiwin_e10"].tables == {}
    assert cfg.sources["digiwin_e10"].reconcile_at == "05:30"
    assert cfg.sources["digiwin_e10"].reconcile_deep_at == "03:30"
    assert cfg.sources["digiwin_e10"].reconcile_deep_day_of_week == "sun"


def test_new_install_has_empty_tables(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    data = build_middle_connect_yaml(home, platform_url="http://10.0.0.1:8850")
    assert data["sources"]["digiwin_e10"]["tables"] == {}
    assert "landing" not in data
    assert data["deployment_mode"] == "production"


def test_build_middle_yaml_can_set_sync_start_at(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    data = build_middle_connect_yaml(
        home,
        platform_url="http://10.0.0.1:8850",
        sync_every="1d",
        sync_start_at="02:00",
        start_date="2026-01-01",
    )
    source = data["sources"]["digiwin_e10"]
    assert source["sync_every"] == "1d"
    assert source["sync_start_at"] == "02:00"
    assert source["start_date"] == "2026-01-01"
    # 缺省不写 start_date(留空按回看窗口起抽)
    data2 = build_middle_connect_yaml(home, platform_url="http://10.0.0.1:8850")
    assert "start_date" not in data2["sources"]["digiwin_e10"]


def test_load_home_secrets_if_present(tmp_path, monkeypatch):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    save_secrets(home.secrets_env, {"D2A_INGEST_TOKEN": "from-file"})
    monkeypatch.setenv("D2A_HOME", str(tmp_path))
    monkeypatch.delenv("D2A_INGEST_TOKEN", raising=False)
    from data2agent.shared.admin.secrets_file import load_home_secrets_if_present
    assert load_home_secrets_if_present() == home.secrets_env
    assert os.environ["D2A_INGEST_TOKEN"] == "from-file"


def test_odbc_named_instance():
    dsn = build_odbc_dsn(
        server=r"DESKTOP\SQLEXPRESS",
        database="E10",
        user="u",
        password="p",
        port=1433,
    )
    assert "SQLEXPRESS" in dsn and ",1433" not in dsn


def test_middle_browser_setup(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from data2agent.shared.admin.secrets_file import restore_environ
    from data2agent.middle.admin.app import create_app

    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    # point templates via app/ so resolve_templates finds them
    root = Path(__file__).resolve().parents[2]
    import shutil
    shutil.copytree(root / "templates", home.app / "templates")

    secret_keys = (
        "D2A_INGEST_TOKEN", "D2A_MIDDLE_ADMIN_TOKEN", "D2A_E10_DSN", "D2A_MCP_TOKEN",
    )
    prior = {k: os.environ.get(k) for k in secret_keys}
    try:
        client = TestClient(create_app(home=home.root, token=None))
        assert client.get("/").status_code in (200, 302)
        st = client.get("/api/setup/status")
        assert st.status_code == 200 and st.json()["needs_setup"] is True

        r = client.post("/api/setup", json={
            "platform_url": "http://10.0.0.2:8850",
            "erp_server": "ERPHOST",
            "erp_database": "E10",
            "erp_user": "ro",
            "erp_password": "secret",
            "erp_port": 1433,
            "ingest_token": "ingest-tok",
            "admin_token": "admin-tok",
            "sync_start_at": "02:00",
            "start_date": "2026-01-01",
        })
        assert r.status_code == 200 and r.json()["ok"] is True
        assert home.connect_yaml.is_file()
        assert home.secrets_env.is_file()
        secrets = load_secrets(home.secrets_env)
        assert secrets["D2A_INGEST_TOKEN"] == "ingest-tok"
        assert "PWD={secret}" in secrets["D2A_E10_DSN"]
        assert "password" not in home.connect_yaml.read_text(encoding="utf-8").lower()
        cfg = load_config(home.connect_yaml)
        assert cfg.sources["digiwin_e10"].sync_start_at == "02:00"
        assert cfg.sources["digiwin_e10"].start_date == "2026-01-01"

        # after setup, protected APIs need token
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer admin-tok"}).status_code == 200
    finally:
        restore_environ(prior)


def test_platform_browser_setup(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from data2agent.shared.admin.secrets_file import restore_environ
    from data2agent.platform.console.app import create_app

    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    root = Path(__file__).resolve().parents[2]
    import shutil
    shutil.copytree(root / "templates", home.app / "templates")

    secret_keys = ("D2A_INGEST_TOKEN", "D2A_CONSOLE_TOKEN", "D2A_MCP_TOKEN")
    prior = {k: os.environ.get(k) for k in secret_keys}
    try:
        client = TestClient(create_app(home=home.root))
        assert client.get("/api/setup/status").json()["needs_setup"] is True
        r = client.post("/api/setup", json={
            "ingest_token": "ingest-tok",
            "console_token": "console-tok",
        })
        assert r.status_code == 200 and r.json()["ok"] is True
        assert home.platform_yaml.is_file()
        secrets = load_secrets(home.secrets_env)
        assert secrets["D2A_INGEST_TOKEN"] == "ingest-tok"
        assert secrets["D2A_CONSOLE_TOKEN"] == "console-tok"
        assert secrets.get("D2A_MCP_TOKEN")  # auto-generated
        assert client.get("/api/overview").status_code == 401
        assert client.get(
            "/api/overview", headers={"Authorization": "Bearer console-tok"}
        ).status_code == 200
    finally:
        restore_environ(prior)
