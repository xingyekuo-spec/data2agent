"""中间机长期运行维护：在线备份、历史清理、孤儿 staging 回收和磁盘预警。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from ...shared.config import load_config
from ...shared.store.landing import LandingStore


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, pending_name = tempfile.mkstemp(
        prefix=".maintenance-status-", suffix=".tmp", dir=str(path.parent))
    pending = Path(pending_name)
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
    except Exception:
        pending.unlink(missing_ok=True)
        raise


def _read_status(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _prune_backups(backup_dir: Path, *, keep: int) -> int:
    files = sorted(
        backup_dir.glob("middle-*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in files[max(1, keep):]:
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def run_once(
    config_path: str | Path, *, backup_dir: str | Path | None = None,
    retention_days: int = 90, receipt_days: int = 365,
    keep_backups: int = 14, min_free_gb: float = 2.0,
    status_file: str | Path | None = None,
) -> dict:
    started_at = datetime.now().isoformat(timespec="seconds")
    explicit_status = Path(status_file) if status_file else None
    status_path = explicit_status
    previous: dict = _read_status(status_path)
    landing_path: Path | None = None
    free: int | None = None
    try:
        cfg = load_config(config_path)
        landing_path = Path(cfg.state_db)
        status_path = explicit_status or landing_path.parent / "run" / "maintenance-status.json"
        previous = _read_status(status_path)
        target_dir = Path(backup_dir) if backup_dir else landing_path.parent / "backups"
        landing_path.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(landing_path.parent).free
        if free < min_free_gb * 1024**3:
            raise RuntimeError(
                f"中间机磁盘可用空间不足 {free / 1024**3:.2f} GiB"
                f"(要求至少 {min_free_gb:g} GiB)")
        store = LandingStore(landing_path)
        maintenance_errors: list[dict[str, str]] = []
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = store.backup_to(target_dir / f"middle-state-{stamp}.sqlite")
            try:
                pruned = store.prune_operational_history(
                    retention_days=retention_days, receipt_days=receipt_days)
            except Exception as exc:
                pruned = {}
                maintenance_errors.append({
                    "step": "history_prune",
                    "error": f"{type(exc).__name__}: {exc}",
                })
            try:
                abandoned = store.cleanup_abandoned_staging(max_age_hours=24)
            except Exception as exc:
                abandoned = {}
                maintenance_errors.append({
                    "step": "staging_cleanup",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        finally:
            store.con.close()
        try:
            removed_backups = _prune_backups(target_dir, keep=keep_backups)
        except Exception as exc:
            removed_backups = 0
            maintenance_errors.append({
                "step": "backup_retention",
                "error": f"{type(exc).__name__}: {exc}",
            })
        finished_at = datetime.now().isoformat(timespec="seconds")
        result = {
            "backup": str(backup),
            "backup_file": backup.name,
            "backup_size_bytes": backup.stat().st_size,
            "backup_kind": "middle_state",
            "integrity": "ok", "pruned": pruned,
            "abandoned": abandoned, "removed_backups": removed_backups,
            "free_gb": round(free / 1024**3, 2),
            "min_free_gb": min_free_gb,
            "errors": maintenance_errors,
        }
        attempt_status = "partial" if maintenance_errors else "ok"
        payload = {
            "schema_version": 2,
            "status": attempt_status,
            "started_at": started_at,
            "finished_at": finished_at,
            "last_attempt": {
                "status": attempt_status,
                "started_at": started_at,
                "finished_at": finished_at,
                "errors": maintenance_errors,
            },
            # last_success 专指最近一次成功生成并通过 integrity_check 的状态库备份。
            "last_success": {
                "started_at": started_at,
                "finished_at": finished_at,
                "backup_file": backup.name,
                "backup_size_bytes": backup.stat().st_size,
                "backup_kind": "middle_state",
                "integrity": "ok",
            },
            "result": result,
        }
        if previous.get("next_run_at"):
            payload["next_run_at"] = previous["next_run_at"]
        _write_status(status_path, payload)
        return result
    except Exception as exc:
        if status_path is not None:
            try:
                finished_at = datetime.now().isoformat(timespec="seconds")
                payload = {
                    "schema_version": 2,
                    "status": "failed", "started_at": started_at,
                    "finished_at": finished_at,
                    "error": f"{type(exc).__name__}: {exc}",
                    "last_attempt": {
                        "status": "failed", "started_at": started_at,
                        "finished_at": finished_at,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    "result": {
                        "backup_kind": "middle_state",
                        "free_gb": (
                            round(free / 1024**3, 2)
                            if free is not None else None),
                        "min_free_gb": min_free_gb,
                        "errors": [{
                            "step": "maintenance",
                            "error": f"{type(exc).__name__}: {exc}",
                        }],
                    },
                }
                if previous.get("last_success"):
                    payload["last_success"] = previous["last_success"]
                elif previous.get("status") == "ok" and previous.get("result"):
                    old = previous["result"]
                    payload["last_success"] = {
                        "started_at": previous.get("started_at"),
                        "finished_at": previous.get("finished_at"),
                        "backup_file": Path(old.get("backup", "")).name,
                        "backup_size_bytes": old.get("backup_size_bytes"),
                        "backup_kind": "middle_state",
                        "integrity": old.get("integrity"),
                    }
                if previous.get("next_run_at"):
                    payload["next_run_at"] = previous["next_run_at"]
                _write_status(status_path, payload)
            except OSError:
                pass
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent 中间机 SQLite 维护")
    ap.add_argument("--config", required=True)
    ap.add_argument("--backup-dir")
    ap.add_argument("--retention-days", type=int, default=90)
    ap.add_argument("--receipt-days", type=int, default=365)
    ap.add_argument("--keep-backups", type=int, default=14)
    ap.add_argument("--min-free-gb", type=float, default=2.0)
    ap.add_argument("--every", type=float, default=86400)
    ap.add_argument("--status-file")
    args = ap.parse_args()
    while True:
        try:
            result = run_once(
                args.config, backup_dir=args.backup_dir,
                retention_days=args.retention_days, receipt_days=args.receipt_days,
                keep_backups=args.keep_backups, min_free_gb=args.min_free_gb,
                status_file=args.status_file,
            )
            print(result, flush=True)
        except Exception as exc:
            # 单次备份/清理失败不能让长期 maintenance 退出并触发重启风暴；
            # 失败已经原子记录到状态文件，下一周期继续尝试。
            print(f"maintenance failed:{type(exc).__name__}: {exc}", flush=True)
        delay = max(3600.0, args.every)
        if args.status_file:
            path = Path(args.status_file)
            try:
                payload = _read_status(path)
                payload["next_run_at"] = (
                    datetime.now() + timedelta(seconds=delay)
                ).isoformat(timespec="seconds")
                _write_status(path, payload)
            except OSError:
                pass
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
