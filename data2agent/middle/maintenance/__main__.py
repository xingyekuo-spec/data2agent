"""中间机长期运行维护：在线备份、历史清理、孤儿 staging 回收和磁盘预警。"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from ...shared.config import load_config
from ...shared.store.landing import LandingStore


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    pending.replace(path)


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
    try:
        cfg = load_config(config_path)
        landing_path = Path(cfg.landing)
        status_path = explicit_status or landing_path.parent / "run" / "maintenance-status.json"
        target_dir = Path(backup_dir) if backup_dir else landing_path.parent / "backups"
        landing_path.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(landing_path.parent).free
        if free < min_free_gb * 1024**3:
            raise RuntimeError(
                f"中间机磁盘可用空间不足 {free / 1024**3:.2f} GiB"
                f"(要求至少 {min_free_gb:g} GiB)")
        store = LandingStore(landing_path)
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = store.backup_to(target_dir / f"middle-{stamp}.sqlite")
            pruned = store.prune_operational_history(
                retention_days=retention_days, receipt_days=receipt_days)
            abandoned = store.cleanup_abandoned_staging(max_age_hours=24)
        finally:
            store.con.close()
        removed_backups = _prune_backups(target_dir, keep=keep_backups)
        result = {
            "backup": str(backup), "integrity": "ok", "pruned": pruned,
            "abandoned": abandoned, "removed_backups": removed_backups,
            "free_gb": round(free / 1024**3, 2),
        }
        _write_status(status_path, {
            "status": "ok", "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "result": result,
        })
        return result
    except Exception as exc:
        if status_path is not None:
            try:
                _write_status(status_path, {
                    "status": "failed", "started_at": started_at,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "error": f"{type(exc).__name__}: {exc}",
                })
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
        result = run_once(
            args.config, backup_dir=args.backup_dir,
            retention_days=args.retention_days, receipt_days=args.receipt_days,
            keep_backups=args.keep_backups, min_free_gb=args.min_free_gb,
            status_file=args.status_file,
        )
        print(result, flush=True)
        if args.status_file:
            path = Path(args.status_file)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["next_run_at"] = (
                    datetime.now() + timedelta(seconds=max(3600.0, args.every))
                ).isoformat(timespec="seconds")
                _write_status(path, payload)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        time.sleep(max(3600.0, args.every))


if __name__ == "__main__":
    raise SystemExit(main())
