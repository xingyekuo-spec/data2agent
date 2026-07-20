"""M5-T02 隔离 API 测试:列表筛选/分页/keys 解析、分组聚合/熔断状态/数据新鲜度。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.landing import LandingStore
from data2agent.console.app import create_app
from data2agent.console.contracts import (
    DEFAULT_BREAKER_THRESHOLD,
    QuarantineGroup,
    QuarantineRecord,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"

_NOW = datetime.now().isoformat(timespec="seconds")


def _insert_q(landing: LandingStore, source: str, object: str,
              keys_json: str | None, reason: str, batch_id: str | None = None) -> None:
    landing.con.execute(
        "INSERT INTO d2a_quarantine (source, object, keys_json, reason, "
        "created_at, batch_id) VALUES (?, ?, ?, ?, ?, ?)",
        (source, object, keys_json, reason, _NOW, batch_id))
    landing.con.commit()


@pytest.fixture()
def db(tmp_path):
    """Create a landing db with seeded quarantine records, an apply run, and object tables."""
    landing = LandingStore(tmp_path / "landing.sqlite")

    # Quarantine records across several groups
    _insert_q(landing, SOURCE, "SalesOrder", '{"order_id":"SO001"}', "bad type", "batch-1")
    _insert_q(landing, SOURCE, "SalesOrder", '{"order_id":"SO002"}', "missing field", "batch-1")
    _insert_q(landing, SOURCE, "Customer", '{"cust_id":"C001"}', "null key", "batch-2")
    _insert_q(landing, SOURCE, "Customer", '{"cust_id":"C002"}', "FK missing", "batch-2")
    _insert_q(landing, SOURCE, "Customer", '{"cust_id":"C003"}', "enum invalid", "batch-2")
    _insert_q(landing, SOURCE, "Material", '{"mat_id":"M001"}', "dup key", "batch-3")
    # Unknown source/object -- must appear in groups, not filtered out
    _insert_q(landing, "unknown_source", "UnknownObj", None, "unknown source error")
    # keys_json parse failures
    _insert_q(landing, SOURCE, "SalesOrder", "not-valid-json", "bad keys json", "batch-4")
    _insert_q(landing, SOURCE, "SalesOrder", '["array","not","object"]', "keys is array", "batch-4")

    # Apply run with structured steps for quarantine_rate computation
    run_id = landing.start_run(SOURCE, "apply")
    for i, (obj, rows_in, quarantined) in enumerate([
        ("SalesOrder",  100, 2),    # rate 2%
        ("Customer",     50, 3),    # rate 6% (tripped)
        ("Material",     30, 0),    # rate 0% (ok)
    ], start=1):
        step_id = landing.add_step(run_id, i, "object", obj)
        landing.update_step(
            step_id, status="ok",
            rows_in=rows_in, rows_out=rows_in - quarantined,
            quarantined=quarantined)
    landing.finish_run(run_id, tables=3, rows=180, status="ok")

    # Object tables for SalesOrder and Customer (Material is not materialized)
    for obj, rows in [("SalesOrder", 98), ("Customer", 47)]:
        landing.con.execute(
            f'CREATE TABLE IF NOT EXISTS "obj_{obj}" '
            f'(id INTEGER PRIMARY KEY, _d2a_mapped_at TEXT, _d2a_batch_id TEXT)')
        for j in range(rows):
            landing.con.execute(
                f'INSERT INTO "obj_{obj}" (id, _d2a_mapped_at, _d2a_batch_id) '
                f'VALUES (?, ?, ?)', (j + 1, "2026-07-10T12:00:00", "batch-1"))
    landing.con.commit()
    return landing


def _client(landing: LandingStore) -> TestClient:
    return TestClient(create_app(landing.db_path, str(ROOT / "templates")))


# ============================================================
# 列表端点测试
# ============================================================

class TestQuarantineList:
    """GET /api/quarantine -- 筛选/分页/契约/keys 解析。"""

    def test_array_shape_and_total_header(self, db):
        client = _client(db)
        r = client.get("/api/quarantine")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert r.headers["X-Total-Count"] == str(len(body))
        for row in body:
            QuarantineRecord.model_validate(row)

    def test_no_raw_field_in_response(self, db):
        client = _client(db)
        for row in client.get("/api/quarantine").json():
            assert "raw_json" not in row
            assert "raw" not in row

    def test_created_at_is_tz_aware(self, db):
        client = _client(db)
        row = client.get("/api/quarantine", params={"limit": 1}).json()[0]
        rec = QuarantineRecord.model_validate(row)
        assert rec.created_at.tzinfo is not None

    def test_age_seconds_computed(self, db):
        client = _client(db)
        row = client.get("/api/quarantine", params={"limit": 1}).json()[0]
        assert isinstance(row["age_seconds"], int)
        assert row["age_seconds"] >= 0

    # -- 分页与排序 --

    def test_default_limit_capped(self, db):
        client = _client(db)
        body = client.get("/api/quarantine").json()
        assert len(body) <= 50

    def test_limit_offset_pagination(self, db):
        client = _client(db)
        r1 = client.get("/api/quarantine", params={"limit": 1, "offset": 0})
        r2 = client.get("/api/quarantine", params={"limit": 1, "offset": 1})
        assert r1.json()[0]["id"] != r2.json()[0]["id"]
        assert int(r1.headers["X-Total-Count"]) == int(r2.headers["X-Total-Count"])

    def test_stable_sort_id_desc(self, db):
        client = _client(db)
        ids = [x["id"] for x in client.get("/api/quarantine").json()]
        assert ids == sorted(ids, reverse=True)

    def test_limit_max_100(self, db):
        client = _client(db)
        r = client.get("/api/quarantine", params={"limit": 100})
        assert r.status_code == 200

    def test_rejects_invalid_limit_offset(self, db):
        client = _client(db)
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            assert client.get("/api/quarantine", params=params).status_code == 422, params

    # -- 筛选 --

    def test_filter_by_source(self, db):
        client = _client(db)
        body = client.get("/api/quarantine", params={"source": SOURCE}).json()
        assert body and all(x["source"] == SOURCE for x in body)
        assert int(client.get("/api/quarantine",
                              params={"source": SOURCE}).headers["X-Total-Count"]) == len(body)

    def test_filter_by_object(self, db):
        client = _client(db)
        body = client.get("/api/quarantine", params={"object": "Customer"}).json()
        assert body and all(x["object"] == "Customer" for x in body)

    def test_filter_by_reason_contains(self, db):
        client = _client(db)
        body = client.get("/api/quarantine", params={"reason": "bad"}).json()
        assert body and all("bad" in x["reason"].lower() for x in body)

    def test_filter_combined(self, db):
        client = _client(db)
        body = client.get("/api/quarantine",
                          params={"source": SOURCE, "object": "SalesOrder"}).json()
        assert body and all(
            x["source"] == SOURCE and x["object"] == "SalesOrder" for x in body)

    def test_empty_result_nonexistent_filter(self, db):
        client = _client(db)
        r = client.get("/api/quarantine", params={"source": "nonexistent"})
        assert r.json() == []
        assert r.headers["X-Total-Count"] == "0"

    # -- keys_json 解析 --

    def test_keys_parse_success(self, db):
        client = _client(db)
        body = client.get("/api/quarantine", params={"object": "Customer"})
        for row in body.json():
            assert isinstance(row["keys"], dict)
            assert "cust_id" in row["keys"]
            assert row["warnings"] == []

    def test_keys_parse_failure_yields_null_with_warning(self, db):
        client = _client(db)
        body = client.get("/api/quarantine", params={"reason": "bad keys json"})
        row = body.json()[0]
        assert row["keys"] is None
        assert any("json" in w.lower() for w in row["warnings"])

    def test_keys_array_yields_null_with_warning(self, db):
        client = _client(db)
        body = client.get("/api/quarantine", params={"reason": "keys is array"})
        row = body.json()[0]
        assert row["keys"] is None
        assert any("不是 JSON 对象" in w for w in row["warnings"])

    def test_null_keys_json_yields_null_keys_no_warning(self, db):
        client = _client(db)
        body = client.get("/api/quarantine", params={"object": "UnknownObj"})
        row = body.json()[0]
        assert row["keys_json"] is None
        assert row["keys"] is None
        assert row["warnings"] == []


# ============================================================
# 分组端点测试
# ============================================================

class TestQuarantineGroups:
    """GET /api/quarantine/groups -- 聚合/状态/契约。"""

    def test_array_shape(self, db):
        client = _client(db)
        r = client.get("/api/quarantine/groups")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        for g in body:
            QuarantineGroup.model_validate(g)

    # -- 未知 source/object 不遗漏 --

    def test_unknown_source_appears(self, db):
        client = _client(db)
        sources = {g["source"] for g in client.get("/api/quarantine/groups").json()}
        assert "unknown_source" in sources

    def test_unknown_object_null_display_name(self, db):
        client = _client(db)
        unknown = next(g for g in client.get("/api/quarantine/groups").json()
                       if g["object"] == "UnknownObj")
        assert unknown["display_name"] is None
        assert unknown["pending"] == 1

    # -- 模板匹配 --

    def test_display_name_from_template(self, db):
        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        assert sales["display_name"] == "销售订单"

    def test_customer_display_name(self, db):
        client = _client(db)
        cust = next(g for g in client.get("/api/quarantine/groups").json()
                    if g["object"] == "Customer")
        assert cust["display_name"] == "客户"

    # -- 聚合计数与最新信息 --

    def test_pending_counts(self, db):
        client = _client(db)
        cust = next(g for g in client.get("/api/quarantine/groups").json()
                    if g["object"] == "Customer")
        assert cust["pending"] == 3

    def test_latest_batch_id_and_reason(self, db):
        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        assert sales["latest_batch_id"] == "batch-4"
        assert sales["latest_reason"] is not None

    def test_total_pending_matches_sum_of_groups(self, db):
        client = _client(db)
        groups = client.get("/api/quarantine/groups").json()
        total_from_groups = sum(g["pending"] for g in groups)
        # Total pending records should match the sum across groups
        list_body = client.get("/api/quarantine").json()
        assert int(client.get("/api/quarantine").headers["X-Total-Count"]) == total_from_groups

    def test_filter_by_source(self, db):
        client = _client(db)
        body = client.get("/api/quarantine/groups", params={"source": SOURCE}).json()
        assert body and all(g["source"] == SOURCE for g in body)

    # -- 隔离率 --

    def test_quarantine_rate_from_step(self, db):
        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        assert sales["quarantine_rate"] == pytest.approx(0.02, abs=0.001)

    def test_breaker_threshold_default(self, db):
        client = _client(db)
        for g in client.get("/api/quarantine/groups").json():
            assert g["breaker_threshold"] == DEFAULT_BREAKER_THRESHOLD

    # -- rate_state 转换 --

    def test_rate_state_ok_zero_rate(self, db):
        client = _client(db)
        mat = next(g for g in client.get("/api/quarantine/groups").json()
                   if g["object"] == "Material")
        assert mat["quarantine_rate"] == 0.0
        assert mat["rate_state"] == "ok"

    def test_rate_state_warning_below_threshold(self, db):
        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        assert sales["quarantine_rate"] == pytest.approx(0.02)
        assert sales["rate_state"] == "warning"

    def test_rate_state_tripped_at_or_above_threshold(self, db):
        client = _client(db)
        cust = next(g for g in client.get("/api/quarantine/groups").json()
                    if g["object"] == "Customer")
        assert cust["quarantine_rate"] == pytest.approx(0.06)
        assert cust["rate_state"] == "tripped"

    def test_rate_state_unknown_no_evidence(self, db):
        client = _client(db)
        unk = next(g for g in client.get("/api/quarantine/groups").json()
                   if g["object"] == "UnknownObj")
        assert unk["quarantine_rate"] is None
        assert unk["rate_state"] == "unknown"

    # -- serving_state 决策 --

    def test_serving_state_fresh(self, db):
        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        assert sales["serving_state"] == "fresh"

    def test_serving_state_not_materialized(self, db):
        client = _client(db)
        # Material has no obj_ table (never materialized)
        mat = next(g for g in client.get("/api/quarantine/groups").json()
                   if g["object"] == "Material")
        assert mat["serving_state"] == "not_materialized"

    def test_serving_state_not_materialized_for_unknown(self, db):
        client = _client(db)
        unk = next(g for g in client.get("/api/quarantine/groups").json()
                   if g["object"] == "UnknownObj")
        assert unk["serving_state"] == "not_materialized"

    def test_serving_state_stale_after_aborted_apply(self, db):
        """Aborted apply run 后对象 serving_state 应为 stale。"""
        # Add an aborted apply run with a step for SalesOrder
        run_id = db.start_run(SOURCE, "apply")
        step_id = db.add_step(run_id, 1, "object", "SalesOrder")
        db.update_step(
            step_id, status="aborted", rows_in=100, rows_out=98, quarantined=2)
        db.finish_run(run_id, tables=1, rows=100, status="ok",
                      detail="apply: breaker tripped")
        db.con.commit()

        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        # 最新 apply step 为 aborted → stale
        assert sales["serving_state"] == "stale"

    def test_serving_state_unavailable(self, db):
        """存在但结构不完整的对象表 → unavailable。"""
        db.con.execute('CREATE TABLE IF NOT EXISTS "obj_BrokenObj" (id INTEGER)')
        db.con.commit()
        _insert_q(db, SOURCE, "BrokenObj", None, "broken table obj")

        client = _client(db)
        broken = next(g for g in client.get("/api/quarantine/groups").json()
                      if g["object"] == "BrokenObj")
        # Table exists but missing _d2a_mapped_at column → read fails → unavailable
        assert broken["serving_state"] == "unavailable"

    # -- mapped_at / object_rows --

    def test_object_rows_and_mapped_at(self, db):
        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        assert sales["object_rows"] == 98
        assert sales["mapped_at"] is not None
        # Validate via pydantic model that mapped_at is tz-aware
        model = QuarantineGroup.model_validate(sales)
        assert model.mapped_at.tzinfo is not None

    def test_object_rows_null_for_unknown(self, db):
        client = _client(db)
        unk = next(g for g in client.get("/api/quarantine/groups").json()
                   if g["object"] == "UnknownObj")
        assert unk["object_rows"] is None
        assert unk["mapped_at"] is None

    # -- latest_apply_run_id --

    def test_latest_apply_run_id_set(self, db):
        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        assert isinstance(sales["latest_apply_run_id"], int)
        # 验证 run 存在
        run = db.con.execute(
            "SELECT id FROM d2a_sync_run WHERE id = ?",
            (sales["latest_apply_run_id"],)).fetchone()
        assert run is not None

    def test_latest_apply_run_id_null_no_steps(self, db):
        client = _client(db)
        unk = next(g for g in client.get("/api/quarantine/groups").json()
                   if g["object"] == "UnknownObj")
        assert unk["latest_apply_run_id"] is None
