"""M5-T02 隔离 API 测试:列表筛选/分页/keys 解析、分组聚合/熔断状态/数据新鲜度。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.landing import LandingStore
from data2agent.console.app import _compute_rate_state, _compute_serving_state, create_app
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
    # Raw tables for SalesOrder's binding tables (SALES_ORDER, CURRENCY)
    # needed for has_raw_evidence check in serving_state
    for bt in ["SALES_ORDER", "CURRENCY"]:
        landing.con.execute(
            f'CREATE TABLE IF NOT EXISTS "raw_digiwin_e10__{bt}" '
            f'(_d2a_extracted_at TEXT)')
        landing.con.execute(
            f'INSERT INTO "raw_digiwin_e10__{bt}" (_d2a_extracted_at) '
            f'VALUES (?)', ("2026-07-09T12:00:00",))  # older than mapped_at
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

    # -- 服务端脱敏:keys / reason(M5 P1 fix) --

    def test_unknown_object_keys_are_fully_masked_in_list(self, db):
        """未知对象(模板中不存在)→ keys 全 mask 为 ***,keys_json 与 keys 一致。"""
        # 创建一条 keys_json 不为 null 的未知对象记录
        landing = LandingStore(db.db_path)
        landing.con.execute(
            "INSERT INTO d2a_quarantine (source, object, keys_json, reason, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("unknown_source", "AnotherUnknown", '{"secret_key":"top-secret"}',
             "unknown object test", _NOW))
        landing.con.commit()

        client = _client(db)
        body = client.get("/api/quarantine", params={"object": "AnotherUnknown"})
        row = body.json()[0]
        assert row["keys"] is not None
        for v in row["keys"].values():
            assert v == "***", f"未知对象 keys 应全 mask, got {v!r}"
        # keys_json 应重序列化为 mask 后的一致值
        assert row["keys_json"] is not None
        roundtrip = json.loads(row["keys_json"])
        assert roundtrip == row["keys"]

    def test_reason_is_sanitized_in_list(self, db):
        """列表 reason 经过 safe_error_summary 处理,不含换行。"""
        client = _client(db)
        body = client.get("/api/quarantine", params={"object": "Customer"})
        for row in body.json():
            if row["reason"] is None:
                continue
            assert isinstance(row["reason"], str)
            assert "\n" not in row["reason"], f"reason 不应含换行: {row['reason']!r}"

    def test_reason_sanitized_when_empty(self, db):
        """空/纯空白 reason 经 safe_error_summary 压缩后返回空字符串,不泄露原值。"""
        # reason 列 NOT NULL,空字符串会被 safe_error_summary 压缩,改用空串兜底
        landing = LandingStore(db.db_path)
        landing.con.execute(
            "INSERT INTO d2a_quarantine (source, object, keys_json, reason, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (SOURCE, "Customer", '{"customer_code":"C_SANITIZED_REASON"}', "   ", _NOW))
        landing.con.commit()
        client = _client(db)
        body = client.get("/api/quarantine", params={"object": "Customer"})
        for row in body.json():
            if row["keys"] and row["keys"].get("customer_code") == "C_SANITIZED_REASON":
                assert row["reason"] == ""


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

    # -- Issue 3: quarantine_rate 按源隔离 --

    def test_quarantine_rate_source_isolated(self, db):
        """两个源映射同一对象时各取自的最近 apply step,不交叉。"""
        landing = LandingStore(db.db_path)
        # 注册第二个源
        landing.con.execute(
            "CREATE TABLE IF NOT EXISTS raw_other_source__SALES_ORDER "
            "(_d2a_extracted_at TEXT)")
        # 为 other_source 创建 apply run(不同隔离率)
        run2 = landing.start_run("other_source", "apply")
        s2 = landing.add_step(run2, 1, "object", "SalesOrder")
        landing.update_step(s2, status="ok", rows_in=200, rows_out=190,
                           quarantined=10)  # rate 5%
        landing.finish_run(run2, tables=1, rows=200, status="ok")
        landing.con.commit()

        client = _client(db)
        # SOURCE (digiwin_e10) 的 SalesOrder rate 仍为 2%
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        # 应在原源(SalesOrder 在 digiwin_e10 的 rate=2/100=0.02)
        assert sales["quarantine_rate"] == pytest.approx(0.02, abs=0.001)

    # -- Issue 4: serving_state 仅扫描 binding 表,与其他源隔离 --

    def test_serving_state_only_checks_binding_tables(self, db):
        """只有 binding 内的 raw 表才影响 serving_state,无关表不影响。"""
        landing = LandingStore(db.db_path)
        # SalesOrder 的 binding tables:[SALES_ORDER, CURRENCY]
        # 创建不匹配 binding 的 raw 表(更新于 mapped_at 之后)
        landing.con.execute(
            'CREATE TABLE IF NOT EXISTS "raw_digiwin_e10__UNRELATED" '
            '(_d2a_extracted_at TEXT)')
        landing.con.execute(
            'INSERT INTO "raw_digiwin_e10__UNRELATED" (_d2a_extracted_at) '
            'VALUES (?)', ("2026-07-11T12:00:00",))  # newer than mapped_at
        landing.con.commit()

        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        # UNRELATED 不在 binding tables 中,SalesOrder 仍为 fresh
        assert sales["serving_state"] == "fresh"

    def test_serving_state_unknown_without_binding_tables(self, db):
        """无 binding 表证据时即使 apply run ok 也不判 fresh。"""
        landing = LandingStore(db.db_path)
        # unknown_source 无模板匹配,故 binding_tables=None
        # 但存在对象表且有 mapped_at
        landing.con.execute(
            'CREATE TABLE IF NOT EXISTS "obj_UnknownObj" '
            '(id INTEGER PRIMARY KEY, _d2a_mapped_at TEXT)')
        landing.con.execute(
            'INSERT INTO "obj_UnknownObj" (id, _d2a_mapped_at) '
            'VALUES (?, ?)', (1, "2026-07-10T12:00:00"))
        landing.con.commit()

        client = _client(db)
        unk = next(g for g in client.get("/api/quarantine/groups").json()
                   if g["object"] == "UnknownObj")
        # binding_tables=None → 无法核实 raw 证据 → unknown
        assert unk["serving_state"] == "unknown"


# ============================================================
# 决策矩阵表驱动测试 (M5-T04)
# ============================================================

_MAPPED_AT = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
_RAW_NEWER = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
_RAW_OLDER = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


class TestRateStateDecisionMatrix:
    """_compute_rate_state 表驱动测试。"""

    @pytest.mark.parametrize("rate,threshold,expected", [
        (None, 0.05, "unknown"),
        (0.0, 0.05, "ok"),
        (0.01, 0.05, "warning"),
        (0.049, 0.05, "warning"),
        (0.05, 0.05, "tripped"),
        (0.10, 0.05, "tripped"),
        (0.0, 0.10, "ok"),
        (0.05, 0.10, "warning"),
        (0.10, 0.10, "tripped"),
    ])
    def test_decision_matrix(self, rate, threshold, expected):
        assert _compute_rate_state(rate, threshold) == expected


class TestServingStateDecisionMatrix:
    """_compute_serving_state 表驱动测试:验证 §6.3 优先级互斥。"""

    @pytest.mark.parametrize(
        "scenario,expected,table_exists,table_ok,object_rows,mapped_at,"
        "step_aborted,raw_ts,apply_run_status", [
            # P1: not_materialized — 表不存在,优先级最高
            ("not_materialized",
             "not_materialized", False, False, None, None, False, None, None),
            ("not_materialized beats step_aborted",
             "not_materialized", False, False, None, _MAPPED_AT, True, None, "ok"),
            ("not_materialized beats raw_newer",
             "not_materialized", False, False, None, _MAPPED_AT, False, _RAW_NEWER, "ok"),
            # P2: unavailable — 表存在但不可读
            ("unavailable (table_ok=False)",
             "unavailable", True, False, None, None, False, None, None),
            ("unavailable (object_rows=None even if table_ok=True)",
             "unavailable", True, True, None, _MAPPED_AT, False, None, None),
            ("unavailable beats step_aborted",
             "unavailable", True, False, None, _MAPPED_AT, True, None, None),
            ("unavailable beats raw_newer",
             "unavailable", True, True, None, _MAPPED_AT, False, _RAW_NEWER, None),
            # P3: stale
            ("stale via aborted step",
             "stale", True, True, 100, _MAPPED_AT, True, None, None),
            ("stale via raw newer than mapped_at",
             "stale", True, True, 100, _MAPPED_AT, False, _RAW_NEWER, None),
            # P4: fresh — 一切正常(需要 raw 证据确认不新于 mapped_at)
            ("fresh",
             "fresh", True, True, 100, _MAPPED_AT, False, _RAW_OLDER, "ok"),
            # P5: unknown — 无充分证据
            ("unknown (no apply run, no raw)",
             "unknown", True, True, 100, _MAPPED_AT, False, None, None),
            ("unknown (apply run exists but not ok)",
             "unknown", True, True, 100, _MAPPED_AT, False, None, "aborted"),
            ("unknown (no mapped_at, has apply run)",
             "unknown", True, True, 100, None, False, None, "ok"),
        ])
    def test_decision_matrix(
        self, tmp_path, scenario, expected, table_exists, table_ok,
        object_rows, mapped_at, step_aborted, raw_ts, apply_run_status,
    ):
        """穿透 _compute_serving_state 直接验证决策矩阵优先级。"""
        db = LandingStore(tmp_path / "serving.sqlite")

        # 按需创建 raw_* 表
        if raw_ts is not None:
            db.con.execute(
                'CREATE TABLE IF NOT EXISTS "raw_digiwin_e10__test" '
                '(_d2a_extracted_at TEXT)')
            db.con.execute(
                'INSERT INTO "raw_digiwin_e10__test" (_d2a_extracted_at) '
                'VALUES (?)', (raw_ts.isoformat(),))
            db.con.commit()

        # 按需创建 apply run
        run_id = None
        if apply_run_status is not None:
            run_id = db.start_run(SOURCE, "apply")
            db.finish_run(run_id, tables=1, rows=100, status=apply_run_status)
            db.con.commit()

        # 提供 binding_tables 以匹配 raw 表验证
        bt = ["test"] if raw_ts is not None or (
            expected == "fresh") else None
        result = _compute_serving_state(
            db, table_exists, table_ok, object_rows, mapped_at,
            SOURCE, run_id, step_aborted, binding_tables=bt,
        )
        assert result == expected, (
            f"{scenario}: expected={expected}, got={result}"
        )


class TestServingStateStaleByRawTimestamp:
    """stale 由 raw 时间戳触发(M5-T04 追加缺失用例)。"""

    def test_stale_via_raw_newer_than_mapped(self, db):
        """创建 raw_ 表,时间晚于 obj 的 mapped_at,验证 serving_state=stale。"""
        # 对现有的 SalesOrder (mapped_at=2026-07-10T12:00:00) 创建更新的 raw 数据
        # 表名必须匹配 binding 中的表名(SalesOrder 的 binding tables: [SALES_ORDER, CURRENCY])
        db.con.execute(
            'CREATE TABLE IF NOT EXISTS "raw_digiwin_e10__SALES_ORDER" '
            '(_d2a_extracted_at TEXT)')
        db.con.execute(
            'INSERT INTO "raw_digiwin_e10__SALES_ORDER" (_d2a_extracted_at) '
            'VALUES (?)', ("2026-07-11T12:00:00",))
        db.con.commit()

        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        # raw 更新于 mapped_at → stale
        assert sales["serving_state"] == "stale"

    def test_stale_not_triggered_when_raw_older(self, db):
        """raw 时间早于 mapped_at 时不触发 stale — 所有 binding 表都需有证据才能判 fresh。"""
        # SalesOrder binding tables: [SALES_ORDER, CURRENCY] — 两个表都有 timestamp 才能判 fresh
        for tbl in ["SALES_ORDER", "CURRENCY"]:
            db.con.execute(
                f'CREATE TABLE IF NOT EXISTS "raw_digiwin_e10__{tbl}" '
                '(_d2a_extracted_at TEXT)')
            db.con.execute(
                f'INSERT INTO "raw_digiwin_e10__{tbl}" (_d2a_extracted_at) '
                'VALUES (?)', ("2026-07-09T12:00:00",))
        db.con.commit()

        client = _client(db)
        sales = next(g for g in client.get("/api/quarantine/groups").json()
                     if g["object"] == "SalesOrder")
        # raw 比 mapped_at 旧且 apply 成功且所有 binding 表有证据 → 仍为 fresh
        assert sales["serving_state"] == "fresh"


# ============================================================
# Issue 2 [P1]: keys_json 解析失败不泄露原始值 (list endpoint)
# ============================================================

class TestKeysJsonSanitizationList:
    """列表端点 keys_json 解析失败/非 dict 时 keys_json_out 应为 null。"""

    @pytest.fixture()
    def db(self, tmp_path):
        landing = LandingStore(tmp_path / "landing.sqlite")
        # 创建解析失败的记录
        landing.con.execute(
            "INSERT INTO d2a_quarantine (source, object, keys_json, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (SOURCE, "SalesOrder", "malformed-secret@example.com-CUSTOMER-007",
             "bad json", datetime.now().isoformat(timespec="seconds")))
        # 创建非 dict 的记录
        landing.con.execute(
            "INSERT INTO d2a_quarantine (source, object, keys_json, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (SOURCE, "SalesOrder", '["array","not","object"]',
             "array keys", datetime.now().isoformat(timespec="seconds")))
        landing.con.commit()
        return landing

    def test_malformed_keys_json_yields_null(self, db):
        """解析失败的 keys_json → keys 为 None, keys_json_out 为 null。"""
        client = _client(db)
        body = client.get("/api/quarantine", params={"reason": "bad json"}).json()
        row = body[0]
        assert row["keys"] is None
        assert row["keys_json"] is None
        assert any("json" in w.lower() for w in row["warnings"])

    def test_array_keys_json_yields_null(self, db):
        """非 dict 的 keys_json → keys 为 None, keys_json_out 为 null。"""
        client = _client(db)
        body = client.get("/api/quarantine", params={"reason": "array keys"}).json()
        row = body[0]
        assert row["keys"] is None
        assert row["keys_json"] is None
        assert any("不是 JSON 对象" in w for w in row["warnings"])

    def test_malformed_keys_json_not_leaking_raw(self, db):
        """解析失败的原始敏感字符串不应出现在响应中。"""
        client = _client(db)
        body = client.get("/api/quarantine", params={"reason": "bad json"}).json()
        row = body[0]
        # keys_json 不应包含原始敏感值
        assert row["keys_json"] is None
        assert row["keys"] is None
        # 原始敏感字符串不应泄露在任何字段中
        row_str = json.dumps(row, ensure_ascii=False)
        assert "malformed-secret" not in row_str
        assert "CUSTOMER-007" not in row_str


# ============================================================
# Issue 3 [P1]: 多表 serving state 证据不完整不能判 fresh
# ============================================================

class TestServingStatePartialEvidence:
    """逐表跟踪证据:部分表缺失 → 不能判 fresh。"""

    def test_partial_evidence_not_fresh(self, tmp_path):
        """两个 binding 表,仅一个有时戳 → fresh 条件不满足,返回 unknown。"""
        db = LandingStore(tmp_path / "serving.sqlite")
        # 创建一个 raw 表有时戳,另一个没有
        db.con.execute(
            'CREATE TABLE IF NOT EXISTS "raw_digiwin_e10__A" '
            '(_d2a_extracted_at TEXT)')
        db.con.execute(
            'INSERT INTO "raw_digiwin_e10__A" (_d2a_extracted_at) '
            'VALUES (?)', ("2026-07-09T12:00:00",))
        db.con.commit()

        run_id = db.start_run(SOURCE, "apply")
        db.finish_run(run_id, tables=1, rows=100, status="ok")
        db.con.commit()

        mapped_at = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
        # binding_tables = ["A", "B"] — B 不存在,应视为缺失证据
        result = _compute_serving_state(
            db, table_exists=True, table_ok=True, object_rows=100,
            mapped_at=mapped_at, source=SOURCE,
            latest_apply_run_id=run_id, step_aborted=False,
            binding_tables=["A", "B"],
        )
        # 仅 A 有证据,B 缺失 → 不能判 fresh,应退为 unknown
        assert result == "unknown", f"expected unknown, got {result}"

    def test_all_tables_with_evidence_still_fresh(self, tmp_path):
        """两个 binding 表都有旧于 mapped_at 的时戳 → 仍可判 fresh。"""
        db = LandingStore(tmp_path / "serving.sqlite")
        for tbl in ["A", "B"]:
            db.con.execute(
                f'CREATE TABLE IF NOT EXISTS "raw_digiwin_e10__{tbl}" '
                '(_d2a_extracted_at TEXT)')
            db.con.execute(
                f'INSERT INTO "raw_digiwin_e10__{tbl}" (_d2a_extracted_at) '
                'VALUES (?)', ("2026-07-09T12:00:00",))
        db.con.commit()

        run_id = db.start_run(SOURCE, "apply")
        db.finish_run(run_id, tables=1, rows=100, status="ok")
        db.con.commit()

        mapped_at = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
        result = _compute_serving_state(
            db, table_exists=True, table_ok=True, object_rows=100,
            mapped_at=mapped_at, source=SOURCE,
            latest_apply_run_id=run_id, step_aborted=False,
            binding_tables=["A", "B"],
        )
        assert result == "fresh", f"expected fresh, got {result}"


# ============================================================
# Issue 4 [P2]: 未知对象禁用重试 (quarantine/groups)
# ============================================================

class TestUnknownObjectRetryGating:
    """模板未识别的对象 → retry_allowed=False。"""

    @pytest.fixture()
    def db_with_config(self, tmp_path):
        """提供 config 的 fixture,避免 '只读模式' 先触发。"""
        from data2agent.connect.config import ConnectConfig, SourceConfig
        landing = LandingStore(tmp_path / "landing.sqlite")
        cfg = ConnectConfig(
            templates=str(ROOT / "templates"),
            landing=landing.db_path,
            sources={"digiwin_e10": SourceConfig(adapter="sqlite_readonly", path=":memory:")},
        )
        return landing, cfg

    def test_unknown_object_retry_disallowed(self, db_with_config):
        """模板中没有的对象在 groups 中 retry_allowed=False。"""
        landing, cfg = db_with_config
        _insert_q(landing, SOURCE, "NotInTemplate", '{"k":"v"}', "some reason")

        client = TestClient(create_app(landing.db_path, str(ROOT / "templates"), config=cfg))
        body = client.get("/api/quarantine/groups").json()
        for g in body:
            if g["object"] == "NotInTemplate":
                assert g["retry_allowed"] is False, (
                    f"未知对象应禁用 retry, got {g['retry_allowed']}")
                assert "模板未识别" in (g.get("retry_disabled_reason") or "")
                break
        else:
            assert False, "NotInTemplate 应出现在 groups 中"

    def test_unknown_object_in_group_retry_disabled(self, db_with_config):
        """隔离记录中存在但模板中没有的对象,groups 中应看到 retry_allowed=False。"""
        landing, cfg = db_with_config

        # 直接插入隔离记录(确保出现在 groups 中)
        landing.con.execute(
            "INSERT INTO d2a_quarantine (source, object, keys_json, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (SOURCE, "FakeObj", '{"k":"v"}', "test unknown",
             datetime.now().isoformat(timespec="seconds")))
        landing.con.commit()

        client = TestClient(create_app(landing.db_path, str(ROOT / "templates"), config=cfg))
        body = client.get("/api/quarantine/groups").json()
        fake = next((g for g in body if g["object"] == "FakeObj"), None)
        assert fake is not None, "隔离记录应在 groups 中出现"
        assert fake["retry_allowed"] is False
        assert "模板未识别" in fake["retry_disabled_reason"]
