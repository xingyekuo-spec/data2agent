from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from data2agent.middle.maintenance.__main__ import run_once
from data2agent.shared.store.landing import LandingStore
from data2agent.shared.store.table import TableInfo


def test_online_backup_and_retention_prune(tmp_path):
    db = tmp_path / "factory.sqlite"
    store = LandingStore(db)
    old = (datetime.now() - timedelta(days=120)).isoformat(timespec="seconds")
    store.con.execute(
        "INSERT INTO d2a_audit_log "
        "(ts, source, action, sql, rows, duration_ms) "
        "VALUES (?, 's', 'read', 'SELECT 1', 1, 0)",
        (old,))
    store.con.commit()

    backup = store.backup_to(tmp_path / "backups" / "factory.sqlite")
    restored = LandingStore.open_readonly(backup)
    try:
        assert restored.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.con.execute(
            "SELECT COUNT(*) FROM d2a_audit_log").fetchone()[0] == 1
    finally:
        restored.con.close()

    counts = store.prune_operational_history(
        retention_days=90, receipt_days=365)
    assert counts["audit"] == 1


def test_recover_abandoned_runs_closes_steps(tmp_path):
    store = LandingStore(tmp_path / "middle.sqlite")
    run_id = store.start_run("s", "sync")
    step_id = store.add_step(run_id, 1, "table", "T")
    assert store.recover_abandoned_runs("s") == 1
    run = store.con.execute(
        "SELECT status, finished_at FROM d2a_sync_run WHERE id = ?", (run_id,)
    ).fetchone()
    step = store.con.execute(
        "SELECT status, finished_at FROM d2a_run_step WHERE id = ?", (step_id,)
    ).fetchone()
    assert run["status"] == "failed" and run["finished_at"]
    assert step["status"] == "aborted" and step["finished_at"]


def test_cleanup_abandoned_staging_drops_only_old_open_state(tmp_path):
    store = LandingStore(tmp_path / "platform.sqlite")
    info = TableInfo("T", [("ID", "int")], ["ID"])
    snapshot = store.begin_snapshot("s", info, "old-snapshot")
    store.begin_ingest_generation("s", "old-generation", ["T"])
    store.con.execute(
        "UPDATE d2a_snapshot SET created_at = '2000-01-01T00:00:00', "
        "last_activity_at = '2000-01-01T00:00:00' "
        "WHERE source = 's' AND snapshot_id = 'old-snapshot'")
    store.con.execute(
        "UPDATE d2a_ingest_generation SET created_at = '2000-01-01T00:00:00', "
        "last_activity_at = '2000-01-01T00:00:00' "
        "WHERE source = 's' AND generation_id = 'old-generation'")
    store.con.commit()
    counts = store.cleanup_abandoned_staging(max_age_hours=1)
    assert counts["snapshots"] == 1 and counts["generations"] == 1
    assert store.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (snapshot["staging_table"],),
    ).fetchone() is None


def test_cleanup_keeps_old_staging_with_recent_activity(tmp_path):
    store = LandingStore(tmp_path / "platform.sqlite")
    info = TableInfo("T", [("ID", "int")], ["ID"])
    snapshot = store.begin_snapshot("s", info, "active-snapshot")
    store.con.execute(
        "UPDATE d2a_snapshot SET created_at = '2000-01-01T00:00:00' "
        "WHERE source = 's' AND snapshot_id = 'active-snapshot'")
    store.con.commit()

    counts = store.cleanup_abandoned_staging(max_age_hours=1)

    assert counts["snapshots"] == 0
    assert store.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (snapshot["staging_table"],),
    ).fetchone() is not None


def test_middle_maintenance_publishes_success_and_failure_status(tmp_path):
    landing = tmp_path / "data" / "middle.sqlite"
    config = tmp_path / "connect.yaml"
    config.write_text(
        f"landing: {landing}\nsources: {{}}\n", encoding="utf-8")
    status = tmp_path / "data" / "run" / "maintenance-status.json"

    result = run_once(
        config, backup_dir=tmp_path / "backups", min_free_gb=0,
        status_file=status)
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert result["integrity"] == "ok"
    assert payload["status"] == "ok"
    assert payload["result"]["backup"].endswith(".sqlite")

    with pytest.raises(RuntimeError, match="磁盘可用空间不足"):
        run_once(config, min_free_gb=10**9, status_file=status)
    failed = json.loads(status.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert "RuntimeError" in failed["error"]
