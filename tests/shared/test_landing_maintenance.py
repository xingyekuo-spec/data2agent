from __future__ import annotations

from datetime import datetime, timedelta

from data2agent.shared.store.landing import LandingStore


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
