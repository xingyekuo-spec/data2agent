"""ingest FastAPI 应用:POST /ingest/batch → 幂等落地。

安全:可选 Bearer Token(--token / D2A_INGEST_TOKEN);无 Token 为开放(仅内网可信段)。
落地复用 connect.landing.upsert_rows(按业务键 upsert,重推安全)。
每请求自建 LandingStore(sqlite 连接不跨线程复用)。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from ..connect.adapters.base import TableInfo
from ..connect.landing import LandingStore, raw_table_name


class BatchBody(BaseModel):
    source: str
    table: str
    columns: list[list[str]]   # [[列名, 可移植类型], ...]
    pk: list[str]
    batch_id: str
    rows: list[dict]


def create_app(landing_path: str | Path, token: str | None = None) -> FastAPI:
    def auth(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if supplied != token:
            raise HTTPException(401, "需要有效 Token(Authorization: Bearer <token>)")

    app = FastAPI(title="data2agent ingest")

    @app.get("/ingest/health")
    def health() -> dict:
        return {"ok": True, "landing": str(landing_path)}

    @app.post("/ingest/batch", dependencies=[Depends(auth)])
    def ingest_batch(body: BatchBody) -> dict:
        if not body.pk:
            raise HTTPException(422, f"{body.table}: 缺主键,无法幂等落地")
        bad = [c for c in body.columns if len(c) != 2]
        if bad:
            raise HTTPException(422, f"columns 须为 [列名, 类型] 二元组,got {bad[:3]}")
        info = TableInfo(name=body.table,
                         columns=[(c[0], c[1]) for c in body.columns], pk=list(body.pk))
        landing = LandingStore(landing_path)
        landing.ensure_raw_table(body.source, info)
        n = landing.upsert_rows(body.source, info, body.rows, body.batch_id)
        landing.log_audit(body.source, "ingest",
                          f"batch {body.batch_id} → {raw_table_name(body.source, body.table)}",
                          n, 0.0, body.batch_id)
        return {"ingested": n, "table": body.table, "batch_id": body.batch_id}

    return app
