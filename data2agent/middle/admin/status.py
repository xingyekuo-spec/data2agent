"""中间机可信状态聚合：调度、进程、维护、数据驻留与生产就绪度。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...protocol.ingest import INGEST_PROTOCOL_VERSION
from ...shared.config import (
    ConnectConfig,
    SourceConfig,
    config_revision,
    in_window,
    parse_window,
)
from ...shared.store.landing import LandingStore

STATUS_TTL_SECONDS = 15
BACKUP_MAX_AGE_SECONDS = 36 * 3600
PROBE_MAX_AGE_SECONDS = 24 * 3600


def _aware_iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    except (TypeError, ValueError):
        return None


def _age_seconds(value: str | None, now: datetime) -> int | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0, int((now.astimezone() - parsed.astimezone()).total_seconds()))


def _next_window_start(now: datetime, windows: list[str]) -> datetime | None:
    """窗外时返回最近未来窗口起点；无窗口限制则 None。"""
    if not windows:
        return None
    candidates: list[datetime] = []
    for raw in windows:
        start, _ = parse_window(raw)
        for day_offset in (0, 1, 2):
            dt = datetime.combine(now.date() + timedelta(days=day_offset), start)
            if now.tzinfo is not None:
                dt = dt.replace(tzinfo=now.tzinfo)
            if dt > now:
                candidates.append(dt)
    return min(candidates) if candidates else None


def _last_run_at(db: LandingStore, source: str) -> datetime | None:
    row = db.con.execute(
        "SELECT MAX(started_at) AS t FROM d2a_sync_run "
        "WHERE source = ? AND run_type = 'sync'",
        (source,),
    ).fetchone()
    if row and row["t"]:
        return _parse_time(row["t"])
    row = db.con.execute(
        "SELECT MAX(last_run_at) AS t FROM d2a_sync_state WHERE source = ?",
        (source,),
    ).fetchone()
    return _parse_time(row["t"]) if row and row["t"] else None


def _estimate_next_sync(
    now: datetime, scfg: SourceConfig, last_run: datetime | None,
) -> datetime:
    if not in_window(now.time(), scfg.windows):
        nxt = _next_window_start(now, scfg.windows)
        return nxt if nxt is not None else now
    if last_run is None:
        return scfg.sync_start_datetime_after(now)
    candidate = last_run + timedelta(seconds=scfg.sync_every_seconds())
    return candidate if candidate > now else now


def _run_dict(row) -> dict | None:
    if not row:
        return None
    keys = set(row.keys())
    return {
        "id": row["id"],
        "source": row["source"],
        "run_type": row["run_type"] if "run_type" in keys else None,
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"] if "finished_at" in keys else None,
        "tables": row["tables"] if "tables" in keys else None,
        "rows": row["rows"] if "rows" in keys else None,
        "detail": row["detail"] if "detail" in keys else None,
        "generation_id": row["generation_id"] if "generation_id" in keys else None,
    }


def _latest_run(
    db: LandingStore,
    source: str,
    *,
    run_type: str = "sync",
    status: str | None = None,
    deep: bool | None = None,
) -> dict | None:
    where = ["source = ?", "run_type = ?"]
    params: list[Any] = [source, run_type]
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if run_type == "reconcile" and deep is not None:
        where.append("detail LIKE ?" if deep else "COALESCE(detail, '') NOT LIKE ?")
        params.append("%reconcile-deep%")
    row = db.con.execute(
        "SELECT * FROM d2a_sync_run WHERE " + " AND ".join(where)
        + " ORDER BY started_at DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    return _run_dict(row)


def _running_run(db: LandingStore, source: str) -> dict | None:
    row = db.con.execute(
        "SELECT * FROM d2a_sync_run WHERE source = ? "
        "AND run_type IN ('sync', 'reconcile') "
        "AND status = 'running' ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()
    return _run_dict(row)


def _watermark_observability(
    db: LandingStore, source: str, rows: list[dict],
) -> list[dict]:
    """补充最近推进和连续不推进次数，不暴露复合运行键。"""
    for item in rows:
        history = db.con.execute(
            "SELECT s.watermark_before, s.watermark_after, "
            "COALESCE(s.progressed_at, s.finished_at, r.finished_at) AS observed_at "
            "FROM d2a_run_step s JOIN d2a_sync_run r ON r.id = s.run_id "
            "WHERE r.source = ? AND r.run_type = 'sync' AND s.target = ? "
            "AND s.status = 'ok' AND s.watermark_after IS NOT NULL "
            "ORDER BY s.id DESC LIMIT 10",
            (source, item["table_name"]),
        ).fetchall()
        unchanged = 0
        last_advance_at = None
        latest_before = latest_after = None
        for index, step in enumerate(history):
            try:
                before = json.loads(step["watermark_before"]) \
                    if step["watermark_before"] is not None else None
            except (TypeError, ValueError, json.JSONDecodeError):
                before = step["watermark_before"]
            try:
                after = json.loads(step["watermark_after"])
            except (TypeError, ValueError, json.JSONDecodeError):
                after = step["watermark_after"]
            if index == 0:
                latest_before, latest_after = before, after
            if before == after:
                if last_advance_at is None:
                    unchanged += 1
            elif last_advance_at is None:
                last_advance_at = step["observed_at"]
        high = item.get("high_water")
        parsed_high = _parse_time(str(high)) if high is not None else None
        if parsed_high is not None:
            value_type = "datetime"
        else:
            try:
                float(str(high))
                value_type = "number"
            except (TypeError, ValueError):
                value_type = "text" if high is not None else "unknown"
        advance: dict[str, Any]
        before_time = _parse_time(str(latest_before)) if latest_before is not None else None
        after_time = _parse_time(str(latest_after)) if latest_after is not None else None
        if before_time is not None and after_time is not None:
            advance = {
                "kind": "duration_seconds",
                "value": int((after_time - before_time).total_seconds()),
            }
        else:
            try:
                advance = {
                    "kind": "numeric",
                    "value": float(str(latest_after)) - float(str(latest_before)),
                }
            except (TypeError, ValueError):
                advance = {
                    "kind": "changed",
                    "value": latest_before != latest_after if history else None,
                }
        item.update({
            "value_type": value_type,
            "recent_advance": advance,
            "unchanged_successive_runs": unchanged,
            "stalled": unchanged >= 3,
            "last_advance_at": last_advance_at,
        })
    return rows


def _next_daily(now: datetime, raw: str | None, weekday: str | None = None) -> str | None:
    if not raw:
        return None
    hour, minute = (int(part) for part in raw.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday:
        weekdays = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
                    "fri": 4, "sat": 5, "sun": 6}
        target = weekdays[weekday]
        delta = (target - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= now:
            candidate += timedelta(days=7)
    elif candidate <= now:
        candidate += timedelta(days=1)
    return _aware_iso(candidate)


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _process_status(state_dir: Path, now_epoch: float) -> dict:
    payload = _read_json(state_dir / "run" / "process-status.json")
    try:
        updated = float(payload.get("updated_at_epoch", 0))
    except (TypeError, ValueError):
        updated = 0.0
    available = bool(payload)
    stale = not available or now_epoch - updated > STATUS_TTL_SECONDS
    processes: list[dict] = []
    for raw in payload.get("processes", []):
        if not isinstance(raw, dict):
            continue
        if raw.get("failed"):
            state = "circuit_open"
        elif raw.get("alive"):
            state = "running"
        else:
            state = "restarting"
        processes.append({
            "name": str(raw.get("name") or "unknown"),
            "pid": raw.get("pid"),
            "state": state,
            "alive": bool(raw.get("alive")),
            "failed": bool(raw.get("failed")),
            "restarts": int(raw.get("restarts") or 0),
            "last_exit_code": raw.get("last_exit_code"),
            "failed_at_epoch": raw.get("failed_at_epoch"),
            "cooldown_until_epoch": raw.get("cooldown_until_epoch"),
            "loaded_config_revision": raw.get("loaded_config_revision"),
        })
    connector = next((item for item in processes if item["name"] == "connector"), None)
    maintenance = next((item for item in processes if item["name"] == "maintenance"), None)
    return {
        "available": available,
        "supervised": available and not stale,
        "stale": stale,
        "status_ttl_seconds": STATUS_TTL_SECONDS,
        "updated_at_epoch": updated or None,
        "age_seconds": max(0, int(now_epoch - updated)) if updated else None,
        "launcher_pid": payload.get("launcher_pid"),
        "startup_mode": payload.get("startup_mode", "unknown"),
        "processes": processes,
        "connector": connector,
        "maintenance": maintenance,
        "connector_running": bool(connector and connector["state"] == "running" and not stale),
        "maintenance_running": bool(
            maintenance and maintenance["state"] == "running" and not stale),
    }


def _readiness_probes(state_dir: Path, now: datetime) -> dict:
    payload = _read_json(state_dir / "run" / "readiness-probes.json")
    result: dict[str, dict] = {}
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, dict):
        return result
    for source, raw_source in sources.items():
        if not isinstance(raw_source, dict):
            continue
        source_result: dict[str, dict] = {}
        for kind in ("erp", "platform"):
            raw = raw_source.get(kind)
            if not isinstance(raw, dict):
                continue
            age = _age_seconds(raw.get("checked_at"), now)
            source_result[kind] = {
                "checked_at": raw.get("checked_at"),
                "age_seconds": age,
                "fresh": age is not None and age <= PROBE_MAX_AGE_SECONDS,
                "ok": bool(raw.get("ok")),
                "status": raw.get("status"),
                "compatible": bool(raw.get("compatible")),
                "error_code": raw.get("error_code"),
                "local_protocol": raw.get("local_protocol"),
                "platform_supported": raw.get("platform_supported") or [],
            }
        result[str(source)] = source_result
    return result


def _maintenance_status(state_dir: Path, now: datetime) -> dict:
    payload = _read_json(state_dir / "run" / "maintenance-status.json")
    if not payload:
        return {
            "available": False,
            "status": "unknown",
            "backup_kind": "middle_state",
            "overdue": True,
        }
    attempt = payload.get("last_attempt") or {
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "error": payload.get("error"),
    }
    success = payload.get("last_success") or {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    if not success and payload.get("status") in ("ok", "partial") and result:
        success = {
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "backup_file": Path(str(result.get("backup") or "")).name or None,
            "backup_size_bytes": result.get("backup_size_bytes"),
            "backup_kind": "middle_state",
            "integrity": result.get("integrity"),
        }
    backup_age = _age_seconds(success.get("finished_at"), now)
    return {
        "available": True,
        "status": payload.get("status", "unknown"),
        "backup_kind": "middle_state",
        "last_attempt_at": attempt.get("finished_at"),
        "last_attempt": attempt,
        "last_success_at": success.get("finished_at"),
        "backup_file": Path(str(success.get("backup_file") or "")).name or None,
        "backup_size_bytes": success.get("backup_size_bytes"),
        "integrity": success.get("integrity"),
        "backup_age_seconds": backup_age,
        "overdue": backup_age is None or backup_age > BACKUP_MAX_AGE_SECONDS,
        "backup_max_age_seconds": BACKUP_MAX_AGE_SECONDS,
        "free_gb": result.get("free_gb"),
        "min_free_gb": result.get("min_free_gb"),
        "pruned": result.get("pruned") or {},
        "abandoned": result.get("abandoned") or {},
        "removed_backups": result.get("removed_backups", 0),
        "errors": result.get("errors") or attempt.get("errors") or [],
        "error": attempt.get("error") or payload.get("error"),
        "next_run_at": payload.get("next_run_at"),
    }


def _autostart_status(state_dir: Path) -> dict:
    payload = _read_json(state_dir / "run" / "autostart-status.json")
    if not payload:
        return {"status": "unknown", "installed": None, "task_name": None}
    installed = payload.get("installed")
    return {
        "status": (
            "installed" if installed is True else
            "not_installed" if installed is False else "unknown"),
        "installed": installed if isinstance(installed, bool) else None,
        "task_name": payload.get("task_name"),
        "checked_at": payload.get("checked_at"),
        "check_source": payload.get("check_source"),
        "error_code": payload.get("error_code"),
    }


def _data_residency(
    cfg: ConnectConfig, db: LandingStore, process: dict,
) -> dict:
    raw_names = [
        str(row[0]) for row in db.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw\\_%' ESCAPE '\\'"
        ).fetchall()
    ]
    # 只暴露不可逆摘要，避免状态 API 泄露 ERP 表名。
    raw_digests = [hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
                   for name in sorted(raw_names)]
    sink_types = sorted({source.sink.type for source in cfg.sources.values()})
    policies = {name: source.spool.policy for name, source in cfg.sources.items()}
    spool_sources: dict[str, dict] = {}
    active_spools = 0
    orphan_spools = 0
    for name, source in cfg.sources.items():
        directory_protected: bool | None = None
        found = 0
        if source.spool.directory:
            try:
                spool_dir = Path(source.spool.directory)
                prefix = "d2a-full-" + hashlib.sha256(
                    name.encode("utf-8")).hexdigest()[:12] + "-"
                found = len(list(spool_dir.glob(prefix + "*.spool")))
                directory_protected = (
                    (spool_dir.stat().st_mode & 0o077) == 0
                    if spool_dir.exists() else None)
            except OSError:
                found = 1
                directory_protected = False
        source_running = db.con.execute(
            "SELECT 1 FROM d2a_sync_run WHERE source = ? "
            "AND run_type = 'sync' AND status = 'running' LIMIT 1",
            (name,),
        ).fetchone() is not None
        spool_active = bool(
            found and process.get("connector_running") and source_running)
        if spool_active:
            active_spools += found
        else:
            orphan_spools += found
        spool_sources[name] = {
            "policy": source.spool.policy,
            "directory_configured": bool(source.spool.directory),
            "directory_protected": directory_protected,
            "encrypted_at_rest": source.spool.encrypted_at_rest,
            "active_count": found if spool_active else 0,
            "orphan_count": 0 if spool_active else found,
        }
    violations = cfg.production_violations()
    if cfg.deployment_mode == "production":
        for name, spool in spool_sources.items():
            if (
                spool["policy"] == "encrypted_temp_volume"
                and spool["directory_protected"] is not True
            ):
                violations.append(
                    f"源 {name}:加密 spool 目录不存在或权限未达到最小保护")
    if raw_names:
        violations.append(f"状态库发现 {len(raw_names)} 张 Raw 表")
    if orphan_spools:
        violations.append(f"发现 {orphan_spools} 个遗留 spool 文件")
    return {
        "deployment_mode": cfg.deployment_mode,
        "sink_type": sink_types[0] if len(sink_types) == 1 else "mixed",
        "sink_types": sink_types,
        "state_db_kind": "middle_state",
        "state_db_file": Path(cfg.state_db).name,
        "raw_table_count": len(raw_names),
        "raw_table_name_digests": raw_digests,
        "spool_policies": policies,
        "spool_sources": spool_sources,
        "spool_active": active_spools > 0,
        "active_spool_count": active_spools,
        "orphan_spool_count": orphan_spools,
        "compliant": not violations,
        "violations": violations,
        "error_code": "middle_raw_persistence_detected" if raw_names else (
            "orphan_spool_detected" if orphan_spools else
            "production_config_not_ready" if violations else None),
    }


def _check(
    check_id: str, status: str, detail: str, suggestion: str = "",
    *, required: bool = True,
) -> dict:
    return {
        "id": check_id,
        "status": status,
        "required": required,
        "detail": detail,
        "suggestion": suggestion or None,
    }


def _version_info() -> dict:
    try:
        version = importlib.metadata.version("data2agent")
    except importlib.metadata.PackageNotFoundError:
        version = "development"
    return {
        "middle_version": version,
        "ingest_protocol_version": INGEST_PROTOCOL_VERSION,
    }


def build_status(
    cfg: ConnectConfig,
    now: datetime | None = None,
    *,
    config_path: str | Path | None = None,
) -> dict:
    """返回中间机状态；所有结论均附真实/推算来源，不读取业务 Raw 行。"""
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    observed_at = _aware_iso(now)
    db = LandingStore(cfg.state_db)
    state_dir = Path(cfg.state_db).parent
    try:
        process = _process_status(state_dir, now.timestamp())
        maintenance = _maintenance_status(state_dir, now)
        autostart = _autostart_status(state_dir)
        probes = _readiness_probes(state_dir, now)
        residency = _data_residency(cfg, db, process)
        current_revision = (
            config_revision(config_path) if config_path and Path(config_path).is_file()
            else None
        )
        sources: list[dict] = []
        for name, scfg in cfg.sources.items():
            source_probes = probes.get(name, {})
            erp_probe = source_probes.get("erp", {})
            platform_probe = source_probes.get("platform", {})
            last_run = _last_run_at(db, name)
            latest = _latest_run(db, name)
            latest_success = _latest_run(db, name, status="ok")
            latest_failure = _latest_run(db, name, status="failed")
            running = _running_run(db, name)
            freshness_age = _age_seconds(
                latest_success.get("finished_at") if latest_success else None, now)
            freshness_limit = int(
                scfg.sync_every_seconds() + max(300, scfg.sync_every_seconds() * 0.25))
            if freshness_age is None:
                freshness_status = "unknown"
            elif freshness_age > freshness_limit:
                freshness_status = "overdue"
            else:
                freshness_status = "fresh"
            source_components = {
                "config": "ok" if scfg.tables else "failed",
                "connector": "ok" if process["connector_running"] else "failed",
                "erp": (
                    "ok" if erp_probe.get("fresh") and erp_probe.get("ok")
                    else "ok" if latest_success else
                    "failed" if erp_probe.get("fresh") else "unknown"),
                "platform": (
                    "ok" if platform_probe.get("fresh") and platform_probe.get("ok")
                    else "ok" if latest_success and latest_success.get("generation_id")
                    else "failed" if platform_probe.get("fresh") else "unknown"),
                "sync": freshness_status,
                "reconcile": "configured" if scfg.reconcile_at else "unknown",
            }
            source_health = "failed" if "failed" in source_components.values() else (
                "warning" if freshness_status in ("unknown", "overdue") else "ok")
            watermarks = _watermark_observability(
                db, name, db.list_sync_watermarks(name))
            sources.append({
                "source": name,
                "health": {"status": source_health, "components": source_components},
                "in_window": in_window(now.time(), scfg.windows),
                "windows": scfg.windows,
                "timezone": str(now.tzinfo),
                "sync_every": scfg.sync_every,
                "sync_start_at": scfg.sync_start_at,
                "last_run_at": _aware_iso(last_run) if last_run else None,
                "next_sync_at": _aware_iso(_estimate_next_sync(now, scfg, last_run)),
                "schedule": {
                    "derived": True,
                    "source": "derived_from_yaml",
                    "next_sync_at": _aware_iso(_estimate_next_sync(now, scfg, last_run)),
                    "next_reconcile_at": _next_daily(now, scfg.reconcile_at),
                    "next_deep_reconcile_at": _next_daily(
                        now, scfg.reconcile_deep_at,
                        scfg.reconcile_deep_day_of_week),
                },
                "watermarks": watermarks,
                "tables_configured": bool(scfg.table_whitelist()),
                "latest_run": latest,
                "latest_success": latest_success,
                "latest_failure": latest_failure,
                "running_run": running,
                "freshness": {
                    "status": freshness_status,
                    "age_seconds": freshness_age,
                    "limit_seconds": freshness_limit,
                },
                "reconcile": {
                    "latest_l1": _latest_run(
                        db, name, run_type="reconcile", deep=False),
                    "latest_deep": _latest_run(
                        db, name, run_type="reconcile", deep=True),
                },
                "sink": {
                    "type": scfg.sink.type,
                    "url_configured": bool(scfg.sink.url),
                    "token_configured": bool(
                        scfg.sink.token_env
                        and os.environ.get(scfg.sink.token_env)),
                    "timeout_seconds": scfg.sink.timeout_seconds,
                    "retries": scfg.sink.retries,
                    "ca_bundle_configured": bool(scfg.sink.ca_bundle),
                },
                "spool": {
                    "policy": scfg.spool.policy,
                    "directory_configured": bool(scfg.spool.directory),
                    "encrypted_at_rest": scfg.spool.encrypted_at_rest,
                },
                "probes": source_probes,
            })

        checks: list[dict] = []
        checks.append(_check(
            "supervision",
            "pass" if process["supervised"] else "fail",
            "launcher 监管状态正常" if process["supervised"] else "launcher 状态不可用或已过期",
            "使用受监管 launcher/开机任务启动中间机",
        ))
        checks.append(_check(
            "connector",
            "pass" if process["connector_running"] else "fail",
            "connector 正在运行" if process["connector_running"] else "connector 未运行",
            "检查 d2a-launcher.log 和 connector 日志",
        ))
        checks.append(_check(
            "maintenance_process",
            "pass" if process["maintenance_running"] else "fail",
            "maintenance 正在运行" if process["maintenance_running"] else "maintenance 未运行",
            "检查 launcher 进程状态与 maintenance 日志",
        ))
        checks.append(_check(
            "state_backup",
            "pass" if maintenance.get("integrity") == "ok" and not maintenance.get("overdue") else "fail",
            "状态库备份有效" if maintenance.get("integrity") == "ok" and not maintenance.get("overdue")
            else "尚无有效的近期状态库备份",
            "运行 maintenance 并检查备份目录和磁盘空间",
        ))
        maintenance_status = maintenance.get("status")
        checks.append(_check(
            "maintenance_last_attempt",
            "pass" if maintenance_status == "ok" else (
                "warning" if maintenance_status == "partial" else "fail"),
            "最近维护任务全部成功" if maintenance_status == "ok" else (
                "最近维护已生成备份，但部分清理步骤失败"
                if maintenance_status == "partial"
                else "最近维护任务失败"),
            "检查 maintenance 日志、目录权限和磁盘空间",
            required=maintenance_status != "partial",
        ))
        free_gb = maintenance.get("free_gb")
        min_free_gb = maintenance.get("min_free_gb")
        disk_known = free_gb is not None and min_free_gb is not None
        disk_ok = disk_known and float(free_gb) >= float(min_free_gb)
        checks.append(_check(
            "disk_space",
            "pass" if disk_ok else ("unknown" if not disk_known else "fail"),
            (
                f"状态库磁盘可用 {float(free_gb):.2f} GiB"
                if disk_known else "尚无状态库磁盘空间检查证据"
            ),
            "运行 maintenance；空间不足时先安全扩容或清理非业务文件",
            required=disk_known,
        ))
        checks.append(_check(
            "data_residency",
            "pass" if residency["compliant"] else "fail",
            "中间机无业务 Raw 持久化" if residency["compliant"]
            else ";".join(residency["violations"]),
            "生产环境使用 HTTP sink，移除 Raw 表并完成 spool 策略验收",
        ))
        checks.append(_check(
            "tables_configured",
            "pass" if sources and all(item["tables_configured"] for item in sources) else "fail",
            "所有源均已选择抽取表" if sources and all(item["tables_configured"] for item in sources)
            else "至少一个源尚未选择抽取表",
            "完成元数据扫描并保存抽取计划",
        ))
        autostart_required = cfg.deployment_mode == "production"
        checks.append(_check(
            "autostart",
            "pass" if autostart.get("installed") else (
                "fail" if autostart_required else "unknown"),
            "Windows 开机任务已安装" if autostart.get("installed")
            else "未检测到开机任务安装记录",
            "以管理员身份运行 install_middle_autostart.ps1",
            required=autostart_required,
        ))
        checks.append(_check(
            "config_revision",
            "pass" if current_revision else "unknown",
            "配置 revision 可用" if current_revision else "未提供配置文件路径，无法核对 revision",
            required=False,
        ))
        for source_status in sources:
            name = source_status["source"]
            latest_success = source_status.get("latest_success")
            source_probes = source_status.get("probes") or {}
            erp_probe = source_probes.get("erp") or {}
            platform_probe = source_probes.get("platform") or {}
            erp_proven = bool(
                (erp_probe.get("fresh") and erp_probe.get("ok"))
                or latest_success)
            platform_proven = bool(
                (platform_probe.get("fresh") and platform_probe.get("ok"))
                or (latest_success and latest_success.get("generation_id")))
            protocol_proven = bool(
                (platform_probe.get("fresh")
                 and platform_probe.get("ok")
                 and platform_probe.get("compatible"))
                or (latest_success and latest_success.get("generation_id")))
            checks.extend([
                _check(
                    f"erp_connection:{name}",
                    "pass" if erp_proven else "fail",
                    f"源 {name} ERP 连通已验证" if erp_proven
                    else f"源 {name} 尚无近期 ERP 连通证据",
                    "在配置页选择该 source 执行 ERP 连接测试",
                ),
                _check(
                    f"platform_connection:{name}",
                    "pass" if platform_proven else "fail",
                    f"源 {name} 平台连通已验证" if platform_proven
                    else f"源 {name} 尚无近期平台连通证据",
                    "在配置页选择该 source 执行平台连通测试",
                ),
                _check(
                    f"protocol_compatibility:{name}",
                    "pass" if protocol_proven else "fail",
                    f"源 {name} ingest 协议兼容" if protocol_proven
                    else f"源 {name} 尚无协议兼容证据",
                    "执行平台连通与协议检查；不兼容时升级中间机或平台",
                ),
            ])
        required_failures = [
            item for item in checks
            if item["required"] and item["status"] != "pass"
        ]
        def alert_links(check_id: str) -> list[str]:
            if check_id in ("supervision", "connector", "autostart"):
                return ["/status", "/logs?service=launcher"]
            if check_id in (
                "maintenance_process", "state_backup",
                "maintenance_last_attempt", "disk_space",
            ):
                return ["/status", "/logs?service=maintenance"]
            return ["/status", "/logs?service=connector"]

        alerts = [
            {
                "key": f"readiness:{item['id']}",
                "severity": "critical" if item["required"] else "warning",
                "category": "readiness",
                "title": item["detail"],
                "suggestion": item["suggestion"],
                "links": alert_links(item["id"]),
            }
            for item in required_failures
        ]
        alerts.extend({
            "key": f"advisory:{item['id']}",
            "severity": "warning",
            "category": "maintenance",
            "title": item["detail"],
            "suggestion": item["suggestion"],
            "links": alert_links(item["id"]),
        } for item in checks if not item["required"] and item["status"] == "warning")
        return {
            "observed_at": observed_at,
            "status_ttl_seconds": STATUS_TTL_SECONDS,
            "schedule_source": "derived_from_yaml",
            "config_revision": current_revision,
            "process_status": process,
            "autostart": autostart,
            "readiness_probes": probes,
            "maintenance": maintenance,
            "data_residency": residency,
            "readiness": {
                "ready": not required_failures,
                "checks": checks,
            },
            "alerts": alerts,
            "version": _version_info(),
            "sources": sources,
        }
    finally:
        db.con.close()
