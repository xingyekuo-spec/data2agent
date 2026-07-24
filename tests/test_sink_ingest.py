"""E6a 测试:落地出口 Sink + 平台接收端 ingest + 推送端到端。"""

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.base import TableInfo  # noqa: E402
from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.increment import incremental_sync  # noqa: E402
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore, raw_table_name  # noqa: E402
from data2agent.connect.sink import HttpPushSink, LocalSink  # noqa: E402
from tests.helpers import whitelist_from_pack  # noqa: E402
from data2agent.console.validation import build_validation_report  # noqa: E402
from data2agent.ingest.app import create_app  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.showroom.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
TABLES = ["CURRENCY", "CUSTOMER", "ITEM", "ITEM_WAREHOUSE", "QUOTATION", "SALES_ORDER", "SALES_ORDER_D"]


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

def test_ingest_batch_lands_rows(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    body = {"source": SOURCE, "table": "CURRENCY",
            "columns": [["Id", "int"], ["CURRENCY_CODE", "text"], ["_x", "text"]],
            "pk": ["Id"], "batch_id": "b1",
            "rows": [{"Id": 1, "CURRENCY_CODE": "USD", "_x": None},
                     {"Id": 2, "CURRENCY_CODE": "EUR", "_x": None}]}
    r = client.post("/ingest/batch", json=body)
    assert r.status_code == 200 and r.json()["ingested"] == 2
    assert landing.count(SOURCE, "CURRENCY") == 2
    # 重推幂等
    assert client.post("/ingest/batch", json=body).json()["ingested"] == 2
    assert landing.count(SOURCE, "CURRENCY") == 2


def test_ingest_batch_retry_keeps_original_completion_time(tmp_path):
    """重放历史数据批次不能把它伪装成新的表级证据。"""
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path))
    body = {"source": SOURCE, "table": "CURRENCY", "columns": [["Id", "int"]],
            "pk": ["Id"], "batch_id": "retry-time", "rows": [{"Id": 1}]}
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
    body = {"source": SOURCE, "table": "EMPTY_DIM", "columns": [["Id", "int"]],
            "pk": ["Id"], "completion_id": "empty-1", "rows": 0, "batches": 0}
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
    sink = HttpPushSink("http://platform", post=_testclient_post(client))
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
    r = client.post("/ingest/batch", json={
        "source": SOURCE, "table": "T", "columns": [["A", "text"]], "pk": [],
        "batch_id": "b", "rows": [{"A": "x"}]})
    assert r.status_code == 422


def test_ingest_token_auth(tmp_path):
    landing = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(landing.db_path, token="s3cret"))
    body = {"source": SOURCE, "table": "CURRENCY", "columns": [["Id", "int"]],
            "pk": ["Id"], "batch_id": "b1", "rows": [{"Id": 1}]}
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
    sink = HttpPushSink("http://platform", post=_testclient_post(client))
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
    sink = HttpPushSink("http://platform", post=_testclient_post(client))
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
        sink = HttpPushSink("http://platform", post=_testclient_post(client))
        return incremental_sync(_adapter(source_db, pack), middle_state, SOURCE,
                                watermarks_from_pack(pack, SOURCE), sink=sink)

    run()
    total = platform.count(SOURCE, "SALES_ORDER")
    r2 = run()
    orders = next(t for t in r2.tables if t.table == "SALES_ORDER")
    assert orders.strategy == "increment"
    assert platform.count(SOURCE, "SALES_ORDER") == total, "幂等:平台行数不变"
