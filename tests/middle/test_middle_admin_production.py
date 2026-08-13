"""中间机生产管理面的状态、安全与生命周期回归。"""

from __future__ import annotations

import json
import hashlib
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

import data2agent.middle.admin.app as middle_app
from data2agent.middle.admin.app import create_app
from data2agent.middle.admin.status import _data_residency
from data2agent.protocol.ingest import INGEST_PROTOCOL_VERSION
from data2agent.shared.config import ConnectConfig
from data2agent.shared.store.landing import LandingStore


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "production_source"
AUTH = {"Authorization": "Bearer secret"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_production_node(
    tmp_path: Path, *, adapter: str = "sqlite_readonly",
) -> tuple[TestClient, Path, Path]:
    data_dir = tmp_path / "data"
    run_dir = data_dir / "run"
    run_dir.mkdir(parents=True)
    state_db = data_dir / "middle-state.sqlite"
    store = LandingStore(state_db)
    store.con.close()

    source_db = tmp_path / "erp.sqlite"
    source_db.touch()
    adapter_lines = (
        f"    adapter: {adapter}\n"
        "    dsn_env: D2A_TEST_ERP_DSN\n"
        if adapter == "mssql_readonly"
        else "    adapter: sqlite_readonly\n" f"    path: {source_db}\n"
    )
    config = tmp_path / "connect.yaml"
    config.write_text(
        f"templates: {ROOT / 'templates'}\n"
        "deployment_mode: production\n"
        f"state_db: {state_db}\n"
        "sources:\n"
        f"  {SOURCE}:\n"
        + adapter_lines
        + "    tables:\n"
        "      CONTROLLED_TABLE:\n"
        "        mode: full_refresh\n"
        "        key_columns: [ID]\n"
        "    sink:\n"
        "      type: http\n"
        "      url: http://localhost:8850\n"
        "      token_env: D2A_TEST_INGEST_TOKEN\n"
        "    spool:\n"
        "      policy: strict_stream\n",
        encoding="utf-8",
    )
    process_status = {
        "updated_at_epoch": time.time(),
        "launcher_pid": 1234,
        "startup_mode": "headless",
        "processes": [
            {"name": "connector", "pid": 1235, "alive": True,
             "failed": False, "restarts": 0},
            {"name": "maintenance", "pid": 1236, "alive": True,
             "failed": False, "restarts": 0},
        ],
    }
    (run_dir / "process-status.json").write_text(
        json.dumps(process_status), encoding="utf-8")
    now = _now()
    (run_dir / "maintenance-status.json").write_text(json.dumps({
        "schema_version": 2,
        "status": "ok",
        "last_attempt": {"status": "ok", "finished_at": now},
        "last_success": {
            "finished_at": now,
            "backup_file": "middle-state-20260808-020000.sqlite",
            "backup_size_bytes": 4096,
            "backup_kind": "middle_state",
            "integrity": "ok",
        },
        "result": {
            "integrity": "ok", "free_gb": 20, "min_free_gb": 2,
            "pruned": {}, "abandoned": {}, "removed_backups": 0,
            "errors": [],
        },
    }), encoding="utf-8")
    (run_dir / "autostart-status.json").write_text(json.dumps({
        "installed": True, "task_name": "data2agent-middle",
        "checked_at": now,
    }), encoding="utf-8")
    (run_dir / "readiness-probes.json").write_text(json.dumps({
        "sources": {SOURCE: {
            "erp": {"checked_at": now, "ok": True, "status": "connected"},
            "platform": {
                "checked_at": now, "ok": True, "compatible": True,
                "local_protocol": INGEST_PROTOCOL_VERSION,
                "platform_supported": [INGEST_PROTOCOL_VERSION],
            },
        }},
    }), encoding="utf-8")
    return TestClient(create_app(
        config_path=config, token="secret", home=tmp_path,
    )), config, state_db


def test_production_readiness_and_security_headers(tmp_path):
    client, _, _ = _write_production_node(tmp_path)
    response = client.get("/status", headers=AUTH)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"

    # 静态脚本必须带版本指纹且禁止启发式缓存:升级后不允许出现
    # 新页面脚本 + 旧缓存共享 admin.js 的错配(runAction is not defined 回归)
    assert '/static/admin.js?v=' in response.text
    script = client.get("/static/admin.js", headers=AUTH)
    assert script.status_code == 200
    assert script.headers["cache-control"] == "no-cache"
    versioned = client.get("/static/admin.js?v=0.6.0", headers=AUTH)
    assert versioned.status_code == 200
    assert versioned.text == script.text

    status = client.get("/api/status", headers=AUTH).json()
    assert status["readiness"]["ready"] is True
    assert status["data_residency"]["raw_table_count"] == 0
    assert status["data_residency"]["spool_policies"] == {
        SOURCE: "strict_stream"}
    assert status["process_status"]["connector_running"] is True
    assert status["process_status"]["maintenance_running"] is True


def test_raw_table_is_redacted_and_blocks_readiness(tmp_path):
    client, _, state_db = _write_production_node(tmp_path)
    secret_table = "raw_customer_salary_secret"
    store = LandingStore(state_db)
    store.con.execute(f'CREATE TABLE "{secret_table}" (secret TEXT)')
    store.con.commit()
    store.con.close()

    status = client.get("/api/status", headers=AUTH).json()
    residency = status["data_residency"]
    assert residency["raw_table_count"] == 1
    assert residency["error_code"] == "middle_raw_persistence_detected"
    assert residency["compliant"] is False
    assert status["readiness"]["ready"] is False
    assert secret_table not in json.dumps(status, ensure_ascii=False)
    assert len(residency["raw_table_name_digests"][0]) == 12
    blocked = client.post("/api/alerts/silences", headers=AUTH, json={
        "alert_key": "readiness:data_residency", "hours": 1,
    })
    assert blocked.status_code == 409


def test_stale_launcher_never_reports_connector_running(tmp_path):
    client, _, state_db = _write_production_node(tmp_path)
    path = state_db.parent / "run" / "process-status.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at_epoch"] = time.time() - 60
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = client.get("/api/status", headers=AUTH).json()
    assert status["process_status"]["stale"] is True
    assert status["process_status"]["connector_running"] is False
    assert status["readiness"]["ready"] is False


