"""M4-T04 运行 API 测试:列表筛选/总数/分页/稳定排序、详情 steps、legacy 语义。"""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_apply import apply_objects
from tests.helpers import whitelist_from_pack
from data2agent.console.app import create_app
from data2agent.console.contracts import HttpError, RunDetailResponse, RunSummary
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE))
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    apply_objects(landing, pack, SOURCE)
    # 一条 legacy 运行(无 run_type、无 steps)
    landing.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, finished_at, tables, rows,"
        " status, detail) VALUES (?, '2020-01-01 00:00:00', '2020-01-01 00:00:05',"
        " 1, 10, 'ok', '老记录')", (SOURCE,))
    landing.con.commit()
    return landing


def _client(landing: LandingStore) -> TestClient:
    return TestClient(create_app(landing.db_path, ROOT / "templates"))


def test_list_array_shape_and_total_header(env):
    client = _client(env)
    r = client.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)  # 数组 wire shape,不是 envelope
    assert r.headers["X-Total-Count"] == str(len(body))
    RunSummary.model_validate(body[0])


def test_list_filters_and_pagination(env):
    client = _client(env)
    # type 筛选
    r = client.get("/api/runs", params={"type": "apply"})
    assert r.status_code == 200
    body = r.json()
    assert body and all(x["type"] == "apply" for x in body)
    assert int(r.headers["X-Total-Count"]) == len(body)
    # status 筛选
    r = client.get("/api/runs", params={"status": "ok"})
    assert all(x["status"] == "ok" for x in r.json())
    # 分页不重叠且总数为筛选后口径
    r1 = client.get("/api/runs", params={"limit": 1, "offset": 0})
    r2 = client.get("/api/runs", params={"limit": 1, "offset": 1})
    assert r1.json()[0]["id"] != r2.json()[0]["id"]
    assert int(r1.headers["X-Total-Count"]) >= 3
    # 稳定排序:started_at DESC(同刻 id DESC 兜底)
    items = client.get("/api/runs").json()
    stamps = [x["started_at"] for x in items]
    assert stamps == sorted(stamps, reverse=True)


def test_list_filters_validation_type(env):
    run_id = env.start_run(SOURCE, "validation")
    env.finish_run(run_id, tables=1, rows=0, status="ok", detail="验收")
    client = _client(env)
    r = client.get("/api/runs", params={"type": "validation"})
    assert r.status_code == 200
    body = r.json()
    assert body and all(x["type"] == "validation" for x in body)


def test_list_rejects_bad_params(env):
    client = _client(env)
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1},
                   {"type": "bogus"}, {"status": "bogus"}):
        assert client.get("/api/runs", params=params).status_code == 422, params


def test_detail_with_structured_steps(env):
    client = _client(env)
    (run_id,) = env.con.execute(
        "SELECT MAX(id) FROM d2a_sync_run WHERE run_type = 'apply'").fetchone()
    r = client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    body = RunDetailResponse.model_validate(r.json())
    assert body.steps_state == "available"
    assert body.steps, "T03 起新运行应有 step 证据"
    step = body.steps[0]
    assert step.kind == "object"
    assert step.ordinal == 1
    assert step.rows_in is not None
    assert step.started_at.tzinfo is not None


def test_detail_legacy_run_is_unavailable_not_empty(env):
    client = _client(env)
    (legacy_id,) = env.con.execute(
        "SELECT id FROM d2a_sync_run WHERE run_type IS NULL").fetchone()
    r = client.get(f"/api/runs/{legacy_id}")
    assert r.status_code == 200
    body = RunDetailResponse.model_validate(r.json())
    assert body.steps_state == "legacy_unavailable"
    assert body.steps == []          # 明确无证据,不是"处理了 0 项"
    assert body.type is None         # legacy 类型未知,不回填


def test_detail_new_zero_step_run_is_available(env):
    run_id = env.start_run(SOURCE, "reconcile")
    env.finish_run(run_id, tables=0, rows=0, status="ok", detail="nothing to do")
    client = _client(env)
    body = RunDetailResponse.model_validate(client.get(f"/api/runs/{run_id}").json())
    assert body.steps_state == "available"
    assert body.steps == []


def test_detail_404(env):
    client = _client(env)
    r = client.get("/api/runs/99999")
    assert r.status_code == 404
    HttpError.model_validate(r.json())


def test_pipeline_nodes_link_to_run_detail(env):
    import data2agent.console.app as console_app

    client = _client(env)
    console_app._probe_http  # noqa: B018  # 保持导入可见
    body = client.get("/api/pipeline").json()
    run_ids = {n["run_id"] for n in body["nodes"] if n["run_id"] is not None}
    linked = {n["detail_path"] for n in body["nodes"] if n["detail_path"] is not None}
    assert run_ids, "sync/apply run 应进入节点"
    assert linked, "有 run 的节点应有 detail_path"
    for path in linked:
        assert path.startswith("/runs?run_id=")
        rid = int(path.split("=", 1)[1])
        assert client.get(f"/api/runs/{rid}").status_code == 200
