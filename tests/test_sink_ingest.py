"""E6a 测试:落地出口 Sink + 平台接收端 ingest + 推送端到端。"""

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.middle.extract.adapters.base import TableInfo  # noqa: E402
from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.middle.extract.increment import incremental_sync  # noqa: E402
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore, raw_table_name  # noqa: E402
from data2agent.middle.extract.sink import HttpPushSink, LocalSink  # noqa: E402
from tests.helpers import whitelist_from_pack  # noqa: E402
from data2agent.platform.console.validation import build_validation_report  # noqa: E402
from data2agent.platform.ingest.app import create_app  # noqa: E402
from data2agent.protocol.ingest import INGEST_PROTOCOL_VERSION  # noqa: E402
from data2agent.shared.metamodel.loader import load_pack  # noqa: E402
from tests.fixtures.e10.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TABLES = ["CURRENCY", "CUSTOMER", "ITEM", "ITEM_WAREHOUSE", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]


def _v(body: dict) -> dict:
    """附加强制协议版本字段。"""
    return {"ingest_protocol_version": INGEST_PROTOCOL_VERSION, **body}


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
    assert body["active_ingest_protocol_version"] == "2"
    assert body["supported_ingest_protocol_versions"] == ["2"]


def test_ingest_batch_lands_rows(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    body = _v({"source": SOURCE, "table": "CURRENCY", "mode": "incremental",
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
    ok = _v(legacy)
    assert client.post("/ingest/batch", json=ok).status_code == 200


def test_ingest_batch_retry_keeps_original_completion_time(tmp_path):
    """重放历史数据批次不能把它伪装成新的表级证据。"""
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    body = _v({"source": SOURCE, "table": "CURRENCY", "mode": "incremental",
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
    body = _v({"source": SOURCE, "table": "EMPTY_DIM", "mode": "incremental",
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
    body = _v({"source": SOURCE, "table": "CURRENCY", "mode": "incremental",
               "columns": [["Id", "int"]],
               "pk": ["Id"], "batch_id": "b1", "rows": [{"Id": 1}]})
    assert client.post("/ingest/batch", json=body).status_code == 401
    ok = client.post("/ingest/batch", json=body,
                     headers={"Authorization": "Bearer s3cret"})
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


def test_http_abort_cleans_remote_staging_after_begin(tmp_path):
    """begin 后 abort:平台不得残留 open snapshot / staging 表。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    info = TableInfo("CURRENCY", [("CODE", "text"), ("NAME", "text")], ["CODE"])
    sink = _push_sink(client)
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
    sink.begin_table(SOURCE, info, mode="incremental")
    sink.write(SOURCE, info, [{"CODE": "USD", "NAME": "美元"}], batch_id)
    sink.write(SOURCE, info, [{"CODE": "EUR", "NAME": "欧元"}], batch_id)
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


def test_http_abort_cleans_after_batch_and_failed_complete(tmp_path):
    """批次写入后 / 完成核对失败后 abort 均清理 staging。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))
    info = TableInfo("CURRENCY", [("CODE", "text"), ("NAME", "text")], ["CODE"])
    sink = _push_sink(client)
    sid = "snap-batch"
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
        "source": SOURCE, "table": "CURRENCY", "mode": "full_refresh",
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
