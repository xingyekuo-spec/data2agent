"""E6a 测试:落地出口 Sink + 平台接收端 ingest + 推送端到端。"""

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.middle.extract.adapters.base import TableInfo  # noqa: E402
from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.middle.extract.increment import incremental_sync  # noqa: E402
from data2agent.middle.extract.reconcile import reconcile_remote  # noqa: E402
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore, raw_table_name  # noqa: E402
from data2agent.middle.extract.sink import HttpPushSink, LocalSink  # noqa: E402
from tests.helpers import whitelist_from_pack  # noqa: E402
from data2agent.platform.console.validation import build_validation_report  # noqa: E402
from data2agent.platform.ingest.app import create_app  # noqa: E402
from data2agent.protocol.ingest import BINARY_ENCODING, INGEST_PROTOCOL_VERSION  # noqa: E402
from data2agent.shared.metamodel.loader import load_pack  # noqa: E402
from tests.fixtures.e10.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"
TABLES = ["CURRENCY", "CUSTOMER", "ITEM", "ITEM_WAREHOUSE", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]


def _v(body: dict) -> dict:
    """附加强制协议版本字段。"""
    return {"ingest_protocol_version": INGEST_PROTOCOL_VERSION, **body}


def _open_generation(
    client: TestClient, generation_id: str, tables: list[str], *,
    headers: dict | None = None,
) -> str:
    response = client.post("/ingest/run-begin", json=_v({
        "source": SOURCE, "generation_id": generation_id, "tables": tables,
    }), headers=headers or {})
    assert response.status_code == 200
    return generation_id


@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def source_db(tmp_path) -> Path:
    db = tmp_path / "source.sqlite"
    write_db(db, build(seed=42, asof=date(2026, 7, 10)))
    return db


def _adapter(source_db, pack):
    return SqliteReadOnlyAdapter(str(source_db), whitelist_from_pack(pack, SOURCE))


