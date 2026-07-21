"""观测聚合内核测试(M3-T03):原语 + 七节点状态矩阵。

状态必须由结构化事实计算;不解析日志 / SQL / detail 关键词。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from data2agent.connect.config import load_config  # noqa: E402
from data2agent.connect.landing import LandingStore  # noqa: E402
from data2agent.console import observability as obs  # noqa: E402
from data2agent.metamodel.dataset_publish_contract import make_build_table  # noqa: E402
from data2agent.metamodel.loader import load_pack  # noqa: E402
from data2agent.metamodel.versioning import (  # noqa: E402
    DatasetVersionRecord,
    ObjectVersionRecord,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "digiwin_e10"
NOW = obs.aware("2026-07-18T12:00:00")
FRESH = "2026-07-18T11:30:00"   # 阈值 1h 内
STALE = "2026-07-18T09:00:00"   # 阈值 1h 外


@pytest.fixture()
def db(tmp_path):
    return LandingStore(tmp_path / "landing.sqlite")


@pytest.fixture()
def pack():
    return load_pack(ROOT / "templates")


def _cfg(tmp_path, sink="local", sync_every="30m"):
    text = (
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {tmp_path / 'landing.sqlite'}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {tmp_path / 'src.sqlite'}\n"
        f"    sync_every: {sync_every}\n"
    )
    if sink == "http":
        text += ('    sink: { type: http, url: "http://127.0.0.1:8850",'
                 " token_env: D2A_INGEST_TOKEN }\n")
    path = tmp_path / "connect.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _run(db, run_type, status, started, finished=None, rows=10, detail=""):
    db.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, finished_at, tables, rows,"
        " status, detail, run_type) VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
        (SOURCE, started, finished, rows, status, detail, run_type))
    db.con.commit()


def _mk_raw(db, table, n, extracted_at):
    db.con.execute(
        f'CREATE TABLE "raw_{SOURCE}__{table}" '
        '("K" TEXT PRIMARY KEY, "_d2a_extracted_at" TEXT, "_d2a_deleted_at" TEXT)')
    db.con.executemany(
        f'INSERT INTO "raw_{SOURCE}__{table}" VALUES (?, ?, NULL)',
        [(f"k{i}", extracted_at) for i in range(n)])
    db.con.commit()


def _mk_obj(db, pack, name, n, mapped_at, *, version="ds-obs-1"):
    """写入 published 快照物理表(不创建遗留 obj_*)。"""
    table = make_build_table(SOURCE, name, f"{abs(hash(version + name)):012x}"[:12])
    db.con.execute(
        f'CREATE TABLE IF NOT EXISTS "{table}" '
        '("K" TEXT PRIMARY KEY, "_d2a_mapped_at" TEXT)')
    db.con.execute(f'DELETE FROM "{table}"')
    db.con.executemany(
        f'INSERT INTO "{table}" VALUES (?, ?)',
        [(f"k{i}", mapped_at) for i in range(n)])
    existing = db.get_dataset_version(version)
    if existing is None:
        db.insert_dataset_version(
            DatasetVersionRecord(
                dataset_version=version,
                source=SOURCE,
                template_version=pack.version,
                status="published",
                built_at=mapped_at,
                published_at=mapped_at,
                object_manifest=f'["{name}"]',
                template_snapshot=pack.model_dump_json(),
            )
        )
    else:
        # 追加对象到同一 published 清单(测试场景通常单对象)
        pass
    # upsert object row
    db.con.execute(
        "DELETE FROM d2a_object_version WHERE dataset_version = ? AND object = ?",
        (version, name),
    )
    db.insert_object_version(
        ObjectVersionRecord(
            dataset_version=version,
            object=name,
            object_version=f"{version}-{name}",
            binding_hash="sha256:" + "ab" * 32,
            row_count=n,
            build_table=table,
            status="published",
            built_at=mapped_at,
            published_at=mapped_at,
        )
    )
    db.con.commit()
    return table


def _mk_quarantine(db, obj, n):
    db.con.executemany(
        "INSERT INTO d2a_quarantine (source, object, keys_json, reason, created_at) "
        "VALUES (?, ?, '{}', '测试原因', '2026-07-18T11:00:00')",
        [(SOURCE, obj)] * n)
    db.con.commit()


def _mk_ingest(db, ts, rows=5):
    db.con.execute(
        "INSERT INTO d2a_audit_log (ts, source, action, sql, rows, duration_ms, batch_id) "
        "VALUES (?, ?, 'ingest', 'batch b1 → raw', ?, 0.0, 'b1')", (ts, SOURCE, rows))
    db.con.commit()


def _nodes(db, pack, cfg, probes=None):
    return {n["node"]: n for n in obs.compute_nodes(db, pack, cfg, SOURCE,
                                                    probes=probes or {}, now=NOW)}


# ---- 原语 ----


def test_aware_naive_and_offset_and_invalid():
    assert obs.aware("2026-07-18T11:30:00").tzinfo is not None
    assert obs.aware("2026-07-18T11:30:00+08:00").tzinfo is not None
    assert obs.aware("不是时间") is None
    assert obs.aware(None) is None
    assert obs.aware(123) is None


def test_freshness_threshold():
    assert obs.freshness_threshold("30m") == timedelta(seconds=3600)
    assert obs.freshness_threshold("2m") == timedelta(seconds=300)  # 下限 300s
    assert obs.freshness_threshold("不是时长") is None
    assert obs.freshness_threshold(None) is None


def test_is_stale_boundary():
    now = obs.aware("2026-07-18T12:00:00")
    threshold = timedelta(hours=1)
    assert obs.is_stale(obs.aware("2026-07-18T11:30:00"), threshold, now) is False
    assert obs.is_stale(obs.aware("2026-07-18T09:00:00"), threshold, now) is True
    assert obs.is_stale(None, threshold, now) is False


def test_safe_error_summary():
    assert obs.safe_error_summary("  多行\n错误\t摘要  ") == "多行 错误 摘要"
    assert obs.safe_error_summary("x" * 600) == "x" * 500
    assert obs.safe_error_summary("") is None
    assert obs.safe_error_summary(None) is None


@pytest.mark.parametrize(("statuses", "expected"), [
    (["healthy", "healthy"], "healthy"),
    (["healthy", "warning"], "warning"),
    (["warning", "failed"], "failed"),
    (["healthy", "unknown"], "unknown"),   # unknown 不得被 healthy 覆盖
    (["running", "unknown"], "running"),
    (["idle", "healthy"], "idle"),
    (["stale", "warning"], "stale"),
    ([], "unknown"),
])
def test_fold_status(statuses, expected):
    assert obs.fold_status(statuses) == expected


# ---- erp / extract:sync run 证据 ----


def test_erp_extract_idle_when_never_run(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["erp"]["status"] == "idle"
    assert nodes["extract"]["status"] == "idle"


def test_erp_extract_running(db, pack, tmp_path):
    _run(db, "sync", "running", FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["erp"]["status"] == "running"
    assert nodes["extract"]["status"] == "running"
    assert nodes["extract"]["run_id"] is not None


def test_erp_extract_failed(db, pack, tmp_path):
    _run(db, "sync", "failed", FRESH, FRESH, detail="连接超时")
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["erp"]["status"] == "failed"
    assert nodes["erp"]["error"] == "连接超时"
    assert nodes["erp"]["last_failure_at"] is not None


def test_erp_extract_healthy_and_stale(db, pack, tmp_path):
    _run(db, "sync", "ok", FRESH, FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["erp"]["status"] == "healthy"
    assert nodes["extract"]["status"] == "healthy"

    db2 = LandingStore(tmp_path / "db2.sqlite")
    _run(db2, "sync", "ok", STALE, STALE)
    nodes2 = _nodes(db2, pack, _cfg(tmp_path))
    assert nodes2["erp"]["status"] == "stale"
    assert nodes2["extract"]["status"] == "stale"


def test_erp_extract_unknown_for_untyped_legacy_runs(db, pack, tmp_path):
    db.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, status) VALUES (?, ?, 'ok')",
        (SOURCE, FRESH))  # run_type 为 NULL(老库)
    db.con.commit()
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["erp"]["status"] == "unknown"
    assert nodes["extract"]["status"] == "unknown"


def test_erp_extract_unknown_when_platform_cannot_see_middle(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path, sink="http"))
    assert nodes["erp"]["status"] == "unknown"
    assert "中间机" in nodes["erp"]["status_reason"]


# ---- push ----


def test_push_idle_for_local_sink(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["push"]["status"] == "idle"
    assert "本地直写" in nodes["push"]["status_reason"]


def test_push_unknown_when_no_batch_and_unprobeable(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path, sink="http"))
    assert nodes["push"]["status"] == "unknown"


def test_push_failed_when_ingest_down(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path, sink="http"),
                   probes={"ingest": lambda: (False, "http")})
    assert nodes["push"]["status"] == "failed"


def test_push_healthy_and_stale_by_batch(db, pack, tmp_path):
    _mk_ingest(db, FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path, sink="http"),
                   probes={"ingest": lambda: (True, "http")})
    assert nodes["push"]["status"] == "healthy"

    db2 = LandingStore(tmp_path / "db2.sqlite")
    _mk_ingest(db2, STALE)
    nodes2 = _nodes(db2, pack, _cfg(tmp_path, sink="http"),
                    probes={"ingest": lambda: (True, "http")})
    assert nodes2["push"]["status"] == "stale"


# ---- raw ----


def test_raw_idle_when_empty_and_never_run(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["raw"]["status"] == "idle"


def test_raw_healthy_and_stale(db, pack, tmp_path):
    _mk_raw(db, "T1", 7, FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["raw"]["status"] == "healthy"
    assert nodes["raw"]["rows_in"] == 7

    db2 = LandingStore(tmp_path / "db2.sqlite")
    _mk_raw(db2, "T1", 3, STALE)
    nodes2 = _nodes(db2, pack, _cfg(tmp_path))
    assert nodes2["raw"]["status"] == "stale"


# ---- mapping ----


def test_mapping_idle_when_never_applied(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["mapping"]["status"] == "idle"


def test_mapping_failed(db, pack, tmp_path):
    _run(db, "apply", "failed", FRESH, FRESH, detail="隔离率 53% 超过阈值")
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["mapping"]["status"] == "failed"
    assert "隔离率" in nodes["mapping"]["error"]


def test_mapping_warning_for_draft_and_quarantine(db, pack, tmp_path):
    _run(db, "apply", "ok", FRESH, FRESH)
    _mk_quarantine(db, "Customer", 2)
    nodes = _nodes(db, pack, _cfg(tmp_path))
    # 模板 binding 全部 draft + 2 行隔离 → warning(不是 healthy)
    assert nodes["mapping"]["status"] == "warning"
    assert "draft" in nodes["mapping"]["status_reason"]
    assert "2 行待处理隔离" in nodes["mapping"]["status_reason"]


def test_mapping_healthy_when_verified_and_clean(db, pack, tmp_path):
    _run(db, "apply", "ok", FRESH, FRESH)
    for tpl in pack.objects:  # 全部置为 verified
        for b in tpl.bindings:
            b.status = "verified"
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["mapping"]["status"] == "healthy"


# ---- objects ----


def test_objects_idle_when_not_materialized(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["objects"]["status"] == "idle"


def test_objects_healthy_when_in_sync(db, pack, tmp_path):
    _mk_raw(db, "CUSTOMER", 5, FRESH)
    _mk_obj(db, pack, "Customer", 5, FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["objects"]["status"] == "healthy"


def test_objects_stale_when_raw_newer(db, pack, tmp_path):
    _mk_raw(db, "CUSTOMER", 6, FRESH)
    _mk_obj(db, pack, "Customer", 5, STALE)   # raw 新于对象
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["objects"]["status"] == "stale"
    assert "对象层仍服务旧版本" in nodes["objects"]["status_reason"]


def test_objects_stale_when_apply_failed_but_old_kept(db, pack, tmp_path):
    _mk_obj(db, pack, "Customer", 5, FRESH)
    _run(db, "apply", "failed", FRESH, FRESH, detail="熔断")
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["objects"]["status"] == "stale"
    assert "对象层仍服务旧版本" in nodes["objects"]["status_reason"]


# ---- mcp ----


def test_mcp_unknown_without_probe(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["mcp"]["status"] == "unknown"


def test_mcp_failed_when_down(db, pack, tmp_path):
    nodes = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (False, "tcp")})
    assert nodes["mcp"]["status"] == "failed"


def test_mcp_healthy_and_stale_follows_objects(db, pack, tmp_path):
    _mk_obj(db, pack, "Customer", 5, FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (True, "http")})
    assert nodes["mcp"]["status"] == "healthy"

    _mk_raw(db, "CUSTOMER", 6, obs.aware("2026-07-18T11:59:00").isoformat())
    nodes2 = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (True, "http")})
    # 服务 200 但对象陈旧:stale,服务健康不等于数据健康
    assert nodes2["mcp"]["status"] == "stale"
    assert nodes2["objects"]["status"] == "stale"


# ---- 整体 ----


def test_pipeline_fixed_seven_nodes_and_overall(db, pack, tmp_path):
    result = obs.build_pipeline(db, pack, _cfg(tmp_path), SOURCE, now=NOW)
    assert [n["node"] for n in result["nodes"]] == list(obs.NODE_IDS)
    assert len(result["nodes"]) == 7
    # 从未运行:全 idle;overall 不得为 healthy
    assert result["overall_status"] in ("idle", "unknown")
    # unknown 存在时总体不为 healthy
    result2 = obs.build_pipeline(db, pack, _cfg(tmp_path, sink="http"), SOURCE, now=NOW)
    statuses = {n["status"] for n in result2["nodes"]}
    if "unknown" in statuses:
        assert result2["overall_status"] != "healthy"


def test_apply_circuit_broken_two_nodes_locatable(db, pack, tmp_path):
    """apply 熔断:映射 failed 与对象层 stale 必须同时可定位。"""
    _mk_raw(db, "CUSTOMER", 10, FRESH)
    _mk_obj(db, pack, "Customer", 5, STALE)
    _run(db, "apply", "failed", FRESH, FRESH, detail="Customer 隔离率超过阈值,apply 中止")
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["mapping"]["status"] == "failed"
    assert nodes["objects"]["status"] == "stale"
    assert "对象层仍服务旧版本" in nodes["objects"]["status_reason"]


# ---- 复审回归:异常即 unknown、按源过滤、独立成功/失败、耗时与版本 ----


def test_sqlite_failure_is_unknown_not_idle(db, pack, tmp_path):
    """SQLite 查询异常必须降级为 unknown,不得解释为 idle(从未运行/没有数据)。"""
    db.con.close()  # 关闭连接:后续查询全部抛 ProgrammingError
    nodes = _nodes(db, pack, _cfg(tmp_path))
    for node_id in ("erp", "extract", "raw", "mapping", "objects"):
        assert nodes[node_id]["status"] == "unknown", node_id
        assert "查询失败" in nodes[node_id]["status_reason"]
    # push(local sink 不查库,按设计 idle)与 mcp(无探测)不受影响
    assert nodes["push"]["status"] == "idle"
    assert nodes["mcp"]["status"] == "unknown"


def test_run_facts_filter_by_source(db, pack, tmp_path):
    """跨源 run 记录不得混用:source_b 的成功不能给 source_a 贴金。"""
    _run(db, "sync", "ok", FRESH, FRESH)                      # digiwin_e10 成功
    db.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, finished_at, tables, rows,"
        " status, detail, run_type) VALUES ('source_b', ?, ?, 1, 10, 'failed', '炸', 'sync')",
        ("2026-07-18T11:59:00", "2026-07-18T11:59:30"))
    db.con.commit()
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["erp"]["status"] == "healthy"  # 用的是本源的 ok,不是 source_b 的 failed
    assert nodes["erp"]["last_failure_at"] is None

    # 换成 source_b 视角:必须看到它自己的 failed
    nodes_b = {n["node"]: n for n in obs.compute_nodes(
        db, pack, _cfg(tmp_path), "source_b", now=NOW)}
    assert nodes_b["erp"]["status"] == "failed"


def test_last_success_and_failure_are_independent(db, pack, tmp_path):
    """最近成功与最近失败分别查询:最新失败不吞掉更早的成功。"""
    _run(db, "sync", "ok", STALE, STALE)
    _run(db, "sync", "failed", FRESH, FRESH, detail="炸了")
    nodes = _nodes(db, pack, _cfg(tmp_path))
    erp = nodes["erp"]
    assert erp["status"] == "failed"
    assert erp["last_success_at"] is not None   # 更早的成功仍在
    assert erp["last_failure_at"] is not None
    assert erp["last_failure_at"] > erp["last_success_at"]

    # 正在运行的 run 不是失败,也不覆盖 last_failure_at
    db2 = LandingStore(tmp_path / "db2.sqlite")
    _run(db2, "sync", "ok", STALE, STALE)
    _run(db2, "sync", "failed", "2026-07-18T10:30:00", "2026-07-18T10:30:30", detail="旧失败")
    _run(db2, "sync", "running", FRESH)
    erp2 = _nodes(db2, pack, _cfg(tmp_path))["erp"]
    assert erp2["status"] == "running"
    assert erp2["last_success_at"] is not None
    assert erp2["last_failure_at"] is not None  # 是旧失败,不是 running 这条


def test_objects_failed_mcp_not_green(db, pack, tmp_path):
    """apply 失败且无对象表:objects=failed 时 MCP 不得显示 healthy。"""
    _run(db, "apply", "failed", FRESH, FRESH, detail="熔断")
    nodes = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (True, "http")})
    assert nodes["mapping"]["status"] == "failed"
    assert nodes["objects"]["status"] == "failed"
    assert nodes["mcp"]["status"] == "failed"
    assert "对象层构建失败" in nodes["mcp"]["status_reason"]


def test_duration_and_version_filled(db, pack, tmp_path):
    """已知节点填充耗时与可检测版本。"""
    _run(db, "sync", "ok", "2026-07-18T11:00:00", "2026-07-18T11:00:05")
    _run(db, "apply", "ok", "2026-07-18T11:01:00", "2026-07-18T11:01:03")
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["extract"]["duration_ms"] == pytest.approx(5000, abs=1)
    assert nodes["mapping"]["duration_ms"] == pytest.approx(3000, abs=1)
    assert nodes["mapping"]["version"] == pack.version


# ---- 第三轮复审回归 ----


def test_orphaned_running_does_not_override_newer_success(db, pack, tmp_path):
    """崩溃遗留的旧 running 不得覆盖更新的成功,也不得产生数年耗时。"""
    _run(db, "sync", "running", "2020-01-01T00:00:00")   # 崩溃遗留
    _run(db, "sync", "ok", FRESH, FRESH)                 # 更新的成功
    erp = _nodes(db, pack, _cfg(tmp_path))["erp"]
    assert erp["status"] == "healthy"                    # 不是 running
    assert erp["last_success_at"] is not None
    assert erp["duration_ms"] == pytest.approx(0, abs=1000 * 60 * 60 * 24)


def test_paused_is_warning_not_failure(db, pack, tmp_path):
    """错峰窗口暂停是合法状态:映射 warning,不计入失败历史。"""
    _run(db, "sync", "paused", FRESH, FRESH)
    erp = _nodes(db, pack, _cfg(tmp_path))["erp"]
    assert erp["status"] == "warning"
    assert "暂停" in erp["status_reason"]
    assert erp["last_failure_at"] is None
    # 后续成功不受 paused 影响
    _run(db, "sync", "ok", "2026-07-18T11:55:00", "2026-07-18T11:55:05")
    erp2 = _nodes(db, pack, _cfg(tmp_path))["erp"]
    assert erp2["status"] == "healthy"
    assert erp2["last_failure_at"] is None


def test_success_without_threshold_is_unknown_not_healthy(db, pack, tmp_path):
    """缺少 sync_every 配置时不猜阈值:2020 年的成功也不能显示 healthy。"""
    _run(db, "sync", "ok", "2020-01-01T00:00:00", "2020-01-01T00:00:05")
    _mk_raw(db, "CUSTOMER", 5, "2020-01-01T00:00:00")
    nodes = {n["node"]: n for n in obs.compute_nodes(db, pack, None, SOURCE, now=NOW)}
    assert nodes["erp"]["status"] == "unknown"
    assert "sync_every" in nodes["erp"]["status_reason"]
    assert nodes["raw"]["status"] == "unknown"


def test_objects_unknown_when_raw_undetectable(db, pack, tmp_path):
    """raw 查询失败时,对象层不能自称"与 raw 同步";MCP 传播 unknown。"""
    _mk_obj(db, pack, "Customer", 5, FRESH)
    db.con.execute('CREATE TABLE "raw_digiwin_e10__BROKEN" ("K" TEXT PRIMARY KEY)')
    db.con.commit()  # 缺元数据列,raw 查询必炸
    nodes = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (True, "http")})
    assert nodes["raw"]["status"] == "unknown"
    assert nodes["objects"]["status"] == "unknown"
    assert nodes["mcp"]["status"] == "unknown"
    assert nodes["mcp"]["status"] != "idle"


# ---- 第四轮复审回归:组合场景 ----


def test_objects_propagate_unknown_when_raw_time_missing(db, pack, tmp_path):
    """raw 缺抽取时间 → raw unknown → objects/mcp 不得为 healthy。"""
    db.con.execute(
        f'CREATE TABLE "raw_{SOURCE}__T1" '
        '("K" TEXT PRIMARY KEY, "_d2a_extracted_at" TEXT, "_d2a_deleted_at" TEXT)')
    db.con.execute(f'INSERT INTO "raw_{SOURCE}__T1" VALUES (\'k0\', NULL, NULL)')
    db.con.commit()
    _mk_obj(db, pack, "Customer", 5, FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (True, "http")})
    assert nodes["raw"]["status"] == "unknown"
    assert nodes["objects"]["status"] == "unknown"
    assert nodes["mcp"]["status"] == "unknown"


def test_objects_propagate_unknown_when_threshold_missing(db, pack, tmp_path):
    """raw 缺新鲜度阈值 → raw unknown → objects/mcp 不得为 healthy。"""
    _mk_raw(db, "CUSTOMER", 5, FRESH)
    _mk_obj(db, pack, "Customer", 5, FRESH)
    nodes = {n["node"]: n for n in obs.compute_nodes(
        db, pack, None, SOURCE, probes={"mcp": lambda: (True, "http")}, now=NOW)}
    assert nodes["raw"]["status"] == "unknown"
    assert nodes["objects"]["status"] == "unknown"
    assert nodes["mcp"]["status"] == "unknown"


def test_objects_propagate_unknown_when_apply_type_legacy(db, pack, tmp_path):
    """legacy run 无 run_type → mapping unknown → objects/mcp 不得为 healthy。"""
    _mk_obj(db, pack, "Customer", 5, FRESH)
    db.con.execute(
        "INSERT INTO d2a_sync_run (source, started_at, finished_at, status) "
        "VALUES (?, ?, ?, 'ok')", (SOURCE, FRESH, FRESH))  # run_type 为 NULL
    db.con.commit()
    nodes = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (True, "http")})
    assert nodes["mapping"]["status"] == "unknown"
    assert nodes["objects"]["status"] == "unknown"
    assert nodes["mcp"]["status"] == "unknown"


def test_apply_paused_is_not_failure_for_objects(db, pack, tmp_path):
    """apply paused:对象层仍服务旧完整版本(stale),不是 apply 失败或部分更新。"""
    _mk_obj(db, pack, "Customer", 5, FRESH)
    _run(db, "apply", "paused", FRESH, FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path), probes={"mcp": lambda: (True, "http")})
    assert nodes["mapping"]["status"] == "warning"
    assert nodes["objects"]["status"] == "stale"
    assert "对象层仍服务旧版本" in nodes["objects"]["status_reason"]
    assert "部分更新" not in nodes["objects"]["status_reason"]
    assert "apply 失败" not in nodes["objects"]["status_reason"]
    assert nodes["mcp"]["status"] == "stale"


def test_apply_running_is_not_failure_for_objects(db, pack, tmp_path):
    """apply running:对象层仍服务旧完整版本,不是失败或部分更新。"""
    _mk_obj(db, pack, "Customer", 5, FRESH)
    _run(db, "apply", "running", FRESH)
    nodes = _nodes(db, pack, _cfg(tmp_path))
    assert nodes["mapping"]["status"] == "running"
    assert nodes["objects"]["status"] == "stale"
    assert "对象层仍服务旧版本" in nodes["objects"]["status_reason"]
    assert "构建进行中" in nodes["objects"]["status_reason"]
    assert "部分更新" not in nodes["objects"]["status_reason"]
    assert "apply 失败" not in nodes["objects"]["status_reason"]


def test_push_unknown_when_threshold_unparseable(db, pack, tmp_path):
    """sync_every 无法解析时,旧 ingest 批次 + 健康探测不得变绿。"""
    _mk_ingest(db, "2020-01-01T00:00:00")
    cfg = _cfg(tmp_path, sink="http", sync_every="不是时长")
    nodes = _nodes(db, pack, cfg, probes={"ingest": lambda: (True, "http")})
    assert nodes["push"]["status"] == "unknown"
    assert "sync_every" in nodes["push"]["status_reason"]
