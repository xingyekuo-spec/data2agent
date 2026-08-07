"""完整 ingest generation 的 apply 租约与心跳。

所有会从 raw 构建/发布数据集的入口都应复用本模块，避免控制台、常驻
worker 各自实现一套屏障语义。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from .landing import LandingStore


@dataclass
class GenerationApplyLease:
    db_path: str
    source: str
    generation_id: str
    owner_id: str
    lease_seconds: float
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _lost: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def claim(
        cls, store: LandingStore, source: str, *,
        lease_seconds: float = 300.0,
    ) -> "GenerationApplyLease | None":
        owner_id = f"apply-{uuid.uuid4().hex}"
        generation_id = store.claim_committed_generation(
            source, owner_id=owner_id, lease_seconds=lease_seconds)
        if generation_id is None:
            return None
        lease = cls(
            db_path=store.db_path,
            source=source,
            generation_id=generation_id,
            owner_id=owner_id,
            lease_seconds=max(30.0, float(lease_seconds)),
        )
        lease._thread = threading.Thread(
            target=lease._heartbeat,
            name=f"d2a-generation-lease-{source}",
            daemon=True,
        )
        lease._thread.start()
        return lease

    @classmethod
    def claim_manual(
        cls, store: LandingStore, source: str, *,
        lease_seconds: float = 300.0,
    ) -> "GenerationApplyLease | None":
        """为控制台重试/手工构建领取互斥租约，不依赖新推送。"""
        owner_id = f"manual-apply-{uuid.uuid4().hex}"
        generation_id = store.claim_manual_generation_apply(
            source, owner_id=owner_id, lease_seconds=lease_seconds)
        if generation_id is None:
            return None
        lease = cls(
            db_path=store.db_path,
            source=source,
            generation_id=generation_id,
            owner_id=owner_id,
            lease_seconds=max(30.0, float(lease_seconds)),
        )
        lease._thread = threading.Thread(
            target=lease._heartbeat,
            name=f"d2a-manual-generation-lease-{source}",
            daemon=True,
        )
        lease._thread.start()
        return lease

    def _heartbeat(self) -> None:
        interval = max(10.0, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            store: LandingStore | None = None
            try:
                store = LandingStore.open_existing(self.db_path)
                if not store.renew_generation_apply_lease(
                    self.source, self.generation_id, self.owner_id,
                    lease_seconds=self.lease_seconds,
                ):
                    self._lost.set()
                    return
            except Exception:
                # 短暂 SQLite 写锁竞争不立即宣告租约丢失；下一心跳重试。
                continue
            finally:
                if store is not None:
                    store.con.close()

    def finish(self, store: LandingStore, *, success: bool) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._lost.is_set():
            raise RuntimeError(
                f"{self.source}: generation {self.generation_id} apply 租约已丢失")
        store.finish_generation_apply(
            self.source,
            self.generation_id,
            success=success,
            owner_id=self.owner_id,
        )
