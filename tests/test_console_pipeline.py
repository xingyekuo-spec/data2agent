"""真实 Pipeline API 集成测试(M3-T05):固定 7 节点、overall 折叠、
local/http sink、服务健康与数据健康分离、apply 熔断可定位。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter  # noqa: E402
from data2agent.connect.config import load_config  # noqa: E402
from data2agent.connect.dataset_publish import build_dataset  # noqa: E402
from data2agent.connect.increment import incremental_sync, watermarks_from_pack  # noqa: E402
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.connect.sync import whitelist_from_pack  # noqa: E402
from data2agent.console.app import create_app  # noqa: E402
from data2agent.console.contracts import PipelineResponse  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.showroom.seed import build, write_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
NODE_ORDER = ["erp", "extract", "push", "raw", "mapping", "objects", "mcp"]


@pytest.fixture()
def env(tmp_path):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    hook = lambda action, sql, rows, ms: landing.log_audit(SOURCE, action, sql, rows, ms)  # noqa: E731
    adapter = SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE), audit_hook=hook)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    result = build_dataset(landing, pack, SOURCE, auto_publish=True)
    assert result.published
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {landing.db_path}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    sync_every: 30m\n",
        encoding="utf-8")
    return landing, load_config(cfg_file)


def _client(env) -> TestClient:
    landing, cfg = env
    return TestClient(create_app(cfg.landing, cfg.templates, cfg))


def _nodes(body: PipelineResponse) -> dict:
    return {n.node: n for n in body.nodes}


def test_pipeline_seven_nodes_fixed_order_and_overall(env):
    body = PipelineResponse.model_validate(_client(env).get("/api/pipeline").json())
    assert [n.node for n in body.nodes] == NODE_ORDER
    assert body.generated_at.tzinfo is not None
    nodes = _nodes(body)
    # 刚完成 sync+apply:erp/extract/raw/objects 健康;push 本地直写 idle;
    # mapping 因全部 binding 为 draft → warning(不是 healthy);
    # mcp 未启动 → failed;overall 折叠不可能是 healthy
    assert nodes["erp"].status == "healthy"
    assert nodes["extract"].status == "healthy"
    assert nodes["push"].status == "idle"
    assert "本地直写" in nodes["push"].status_reason
    assert nodes["raw"].status == "healthy"
    assert nodes["mapping"].status == "warning"
    assert nodes["objects"].status == "healthy"
    assert nodes["mcp"].status == "failed"
    assert body.overall_status == "failed"


def test_pipeline_mcp_probe_ok_but_data_still_rules(env, monkeypatch):
    # 服务健康 ≠ 数据健康:MCP 探测 200 也不能把 draft warning 抹绿
    import data2agent.console.app as console_app

    monkeypatch.setattr(console_app, "_probe_http", lambda *a, **k: (True, "http"))
    monkeypatch.setattr(console_app, "_probe_tcp", lambda *a, **k: (True, "tcp"))
    body = PipelineResponse.model_validate(_client(env).get("/api/pipeline").json())
    nodes = _nodes(body)
    assert nodes["mcp"].status == "healthy"
    assert nodes["mapping"].status == "warning"
    assert body.overall_status == "warning"


def test_pipeline_empty_db_idle_and_unknown(tmp_path):
    landing = LandingStore(tmp_path / "empty.sqlite")
    # 空库无任何 run:erp/raw/objects/mapping 为 idle(从未执行,不需要阈值即可判断)
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    body = PipelineResponse.model_validate(client.get("/api/pipeline").json())
    nodes = _nodes(body)
    assert nodes["erp"].status == "idle"
    assert nodes["raw"].status == "idle"
    assert nodes["objects"].status == "idle"
    assert nodes["mapping"].status == "idle"
    assert nodes["mcp"].status == "failed"  # 未启动
    assert body.overall_status == "failed"


def test_pipeline_apply_circuit_broken_locates_two_nodes(env):
    # apply 熔断后重跑一个失败 apply:mapping failed + 对象层继续使用旧结果 stale
    landing, _cfg = env
    landing.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, finished_at, tables, rows,"
        " status, detail, run_type) "
        "VALUES (?, '2026-07-18T11:00:00', '2026-07-18T11:00:40', 5, 77,"
        " 'failed', 'Customer 隔离率 53% 超过阈值,apply 中止', 'apply')", (SOURCE,))
    landing.con.commit()
    body = PipelineResponse.model_validate(_client(env).get("/api/pipeline").json())
    nodes = _nodes(body)
    assert nodes["mapping"].status == "failed"
    assert "隔离率" in (nodes["mapping"].error or "")
    assert nodes["objects"].status == "stale"
    assert "对象层仍服务旧版本" in nodes["objects"].status_reason
    assert nodes["mapping"].run_id is not None


def test_pipeline_get_has_no_side_effects(env):
    landing, _cfg = env
    client = _client(env)
    before = landing.con.execute("SELECT COUNT(*) FROM d2a_audit_log").fetchone()[0]
    client.get("/api/pipeline")
    after = landing.con.execute("SELECT COUNT(*) FROM d2a_audit_log").fetchone()[0]
    assert before == after