def _testclient_post(client: TestClient, token: str | None = None):
    """把 HttpPushSink 的 POST 路由到 ingest 的 TestClient(免起真服务)。"""
    def post(url, payload, tok, timeout):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        r = client.post(path, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()
    return post


def _testclient_get_json(client: TestClient, token: str | None = None):
    def get_json(url, tok, timeout):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        r = client.get(path, headers=headers)
        r.raise_for_status()
        return r.json()
    return get_json


def _push_sink(client: TestClient) -> HttpPushSink:
    return HttpPushSink(
        "http://platform",
        post=_testclient_post(client),
        get_json=_testclient_get_json(client),
    )


# ---- Sink 抽象 ----

def test_local_sink_is_default_behavior(source_db, pack, tmp_path):
    """不传 sink 与显式传 LocalSink 结果一致(向后兼容)。"""
    a = LandingStore(tmp_path / "a.sqlite")
    incremental_sync(_adapter(source_db, pack), a, SOURCE, watermarks_from_pack(pack, SOURCE))
    b = LandingStore(tmp_path / "b.sqlite")
    incremental_sync(_adapter(source_db, pack), b, SOURCE, watermarks_from_pack(pack, SOURCE),
                     sink=LocalSink(b))
    for t in TABLES:
        assert a.count(SOURCE, t) == b.count(SOURCE, t)


# ---- ingest 接收端 ----

def test_ingest_health_reports_protocol_version(tmp_path):
    client = TestClient(create_app(LandingStore(tmp_path / "p.sqlite").db_path))
    r = client.get("/ingest/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ingest_protocol_version"] == "2"
    assert body["active_ingest_protocol_version"] == "3"
    assert body["supported_ingest_protocol_versions"] == ["2", "3"]
    assert body["reconcile_protocol_version"] == "1"
    assert body["binary_encoding"] == BINARY_ENCODING


def test_active_generation_rejects_duplicate_connector_and_stale_is_recoverable(
    tmp_path,
):
    landing = LandingStore(tmp_path / "platform.sqlite")
    landing.begin_ingest_generation(SOURCE, "g-active", ["T"])
    with pytest.raises(ValueError, match="仍在活动"):
        landing.begin_ingest_generation(SOURCE, "g-duplicate", ["T"])
    landing.con.execute(
        "UPDATE d2a_ingest_generation SET last_activity_at = '2000-01-01T00:00:00' "
        "WHERE source = ? AND generation_id = 'g-active'", (SOURCE,))
    landing.con.commit()
    result = landing.begin_ingest_generation(SOURCE, "g-recovered", ["T"])
    assert result["status"] == "open"
    assert landing.con.execute(
        "SELECT status FROM d2a_ingest_generation "
        "WHERE source = ? AND generation_id = 'g-active'", (SOURCE,)
    ).fetchone()["status"] == "failed"


def test_closed_generation_cannot_modify_raw(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    assert client.post("/ingest/run-begin", json=_v({
        "source": SOURCE, "generation_id": "g-closed", "tables": ["T"],
    })).status_code == 200
    assert client.post("/ingest/run-abort", json=_v({
        "source": SOURCE, "generation_id": "g-closed",
    })).status_code == 200
    response = client.post("/ingest/batch", json=_v({
        "source": SOURCE, "generation_id": "g-closed", "table": "T",
        "mode": "incremental", "columns": [["ID", "int"]], "pk": ["ID"],
        "batch_id": "b-closed", "table_run_id": "r-closed", "rows": [{"ID": 1}],
    }))
    assert response.status_code == 409
    assert not landing.raw_table_exists(SOURCE, "T")


def test_blob_round_trips_over_http_without_string_corruption(tmp_path):
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    sink = _push_sink(client)
    info = TableInfo(
        "BINARY_T", [("ID", "int"), ("PAYLOAD", "blob")], ["ID"])
    raw = b"\x00\xffbinary;not-text\x80"
    sink.begin_sync(SOURCE, [info.name], 1)
    sink.begin_table(SOURCE, info, mode="incremental")
    assert sink.write(
        SOURCE, info, [{"ID": 1, "PAYLOAD": raw}], "blob-batch",
        table_run_id="blob-run") == 1
    sink.complete_table(SOURCE, info, "blob-run", 1, 1)
    sink.complete_sync(SOURCE)
    stored = platform.con.execute(
        f'SELECT PAYLOAD FROM "{raw_table_name(SOURCE, info.name)}" WHERE ID = 1'
    ).fetchone()["PAYLOAD"]
    assert stored == raw and isinstance(stored, bytes)


def test_expired_generation_apply_lease_is_recoverable(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    landing.begin_ingest_generation(SOURCE, "g-expire", [])
    landing.complete_ingest_generation(SOURCE, "g-expire")
    assert landing.claim_committed_generation(
        SOURCE, owner_id="dead-worker") == "g-expire"
    stale_run = landing.start_run(SOURCE, "apply")
    landing.con.execute(
        "UPDATE d2a_ingest_generation SET apply_lease_until = ? "
        "WHERE source = ? AND generation_id = ?",
        ("2000-01-01T00:00:00", SOURCE, "g-expire"))
    landing.con.commit()

    assert landing.claim_committed_generation(
        SOURCE, owner_id="replacement") == "g-expire"
    assert landing.con.execute(
        "SELECT status FROM d2a_sync_run WHERE id = ?", (stale_run,)
    ).fetchone()["status"] == "failed"
    landing.finish_generation_apply(
        SOURCE, "g-expire", success=True, owner_id="replacement")


def test_manual_generation_lease_serializes_retry_without_new_push(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    landing.begin_ingest_generation(SOURCE, "g-applied", [])
    landing.complete_ingest_generation(SOURCE, "g-applied")
    assert landing.claim_committed_generation(
        SOURCE, owner_id="worker") == "g-applied"
    landing.finish_generation_apply(
        SOURCE, "g-applied", success=True, owner_id="worker")

    manual = landing.claim_manual_generation_apply(
        SOURCE, owner_id="console-retry")
    assert manual and manual.startswith("manual-")
    with pytest.raises(ValueError, match="仍处于 applying"):
        landing.begin_ingest_generation(SOURCE, "g-overlap", [])
    landing.finish_generation_apply(
        SOURCE, manual, success=False, owner_id="console-retry")
    assert landing.con.execute(
        "SELECT status FROM d2a_ingest_generation "
        "WHERE source = ? AND generation_id = ?", (SOURCE, manual),
    ).fetchone()["status"] == "failed"
    assert landing.claim_manual_generation_apply(
        SOURCE, owner_id="console-retry-2") is not None


def test_ingest_batch_lands_rows(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    generation = _open_generation(client, "g-batch-lands", ["CURRENCY"])
    body = _v({"source": SOURCE, "generation_id": generation,
               "table": "CURRENCY", "mode": "incremental",
               "columns": [["Id", "int"], ["CURRENCY_CODE", "text"], ["_x", "text"]],
               "pk": ["Id"], "batch_id": "b1",
               "rows": [{"Id": 1, "CURRENCY_CODE": "USD", "_x": None},
                        {"Id": 2, "CURRENCY_CODE": "EUR", "_x": None}]})
    r = client.post("/ingest/batch", json=body)
    assert r.status_code == 200 and r.json()["ingested"] == 2
    assert landing.count(SOURCE, "CURRENCY") == 2
    # 重推幂等
    assert client.post("/ingest/batch", json=body).json()["ingested"] == 2
    assert landing.count(SOURCE, "CURRENCY") == 2


def test_ingest_rejects_same_batch_id_with_different_payload(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    generation = _open_generation(client, "g-batch-conflict", ["T"])
    base = _v({
        "source": SOURCE, "generation_id": generation,
        "table": "T", "mode": "incremental",
        "columns": [["ID", "int"], ["V", "text"]], "pk": ["ID"],
        "batch_id": "same-id", "table_run_id": "run-1",
    })
    assert client.post(
        "/ingest/batch", json={**base, "rows": [{"ID": 1, "V": "a"}]}
    ).status_code == 200
    conflict = client.post(
        "/ingest/batch", json={**base, "rows": [{"ID": 2, "V": "b"}]}
    )
    assert conflict.status_code == 409
    assert landing.count(SOURCE, "T") == 1


def test_generation_requires_all_tables_and_blocks_apply_overlap(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    begin = _v({
        "source": SOURCE, "generation_id": "g1", "tables": ["A", "B"],
    })
    assert client.post("/ingest/run-begin", json=begin).status_code == 200
    incomplete = client.post("/ingest/run-complete", json=_v({
        "source": SOURCE, "generation_id": "g1",
    }))
    assert incomplete.status_code == 409
    landing.record_ingest_table_commit(SOURCE, "g1", "A", 1, 1)
    landing.record_ingest_table_commit(SOURCE, "g1", "B", 1, 1)
    assert client.post("/ingest/run-complete", json=_v({
        "source": SOURCE, "generation_id": "g1",
    })).status_code == 200
    pending = client.post("/ingest/run-begin", json=_v({
        "source": SOURCE, "generation_id": "g2", "tables": ["A"],
    }))
    assert pending.status_code == 409
    assert landing.claim_committed_generation(SOURCE) == "g1"
    blocked = client.post("/ingest/run-begin", json=_v({
        "source": SOURCE, "generation_id": "g2", "tables": ["A"],
    }))
    assert blocked.status_code == 409
    landing.finish_generation_apply(SOURCE, "g1", success=True)
    assert client.post("/ingest/run-begin", json=_v({
        "source": SOURCE, "generation_id": "g2", "tables": ["A"],
    })).status_code == 200


def test_ingest_rejects_missing_or_wrong_protocol_version(tmp_path):
    """旧客户端不带版本 / 错误版本不得写入。"""
    client = TestClient(create_app(LandingStore(tmp_path / "p.sqlite").db_path))
    legacy = {
        "source": SOURCE, "table": "CURRENCY", "mode": "incremental",
        "columns": [["Id", "int"]], "pk": ["Id"], "batch_id": "b1",
        "rows": [{"Id": 1}],
    }
    assert client.post("/ingest/batch", json=legacy).status_code == 422
    wrong = {**legacy, "ingest_protocol_version": "1"}
    assert client.post("/ingest/batch", json=wrong).status_code == 422
    generation = _open_generation(client, "g-protocol", ["CURRENCY"])
    ok = _v({**legacy, "generation_id": generation})
    assert client.post("/ingest/batch", json=ok).status_code == 200


def test_v3_write_and_reconcile_requests_require_generation_id(tmp_path):
    """v3 的数据写入与 E6b 请求都不能退化为无 generation 的旁路。"""
    client = TestClient(create_app(LandingStore(tmp_path / "p.sqlite").db_path))
    batch = _v({
        "source": SOURCE, "table": "T", "mode": "incremental",
        "columns": [["ID", "int"]], "pk": ["ID"],
        "batch_id": "b-no-generation", "rows": [{"ID": 1}],
    })
    reconcile_stats = _v({"source": SOURCE, "table": "T"})

    assert client.post("/ingest/batch", json=batch).status_code == 422
    assert client.post("/ingest/reconcile", json=reconcile_stats).status_code == 422

    # v2 兼容窗口仍保留旧写法，避免升级平台时直接切断旧中间机。
    assert client.post("/ingest/batch", json={
        **batch, "ingest_protocol_version": "2",
    }).status_code == 200


def test_ingest_batch_retry_keeps_original_completion_time(tmp_path):
    """重放历史数据批次不能把它伪装成新的表级证据。"""
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    generation = _open_generation(client, "g-retry-time", ["CURRENCY"])
    body = _v({"source": SOURCE, "generation_id": generation,
               "table": "CURRENCY", "mode": "incremental",
               "columns": [["Id", "int"]],
               "pk": ["Id"], "batch_id": "retry-time", "rows": [{"Id": 1}]})
    assert client.post("/ingest/batch", json=body).status_code == 200
    (step_id,) = landing.con.execute(
        "SELECT id FROM d2a_run_step WHERE kind = 'batch' AND batch_id = ?", ("retry-time",)
    ).fetchone()
    landing.con.execute("UPDATE d2a_run_step SET finished_at = ? WHERE id = ?",
                        ("2020-01-01T00:00:00", step_id))
    landing.con.commit()
    retried = client.post("/ingest/batch", json=body)
    assert retried.status_code == 200 and retried.json()["duplicate"] is True
    (finished_at,) = landing.con.execute(
        "SELECT finished_at FROM d2a_run_step WHERE id = ?", (step_id,)
    ).fetchone()
    assert finished_at == "2020-01-01T00:00:00"


def test_table_complete_creates_zero_row_raw_table(tmp_path):
    """零行表也必须有 raw 表和表级完成事件。"""
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    generation = _open_generation(client, "g-empty", ["EMPTY_DIM"])
    body = _v({"source": SOURCE, "generation_id": generation,
               "table": "EMPTY_DIM", "mode": "incremental",
               "columns": [["Id", "int"]],
               "pk": ["Id"], "completion_id": "empty-1", "rows": 0, "batches": 0})
    result = client.post("/ingest/table-complete", json=body)
    assert result.status_code == 200 and result.json()["completed"] is True
    assert landing.count(SOURCE, "EMPTY_DIM") == 0
    step = landing.con.execute(
        "SELECT kind, target, status, rows_out FROM d2a_run_step WHERE batch_id = ?",
        ("empty-1",),
    ).fetchone()
    assert tuple(step) == ("table", "EMPTY_DIM", "ok", 0)
    # 幂等重试不刷新首次完成时间。
    landing.con.execute("UPDATE d2a_run_step SET finished_at = '2020-01-01T00:00:00' WHERE batch_id = ?",
                        ("empty-1",))
    landing.con.commit()
    assert client.post("/ingest/table-complete", json=body).json()["duplicate"] is True
    (finished_at,) = landing.con.execute(
        "SELECT finished_at FROM d2a_run_step WHERE batch_id = ?", ("empty-1",)
    ).fetchone()
    assert finished_at == "2020-01-01T00:00:00"


def test_http_sync_emits_completion_for_zero_row_table(tmp_path):
    """中间机真实同步零行表时，仍必须推送 table-complete。"""
    class EmptyAdapter:
        def tables(self):
            return [TableInfo("EMPTY_DIM", [("Id", "int")], ["Id"])]

        def validate_runtime_keys(self, _table):
            return None

        def read_increment(self, _table, since=None, watermark_col=None, resume_after=None):
            return iter(())

    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    middle = LandingStore(tmp_path / "middle.sqlite")
    sink = _push_sink(client)
    report = incremental_sync(EmptyAdapter(), middle, SOURCE, sink=sink)
    assert report.tables[0].rows == 0
    assert platform.count(SOURCE, "EMPTY_DIM") == 0
    step = platform.con.execute(
        "SELECT kind, target, status FROM d2a_run_step WHERE kind = 'table'"
    ).fetchone()
    assert tuple(step) == ("table", "EMPTY_DIM", "ok")


def test_ingest_rejects_missing_pk(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    r = client.post("/ingest/batch", json=_v({
        "source": SOURCE, "table": "T", "mode": "incremental",
        "columns": [["A", "text"]], "pk": [],
        "batch_id": "b", "rows": [{"A": "x"}]}))
    assert r.status_code == 422


def test_ingest_token_auth(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path, token="s3cret"))
    headers = {"Authorization": "Bearer s3cret"}
    generation = _open_generation(
        client, "g-auth", ["CURRENCY"], headers=headers)
    body = _v({"source": SOURCE, "generation_id": generation,
               "table": "CURRENCY", "mode": "incremental",
               "columns": [["Id", "int"]],
               "pk": ["Id"], "batch_id": "b1", "rows": [{"Id": 1}]})
    assert client.post("/ingest/batch", json=body).status_code == 401
    ok = client.post("/ingest/batch", json=body, headers=headers)
    assert ok.status_code == 200


# ---- 推送端到端(E6a 核心)----

def test_push_matches_direct_sync(source_db, pack, tmp_path):
    """中间(HttpPushSink)→ 平台 落地,应与直连本地 sync 逐表一致。"""
    # 直连基准
    direct = LandingStore(tmp_path / "direct.sqlite")
    incremental_sync(_adapter(source_db, pack), direct, SOURCE,
                     watermarks_from_pack(pack, SOURCE))

    # 推送:平台 ingest + 中间 HttpPushSink
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    middle_state = LandingStore(tmp_path / "middle.sqlite")   # 中间只存水位/审计,不存 raw
    sink = _push_sink(client)
    report = incremental_sync(_adapter(source_db, pack), middle_state, SOURCE,
                              watermarks_from_pack(pack, SOURCE), sink=sink)

    assert not report.paused
    for t in TABLES:
        assert platform.count(SOURCE, t) == direct.count(SOURCE, t) > 0, f"{t} 行数不一致"
    # 内容逐行一致(抽样订单表)
    raw = raw_table_name(SOURCE, "SALES_ORDER")
    d = dict(direct.con.execute(f'SELECT DOC_NO, TOTAL_AMOUNT FROM "{raw}"').fetchall())
    p = dict(platform.con.execute(f'SELECT DOC_NO, TOTAL_AMOUNT FROM "{raw}"').fetchall())
    assert d == p
    completed = platform.con.execute(
        "SELECT target FROM d2a_run_step WHERE kind = 'table' AND status = 'ok' "
        "ORDER BY target"
    ).fetchall()
    assert [r["target"] for r in completed] == sorted(TABLES)
    validation_run = platform.start_run(SOURCE, "validation", commit=False)
    report = build_validation_report(
        platform, run_id=validation_run, pack=pack, source=SOURCE,
        config=None, include_mcp_probe=False,
    )
    checks = {c["check_id"]: c for c in report["checks"]}
    assert checks["raw_presence"]["status"] == "pass"


def test_middle_holds_no_raw_but_has_watermark(source_db, pack, tmp_path):
    """Pattern A:中间只留水位/审计/运行,不落 raw。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    middle_state = LandingStore(tmp_path / "middle.sqlite")
    sink = _push_sink(client)
    incremental_sync(_adapter(source_db, pack), middle_state, SOURCE,
                     watermarks_from_pack(pack, SOURCE), sink=sink)

    raw_tables = [r[0] for r in middle_state.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'")]
    assert raw_tables == [], "中间不应有任何 raw 表"
    assert middle_state.get_high_water(SOURCE, "SALES_ORDER") is not None, "水位在中间"
    assert platform.get_high_water(SOURCE, "SALES_ORDER") is None, "平台不管水位(中间驱动)"


def test_incremental_over_push(source_db, pack, tmp_path):
    """推送模式下第二轮只推回看窗口增量,平台幂等。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    middle_state = LandingStore(tmp_path / "middle.sqlite")

    def run():
        return incremental_sync(
            _adapter(source_db, pack), middle_state, SOURCE,
            watermarks_from_pack(pack, SOURCE), sink=_push_sink(client))

    run()
    total = platform.count(SOURCE, "SALES_ORDER")
    # 平台必须先消费完整 generation，下一轮才可修改 raw。
    generation_id = platform.claim_committed_generation(SOURCE)
    assert generation_id is not None
    platform.finish_generation_apply(SOURCE, generation_id, success=True)
    r2 = run()
    orders = next(t for t in r2.tables if t.table == "SALES_ORDER")
    assert orders.strategy == "increment"
    assert platform.count(SOURCE, "SALES_ORDER") == total, "幂等:平台行数不变"


def test_protocol_version_mismatch_fail_fast(tmp_path):
    middle = LandingStore(tmp_path / "middle.sqlite")

    class TinyAdapter:
        def tables(self):
            return [TableInfo("T", [("ID", "int")], ["ID"])]

        def validate_runtime_keys(self, _table):
            return None

        def read_increment(self, *_a, **_k):
            return iter(())

    sink = HttpPushSink(
        "http://platform",
        post=lambda *a, **k: None,
        get_json=lambda *a, **k: {"ok": True, "ingest_protocol_version": "1"},
    )
    from data2agent.middle.extract.sink import ProtocolVersionError
    with pytest.raises(ProtocolVersionError, match="协议版本不一致"):
        incremental_sync(TinyAdapter(), middle, SOURCE, watermarks={}, sink=sink)


def test_push_error_details_are_sanitized_before_state_persistence(tmp_path):
    middle = LandingStore(tmp_path / "middle.sqlite")
    run_id = middle.start_run(SOURCE, "sync")

    def fail_with_secret(*_args, **_kwargs):
        raise RuntimeError(
            "Authorization: Bearer push-super-secret; "
            "DSN=erp; password=db-super-secret; SELECT * FROM payroll"
        )

    sink = HttpPushSink(
        "http://platform",
        retries=1,
        post=fail_with_secret,
        get_json=lambda *_args, **_kwargs: {
            "supported_ingest_protocol_versions": [INGEST_PROTOCOL_VERSION],
        },
        landing=middle,
        source=SOURCE,
        run_id=run_id,
    )
    with pytest.raises(RuntimeError):
        sink.begin_sync(SOURCE, ["T"], run_id)

    detail = middle.con.execute(
        "SELECT error_detail FROM d2a_http_push_log "
        "WHERE run_id = ? AND status = 'failed'",
        (run_id,),
    ).fetchone()["error_detail"]
    assert "push-super-secret" not in detail
    assert "db-super-secret" not in detail
    assert "payroll" not in detail
    assert "已脱敏" in detail


def test_http_abort_cleans_remote_staging_after_begin(tmp_path):
    """begin 后 abort:平台不得残留 open snapshot / staging 表。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    info = TableInfo("CURRENCY", [("CODE", "text"), ("NAME", "text")], ["CODE"])
    sink = _push_sink(client)
    sink.begin_sync(SOURCE, [info.name], 1)
    sink.begin_table(SOURCE, info, mode="full_refresh", snapshot_id="snap-begin")
    row = platform.con.execute(
        "SELECT status, staging_table FROM d2a_snapshot "
        "WHERE snapshot_id = ?", ("snap-begin",)).fetchone()
    assert row["status"] == "open"
    staging = row["staging_table"]
    sink.abort_table(SOURCE, info, mode="full_refresh", snapshot_id="snap-begin")
    row2 = platform.con.execute(
        "SELECT status FROM d2a_snapshot WHERE snapshot_id = ?",
        ("snap-begin",)).fetchone()
    assert row2["status"] == "failed"
    assert platform.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (staging,)).fetchone() is None


def test_http_push_log_counts_write_rows_once_and_records_abort(tmp_path):
    """推送详情只累计成功 write 行数，且 abort 也必须留痕。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    middle = LandingStore(tmp_path / "middle.sqlite")
    info = TableInfo("CURRENCY", [("CODE", "text"), ("NAME", "text")], ["CODE"])
    sink = HttpPushSink(
        "http://platform", post=_testclient_post(client),
        get_json=_testclient_get_json(client), landing=middle, source=SOURCE,
    )
    batch_id = "increment-1"
    sink.begin_sync(SOURCE, [info.name], 1)
    sink.begin_table(SOURCE, info, mode="incremental")
    sink.write(
        SOURCE, info, [{"CODE": "USD", "NAME": "美元"}],
        f"{batch_id}-0", table_run_id=batch_id)
    sink.write(
        SOURCE, info, [{"CODE": "EUR", "NAME": "欧元"}],
        f"{batch_id}-1", table_run_id=batch_id)
    sink.complete_table(SOURCE, info, batch_id, rows=2, batches=2)
    progress = middle.push_log_batch_progress(SOURCE, "CURRENCY", batch_id)
    assert progress["rows"] == 2
    assert progress["write_ok_batches"] == 2
    assert progress["completed"] is True

    snapshot_id = "snapshot-abort"
    sink.begin_table(SOURCE, info, mode="full_refresh", snapshot_id=snapshot_id)
    sink.abort_table(SOURCE, info, mode="full_refresh", snapshot_id=snapshot_id)
    abort_log = middle.con.execute(
        "SELECT status FROM d2a_http_push_log "
        "WHERE step_kind = 'abort_table' AND batch_id = ?",
        (snapshot_id,),
    ).fetchone()
    assert abort_log["status"] == "ok"


def test_push_summary_recovers_from_retrying_and_exposes_receipt(tmp_path):
    """退避证据不能在后续成功后把批次永久显示为 retrying。"""
    middle = LandingStore(tmp_path / "middle-state.sqlite")
    batch_id = "table-run-1"
    middle.record_push_log(
        SOURCE, "write", "CURRENCY", "incremental", batch_id=batch_id,
        rows_count=1, status="retrying", retry_count=1,
        error_category="network", retryable=True,
    )
    assert middle.push_log_table_summaries()[0]["status"] == "retrying"

    middle.record_push_log(
        SOURCE, "write", "CURRENCY", "incremental", batch_id=batch_id,
        rows_count=1, status="ok", retry_count=1,
        receipt_received=True, idempotent_replay=True,
        receipt_digest="sha256:test",
    )
    after_retry = middle.push_log_table_summaries()[0]
    assert after_retry["status"] == "pushing"
    assert after_retry["retry_count"] == 1
    assert after_retry["receipt_received"] is True
    assert after_retry["idempotent_replay"] is True

    middle.record_push_log(
        SOURCE, "complete_table", "CURRENCY", "incremental",
        batch_id=batch_id, rows_count=1, status="ok",
        receipt_received=True,
    )
    completed = middle.push_log_table_summaries()[0]
    assert completed["status"] == "completed"
    assert completed["rows"] == 1


def test_generation_heartbeat_success_and_rejection_are_audited(tmp_path):
    """generation 心跳须携带 generation，并给 409 独立稳定分类。"""
    import io
    import json
    import urllib.error

    from data2agent.protocol.ingest import INGEST_PROTOCOL_VERSION

    middle = LandingStore(tmp_path / "middle-state.sqlite")
    posted: list[tuple[str, dict]] = []

    def post_ok(url, payload, *_args):
        posted.append((url, payload))
        return {"ok": True}

    health = lambda *_args: {  # noqa: E731
        "supported_ingest_protocol_versions": [INGEST_PROTOCOL_VERSION],
        "generation_heartbeat": True,
    }
    sink = HttpPushSink(
        "http://platform", post=post_ok, get_json=health,
        landing=middle, source=SOURCE, retries=1,
    )
    run_id = middle.start_run(SOURCE, "sync")
    sink.begin_sync(SOURCE, ["T"], run_id)
    sink.heartbeat_sync(SOURCE)
    heartbeat_url, heartbeat_payload = posted[-1]
    assert heartbeat_url.endswith("/ingest/run-heartbeat")
    assert heartbeat_payload["generation_id"] == sink._generation_id
    ok_log = middle.con.execute(
        "SELECT status, generation_id FROM d2a_http_push_log "
        "WHERE step_kind = 'heartbeat_generation' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ok_log["status"] == "ok"
    assert ok_log["generation_id"] == sink._generation_id

    def reject_heartbeat(url, payload, *_args):
        if url.endswith("/ingest/run-heartbeat"):
            body = io.BytesIO(json.dumps({
                "detail": "旧 generation 已关闭",
            }).encode("utf-8"))
            raise urllib.error.HTTPError(url, 409, "Conflict", {}, body)
        return {"ok": True}

    rejected = HttpPushSink(
        "http://platform", post=reject_heartbeat, get_json=health,
        landing=middle, source=SOURCE, retries=1,
    )
    rejected.begin_sync(SOURCE, ["T"], run_id)
    with pytest.raises(RuntimeError, match="HTTP 409"):
        rejected.heartbeat_sync(SOURCE)
    failed_log = middle.con.execute(
        "SELECT status, error_category, retryable FROM d2a_http_push_log "
        "WHERE step_kind = 'heartbeat_generation' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert failed_log["status"] == "failed"
    assert failed_log["error_category"] == "generation_heartbeat_rejected"
    assert failed_log["retryable"] == 0


def test_http_abort_cleans_after_batch_and_failed_complete(tmp_path):
    """批次写入后 / 完成核对失败后 abort 均清理 staging。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    info = TableInfo("CURRENCY", [("CODE", "text"), ("NAME", "text")], ["CODE"])
    sink = _push_sink(client)
    sid = "snap-batch"
    sink.begin_sync(SOURCE, [info.name], 1)
    sink.begin_table(SOURCE, info, mode="full_refresh", snapshot_id=sid)
    sink.write(SOURCE, info, [{"CODE": "USD", "NAME": "美元"}], "b1",
               mode="full_refresh", snapshot_id=sid)
    staging = platform.con.execute(
        "SELECT staging_table FROM d2a_snapshot WHERE snapshot_id = ?",
        (sid,)).fetchone()["staging_table"]
    assert platform.con.execute(
        f'SELECT COUNT(*) FROM "{staging}"').fetchone()[0] == 1

    # 完成行数不符 → 422,随后 abort
    bad = _v({
        "source": SOURCE, "generation_id": sink._generation_id,
        "table": "CURRENCY", "mode": "full_refresh",
        "columns": [["CODE", "text"], ["NAME", "text"]], "pk": ["CODE"],
        "snapshot_id": sid, "completion_id": "c1", "rows": 99, "batches": 1,
    })
    assert client.post("/ingest/table-complete", json=bad).status_code == 422
    sink.abort_table(SOURCE, info, mode="full_refresh", snapshot_id=sid)
    assert platform.con.execute(
        "SELECT status FROM d2a_snapshot WHERE snapshot_id = ?", (sid,)
    ).fetchone()["status"] == "failed"
    assert platform.con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (staging,)).fetchone() is None


def test_http_sync_pause_aborts_remote_snapshot(tmp_path):
    """窗口暂停时 HttpPushSink 必须清理远端 open snapshot。"""
    import sqlite3

    src = tmp_path / "src.sqlite"
    con = sqlite3.connect(src)
    con.executescript(
        """
        CREATE TABLE CURRENCY (CODE TEXT PRIMARY KEY, NAME TEXT);
        INSERT INTO CURRENCY VALUES ('USD', '美元');
        INSERT INTO CURRENCY VALUES ('EUR', '欧元');
        INSERT INTO CURRENCY VALUES ('JPY', '日元');
        """
    )
    con.close()
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    middle = LandingStore(tmp_path / "middle.sqlite")
    calls = {"n": 0}

    def pause_mid():
        calls["n"] += 1
        # 1=表入口, 2=第一批前 → 写入; 3=第二批前 → 暂停并 abort
        return calls["n"] <= 2

    report = incremental_sync(
        SqliteReadOnlyAdapter(str(src), {"CURRENCY"}, batch_size=1),
        middle, SOURCE, watermarks={}, sink=_push_sink(client),
        should_continue=pause_mid,
    )
    assert report.paused is True
    open_snaps = platform.con.execute(
        "SELECT snapshot_id, status FROM d2a_snapshot WHERE status = 'open'"
    ).fetchall()
    assert open_snaps == []
    staging_left = platform.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB '*__snap_*'"
    ).fetchall()
    assert staging_left == []


def test_e6b_remote_reconcile_repairs_edit_and_delete(
    source_db, pack, tmp_path,
):
    """中间只留运行元数据；平台完成跨机重抽与运行键 diff 软删。"""
    import sqlite3

    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    middle = LandingStore(tmp_path / "middle.sqlite")
    sink = _push_sink(client)
    watermarks = watermarks_from_pack(pack, SOURCE)

    incremental_sync(
        _adapter(source_db, pack), middle, SOURCE, watermarks, sink=sink)
    initial_generation = platform.claim_committed_generation(
        SOURCE, owner_id="test-apply")
    assert initial_generation is not None
    platform.finish_generation_apply(
        SOURCE, initial_generation, success=True, owner_id="test-apply")
    assert not any(
        row[0].startswith(f"raw_{SOURCE}__")
        for row in middle.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))

    rw = sqlite3.connect(source_db)
    rw.execute("DELETE FROM CURRENCY WHERE Id = 2")
    rw.execute(
        "UPDATE ITEM SET STANDARD_COST = 123456789.1234 WHERE Id = 1")
    rw.commit()
    rw.close()

    report = reconcile_remote(
        _adapter(source_db, pack), middle, sink, SOURCE,
        watermarks, deep=True)
    assert report.total_soft_deleted >= 1
    deleted = platform.con.execute(
        f'SELECT _d2a_deleted_at FROM "{raw_table_name(SOURCE, "CURRENCY")}" '
        "WHERE Id = 2").fetchone()
    assert deleted["_d2a_deleted_at"] is not None
    repaired = platform.con.execute(
        f'SELECT STANDARD_COST FROM "{raw_table_name(SOURCE, "ITEM")}" '
        "WHERE Id = 1").fetchone()
    assert repaired["STANDARD_COST"] == "123456789.1234"
    pending = platform.claim_committed_generation(
        SOURCE, owner_id="test-reconcile-apply")
    assert pending is not None and pending.startswith("reconcile-")