def test_latest_maintenance_failure_is_not_hidden_by_old_success(tmp_path):
    client, _, state_db = _write_production_node(tmp_path)
    path = state_db.parent / "run" / "maintenance-status.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update({
        "status": "failed",
        "last_attempt": {
            "status": "failed", "finished_at": _now(),
            "error": "PermissionError: backup directory denied",
        },
        "error": "PermissionError: backup directory denied",
    })
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = client.get("/api/status", headers=AUTH).json()
    checks = {item["id"]: item for item in status["readiness"]["checks"]}
    assert checks["state_backup"]["status"] == "pass"
    assert checks["maintenance_last_attempt"]["status"] == "fail"
    assert checks["disk_space"]["status"] == "pass"
    assert status["readiness"]["ready"] is False
    assert any(
        item["key"] == "readiness:maintenance_last_attempt"
        for item in status["alerts"]
    )


def test_spool_is_active_only_while_its_source_sync_is_running(tmp_path):
    spool_dir = tmp_path / "encrypted-spool"
    spool_dir.mkdir(mode=0o700)
    spool_dir.chmod(0o700)
    source_hash = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()[:12]
    spool_file = spool_dir / f"d2a-full-{source_hash}-orphan.spool"
    spool_file.write_bytes(b"encrypted-volume-test")
    cfg = ConnectConfig.model_validate({
        "deployment_mode": "production",
        "state_db": str(tmp_path / "middle-state.sqlite"),
        "sources": {
            SOURCE: {
                "adapter": "sqlite_readonly",
                "path": str(tmp_path / "erp.sqlite"),
                "tables": {"T": {"mode": "full_refresh", "key_columns": ["ID"]}},
                "sink": {"type": "http", "url": "https://platform.example"},
                "spool": {
                    "policy": "encrypted_temp_volume",
                    "directory": str(spool_dir),
                    "encrypted_at_rest": True,
                },
            },
        },
    })
    store = LandingStore(cfg.state_db)
    process = {"connector_running": True}
    try:
        idle = _data_residency(cfg, store, process)
        assert idle["active_spool_count"] == 0
        assert idle["orphan_spool_count"] == 1
        assert idle["compliant"] is False

        run_id = store.start_run(SOURCE, "sync")
        active = _data_residency(cfg, store, process)
        assert active["active_spool_count"] == 1
        assert active["orphan_spool_count"] == 0
        store.finish_run(run_id, tables=0, rows=0, status="failed")
    finally:
        store.con.close()


