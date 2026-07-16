from pathlib import Path

import yaml

from data2agent.admin_common.auth_token import resolve_token
from data2agent.admin_common.config_edit import (
    MIDDLE_EDITABLE,
    merge_whitelist_and_save,
)
from data2agent.admin_common.logs import tail_lines


def test_merge_whitelist_preserves_secrets_and_backs_up(tmp_path):
    p = tmp_path / "connect.yaml"
    p.write_text(
        "templates: t\nlanding: L\nsources:\n  digiwin_e10:\n"
        "    adapter: mssql_readonly\n    dsn_env: D2A_E10_DSN\n"
        "    sync_every: 30m\n    sink: {type: http, url: http://a:8850, token_env: D2A_INGEST_TOKEN}\n",
        encoding="utf-8",
    )
    ok, errors = merge_whitelist_and_save(
        p,
        MIDDLE_EDITABLE,
        {"sources": {"digiwin_e10": {"sync_every": "15m", "dsn_env": "HACKED"}}},
        validate=None,  # 本单元跳过 load_config；后续 Task 接真实校验
    )
    assert ok and not errors
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["sources"]["digiwin_e10"]["sync_every"] == "15m"
    assert data["sources"]["digiwin_e10"]["dsn_env"] == "D2A_E10_DSN"
    assert list(tmp_path.glob("connect.yaml.bak*")) or (tmp_path / "connect.yaml.bak").exists()


def test_merge_whitelist_validate_failure_restores_backup(tmp_path):
    p = tmp_path / "connect.yaml"
    p.write_text("templates: t\nlanding: L\n", encoding="utf-8")

    def fail_validate(_path: Path) -> None:
        raise ValueError("invalid config")

    ok, errors = merge_whitelist_and_save(
        p,
        MIDDLE_EDITABLE,
        {"templates": "new"},
        validate=fail_validate,
    )
    assert not ok
    assert errors and errors[0]["message"] == "invalid config"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["templates"] == "t"
    assert list(tmp_path.glob("connect.yaml.bak*"))


def test_resolve_token_cli_over_env(monkeypatch):
    monkeypatch.setenv("D2A_MIDDLE_ADMIN_TOKEN", "from-env")
    assert resolve_token("from-cli", "D2A_MIDDLE_ADMIN_TOKEN") == "from-cli"


def test_resolve_token_env_fallback(monkeypatch):
    monkeypatch.setenv("D2A_MIDDLE_ADMIN_TOKEN", "  secret  ")
    assert resolve_token(None, "D2A_MIDDLE_ADMIN_TOKEN") == "secret"


def test_resolve_token_empty(monkeypatch):
    monkeypatch.delenv("D2A_MIDDLE_ADMIN_TOKEN", raising=False)
    assert resolve_token(None, "D2A_MIDDLE_ADMIN_TOKEN") is None


def test_tail_lines_reads_and_filters(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("INFO ok\nERROR boom\nINFO fine\n", encoding="utf-8")
    ok, text = tail_lines(log, lines=10, level="ERROR")
    assert ok
    assert "ERROR boom" in text
    assert "INFO ok" not in text


def test_tail_lines_missing_file(tmp_path):
    ok, text = tail_lines(tmp_path / "missing.log")
    assert not ok
    assert text
