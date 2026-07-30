"""跨进程 source 级同步锁:connector 常驻调度与管理页手动触发共用。

- 锁文件: <landing 父目录>/locks/sync-<safe_source_name>.lock
- Windows: msvcrt.locking, Linux/macOS: fcntl.flock — 非阻塞独占锁
- 进程崩溃时由 OS 自动释放; 获取失败不排队
"""

from __future__ import annotations

import os
import re
import sys


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_source(name: str) -> str:
    return _SAFE_RE.sub("_", name)


def _lock_dir(landing_path: str) -> str:
    parent = os.path.dirname(os.path.abspath(landing_path))
    return os.path.join(parent, "locks")


class SourceSyncLock:
    """source 级非阻塞文件锁。

    用法:
        lock = SourceSyncLock.try_acquire(landing, source)
        if lock is None:
            ...  // 已有其他进程在同步该 source
        try:
            ...
        finally:
            lock.release()
    """

    def __init__(self, lock_path: str, fd: int) -> None:
        self._path = lock_path
        self._fd = fd
        self._held = True

    @classmethod
    def try_acquire(cls, landing_path: str, source: str) -> SourceSyncLock | None:
        locks = _lock_dir(landing_path)
        os.makedirs(locks, exist_ok=True)
        safe = _safe_source(source)
        lock_path = os.path.join(locks, f"sync-{safe}.lock")
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return None
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            os.close(fd)
            return None
        return cls(lock_path, fd)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass

    @classmethod
    def find_running_run(cls, landing_path: str, source: str) -> int | None:
        """查询该 source 最近一个 status='running' 的 run_id。"""
        from ...shared.store.landing import LandingStore
        db = LandingStore(landing_path)
        try:
            row = db.con.execute(
                "SELECT id FROM d2a_sync_run WHERE source = ? AND status = 'running' "
                "AND run_type = 'sync' ORDER BY id DESC LIMIT 1",
                (source,),
            ).fetchone()
            return row["id"] if row else None
        finally:
            db.con.close()