def test_connection_checks_persist_only_sanitized_readiness_evidence(
    tmp_path, monkeypatch,
):
    client, _, state_db = _write_production_node(
        tmp_path, adapter="mssql_readonly")
    monkeypatch.setenv("D2A_TEST_ERP_DSN", "SERVER=secret;PWD=very-secret")
    monkeypatch.setenv("D2A_TEST_INGEST_TOKEN", "ingest-super-secret")
    monkeypatch.setattr(
        middle_app, "_probe_connection_with_timeout",
        lambda *_args, **_kwargs: {"status": "connected", "database": "ERP"},
    )
    monkeypatch.setattr(middle_app, "_http_get_json", lambda *_args, **_kwargs: {
        "ok": True,
        "active_ingest_protocol_version": INGEST_PROTOCOL_VERSION,
        "supported_ingest_protocol_versions": [INGEST_PROTOCOL_VERSION],
    })

    erp = client.post("/api/connection/test", headers=AUTH, json={
        "source": SOURCE}).json()
    platform = client.get(
        f"/api/config/connection-check?source={SOURCE}", headers=AUTH).json()
    assert erp["status"] == "connected"
    assert platform["compatible"] is True
    evidence = (state_db.parent / "run" / "readiness-probes.json").read_text(
        encoding="utf-8")
    assert "very-secret" not in evidence
    assert "ingest-super-secret" not in evidence
    assert "localhost:8850" not in evidence
    parsed = json.loads(evidence)
    assert parsed["sources"][SOURCE]["erp"]["ok"] is True
    assert parsed["sources"][SOURCE]["platform"]["compatible"] is True


def test_alert_lifecycle_recovers_without_incrementing_on_poll(tmp_path):
    client, _, state_db = _write_production_node(tmp_path)
    store = LandingStore(state_db)
    failed_run = store.start_run(SOURCE, "sync")
    store.add_step(
        failed_run, 1, "table", "CONTROLLED_TABLE",
        status="failed", error="network timeout",
    )
    store.finish_run(
        failed_run, tables=0, rows=0, status="failed",
        detail="network timeout",
    )
    store.con.close()

    first = client.get("/api/alerts", headers=AUTH).json()
    alert = next(item for item in first["alerts"] if item["key"].startswith("run:"))
    assert alert["status"] == "active"
    assert alert["occurrences"] == 1

    store = LandingStore(state_db)
    ok_run = store.start_run(SOURCE, "sync")
    store.add_step(ok_run, 1, "table", "CONTROLLED_TABLE", status="ok")
    store.finish_run(ok_run, tables=1, rows=1, status="ok")
    store.con.close()

    second = client.get("/api/alerts", headers=AUTH).json()
    recovered = next(item for item in second["alerts"] if item["key"] == alert["key"])
    assert recovered["status"] == "recovered"
    assert recovered["recovered_at"]
    third = client.get("/api/alerts", headers=AUTH).json()
    polled = next(item for item in third["alerts"] if item["key"] == alert["key"])
    assert polled["occurrences"] == 1


def test_run_and_push_apis_never_return_persisted_secret_details(tmp_path):
    client, _, state_db = _write_production_node(tmp_path)
    store = LandingStore(state_db)
    secret = "Authorization: Bearer api-super-secret"
    run_id = store.start_run(SOURCE, "sync")
    store.set_run_generation(run_id, "sync-sensitive-test")
    store.add_step(
        run_id, 1, "table", "CONTROLLED_TABLE",
        status="failed", error=f"{secret}; SELECT * FROM payroll",
    )
    store.finish_run(
        run_id, tables=0, rows=0, status="failed",
        detail=f"token=run-super-secret; {secret}",
    )
    store.record_push_log(
        SOURCE, "heartbeat_generation", "*", "generation",
        run_id=run_id, status="failed", generation_id="sync-sensitive-test",
        error_detail=f"DSN=erp-secret; password=push-super-secret; {secret}",
    )
    store.con.close()

    responses = [
        client.get("/api/runs", headers=AUTH),
        client.get(f"/api/runs/{run_id}", headers=AUTH),
        client.get("/api/push-logs", headers=AUTH),
    ]
    for response in responses:
        assert response.status_code == 200
        body = response.text
        assert "api-super-secret" not in body
        assert "run-super-secret" not in body
        assert "push-super-secret" not in body
        assert "SELECT * FROM payroll" not in body
        assert "已省略" in body
