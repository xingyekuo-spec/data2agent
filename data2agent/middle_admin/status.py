"""调度状态推算:由 connect.yaml + 时钟 + 落地库水位推导,非 APScheduler 反射。"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..connect.config import ConnectConfig, SourceConfig, in_window, parse_window
from ..connect.landing import LandingStore


def _next_window_start(now: datetime, windows: list[str]) -> datetime | None:
    """窗外时返回最近未来窗口起点;无窗口限制则 None。"""
    if not windows:
        return None
    today = now.date()
    candidates: list[datetime] = []
    for raw in windows:
        start, _ = parse_window(raw)
        for day_offset in (0, 1):
            dt = datetime.combine(today + timedelta(days=day_offset), start)
            if dt > now:
                candidates.append(dt)
    return min(candidates) if candidates else None


def _last_run_at(db: LandingStore, source: str) -> datetime | None:
    row = db.con.execute(
        "SELECT MAX(started_at) AS t FROM d2a_sync_run WHERE source = ?",
        (source,)).fetchone()
    if row and row["t"]:
        return datetime.fromisoformat(row["t"])
    row = db.con.execute(
        "SELECT MAX(last_run_at) AS t FROM d2a_sync_state WHERE source = ?",
        (source,)).fetchone()
    if row and row["t"]:
        return datetime.fromisoformat(row["t"])
    return None


def _estimate_next_sync(now: datetime, scfg: SourceConfig, last_run: datetime | None) -> datetime:
    if not in_window(now.time(), scfg.windows):
        nxt = _next_window_start(now, scfg.windows)
        return nxt if nxt is not None else now
    if last_run is None:
        return now
    return last_run + timedelta(seconds=scfg.sync_every_seconds())


def _latest_run(db: LandingStore, source: str) -> dict | None:
    row = db.con.execute(
        "SELECT id, source, status, started_at, finished_at, tables, rows, detail "
        "FROM d2a_sync_run WHERE source = ? AND run_type = 'sync' "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (source,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "source": row["source"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "tables": row["tables"],
        "rows": row["rows"],
        "detail": row["detail"],
    }


def _running_run(db: LandingStore, source: str) -> dict | None:
    row = db.con.execute(
        "SELECT id, source, status, started_at, tables, rows "
        "FROM d2a_sync_run WHERE source = ? AND run_type = 'sync' "
        "AND status = 'running' ORDER BY id DESC LIMIT 1",
        (source,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "source": row["source"],
        "status": row["status"],
        "started_at": row["started_at"],
        "tables": row["tables"],
        "rows": row["rows"],
    }


def build_status(cfg: ConnectConfig, now: datetime | None = None) -> dict:
    """返回 /api/status JSON 体。"""
    now = now or datetime.now()
    db = LandingStore(cfg.landing)
    sources = []
    for name, scfg in cfg.sources.items():
        last_run = _last_run_at(db, name)
        watermarks = db.list_sync_watermarks(name)
        tables_configured = len(scfg.table_whitelist()) > 0
        latest = _latest_run(db, name)
        running = _running_run(db, name)
        sources.append({
            "source": name,
            "in_window": in_window(now.time(), scfg.windows),
            "windows": scfg.windows,
            "sync_every": scfg.sync_every,
            "last_run_at": last_run.isoformat() if last_run else None,
            "next_sync_at": _estimate_next_sync(now, scfg, last_run).isoformat(),
            "watermarks": watermarks,
            "tables_configured": tables_configured,
            "latest_run": latest,
            "running_run": running,
        })
    return {"schedule_source": "derived_from_yaml", "sources": sources}
