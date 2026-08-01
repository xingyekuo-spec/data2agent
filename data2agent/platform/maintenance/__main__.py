"""平台落地库维护：先在线备份，再按显式保留期清理运行历史。"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path

from ...shared.store.landing import LandingStore


def _run_once(
    landing: str, backup_dir: str, retention_days: int, receipt_days: int,
) -> None:
    store = LandingStore(landing)
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path(backup_dir) / f"factory-{stamp}.sqlite"
        store.backup_to(target)
        counts = store.prune_operational_history(
            retention_days=retention_days, receipt_days=receipt_days)
        print(f"backup={target} integrity=ok pruned={counts}", flush=True)
    finally:
        store.con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="data2agent 平台 SQLite 维护")
    ap.add_argument("--landing", default="landing/factory.sqlite")
    ap.add_argument("--backup-dir", default="landing/backups")
    ap.add_argument("--retention-days", type=int, default=90)
    ap.add_argument("--receipt-days", type=int, default=365)
    ap.add_argument("--every", type=float, help="常驻模式间隔秒数")
    args = ap.parse_args()
    while True:
        _run_once(
            args.landing, args.backup_dir,
            args.retention_days, args.receipt_days)
        if args.every is None:
            return 0
        time.sleep(max(60.0, args.every))


if __name__ == "__main__":
    raise SystemExit(main())
