"""Tests for home layout + secrets + setup yaml builders."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from data2agent.admin_common.home_layout import HomeLayout, resolve_templates
from data2agent.admin_common.secrets_file import apply_secrets_to_environ, load_secrets, save_secrets
from data2agent.admin_common.setup_yaml import (
    build_middle_connect_yaml,
    build_odbc_dsn,
    load_default_erp_tables,
    write_yaml,
)
from data2agent.connect.config import load_config


def test_secrets_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "secrets.env"
    save_secrets(p, {"D2A_INGEST_TOKEN": "abc", "D2A_E10_DSN": "DRIVER={x};PWD=y"})
    data = load_secrets(p)
    assert data["D2A_INGEST_TOKEN"] == "abc"
    monkeypatch.delenv("D2A_INGEST_TOKEN", raising=False)
    apply_secrets_to_environ(p)
    assert os.environ["D2A_INGEST_TOKEN"] == "abc"


def test_build_middle_yaml_validates(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    # point templates at repo
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "app").mkdir()
    # symlink or just rely on resolve_templates falling back to repo
    data = build_middle_connect_yaml(home, platform_url="http://10.0.0.1:8850")
    # force templates to repo for load_config
    data["templates"] = str(root / "templates")
    path = home.connect_yaml
    write_yaml(path, data)
    # load_config needs mssql dsn_env present as name only — ok
    cfg = load_config(path)
    assert cfg.sources["digiwin_e10"].sink.type == "http"
    assert cfg.sources["digiwin_e10"].sink.url == "http://10.0.0.1:8850"
    assert cfg.sources["digiwin_e10"].tables["ITEM_WAREHOUSE"].mode == "full_refresh"


def test_middle_setup_uses_independent_erp_profile(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    tables = load_default_erp_tables(home)
    assert "ITEM_WAREHOUSE" in tables
    assert tables["ITEM_WAREHOUSE"] == {"mode": "full_refresh"}
    # Object templates are neither loaded nor used to obtain this table policy.
    from data2agent.scenarios.e10_dead_stock_schema import VERIFIED_E10_COLUMNS

    assert set(VERIFIED_E10_COLUMNS).issubset(tables)
    assert len(tables) == len(VERIFIED_E10_COLUMNS) + 6
    assert all(spec["mode"] == "full_refresh"
               for name, spec in tables.items() if name in VERIFIED_E10_COLUMNS)


def test_middle_setup_prefers_portable_erp_profile(tmp_path):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    profile = home.app / "erp-configs" / "digiwin_e10.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "tables:\n  FACTORY_STOCK:\n    mode: full_refresh\n",
        encoding="utf-8",
    )
    assert load_default_erp_tables(home) == {
        "FACTORY_STOCK": {"mode": "full_refresh"},
    }


def test_load_home_secrets_if_present(tmp_path, monkeypatch):
    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    save_secrets(home.secrets_env, {"D2A_INGEST_TOKEN": "from-file"})
    monkeypatch.setenv("D2A_HOME", str(tmp_path))
    monkeypatch.delenv("D2A_INGEST_TOKEN", raising=False)
    from data2agent.admin_common.secrets_file import load_home_secrets_if_present
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

    from data2agent.middle_admin.app import create_app

    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    # point templates via app/ so resolve_templates finds them
    root = Path(__file__).resolve().parents[1]
    import shutil
    shutil.copytree(root / "templates", home.app / "templates")

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
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert home.connect_yaml.is_file()
    assert home.secrets_env.is_file()
    secrets = load_secrets(home.secrets_env)
    assert secrets["D2A_INGEST_TOKEN"] == "ingest-tok"
    assert "PWD=secret" in secrets["D2A_E10_DSN"]
    assert "password" not in home.connect_yaml.read_text(encoding="utf-8").lower()

    # after setup, protected APIs need token
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer admin-tok"}).status_code == 200


def test_platform_browser_setup(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from data2agent.console.app import create_app

    home = HomeLayout(tmp_path)
    home.ensure_dirs()
    root = Path(__file__).resolve().parents[1]
    import shutil
    shutil.copytree(root / "templates", home.app / "templates")

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
