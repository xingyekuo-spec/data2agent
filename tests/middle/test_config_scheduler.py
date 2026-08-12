"""抽取框架 E5 测试:配置解析、错峰窗口、批次边界暂停、调度周期。"""

import sys
import types
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.shared.config import (
    ConnectConfig,
    SourceConfig,
    assert_production_ready,
    in_window,
    load_config,
    parse_duration_seconds,
)
from data2agent.middle.extract.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore
from tests.helpers import whitelist_from_pack
from data2agent.shared.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"


# ---- 配置与窗口 ----

def test_parse_duration():
    assert parse_duration_seconds("30m") == 1800
    assert parse_duration_seconds("3d") == 259200
    assert parse_duration_seconds(90) == 90
    with pytest.raises(ValueError, match="无法解析时长"):
        parse_duration_seconds("半小时")


def test_in_window_normal_and_overnight():
    assert in_window(dtime(23, 0), ["22:00-06:30"]), "跨零点窗口:夜里在窗口内"
    assert in_window(dtime(6, 0), ["22:00-06:30"])
    assert not in_window(dtime(12, 0), ["22:00-06:30"])
    assert in_window(dtime(12, 30), ["12:00-13:00"])
    assert not in_window(dtime(13, 0), ["12:00-13:00"]), "窗口右开"
    assert in_window(dtime(12, 0), []), "空窗口 = 不限"


def test_load_config(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "landing: l.sqlite\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        "    path: s.sqlite\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n"
        "    windows: [\"22:00-06:30\"]\n"
        "    lookback: 2d\n"
        "    sync_every: 15m\n",
        encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.state_db == "l.sqlite"  # 旧 landing 只读兼容
    s = cfg.sources["digiwin_e10"]
    assert s.lookback_days() == 2 and s.sync_every_seconds() == 900


def test_production_rejects_local_sink_and_uncontrolled_spool():
    cfg = ConnectConfig(
        deployment_mode="production",
        sources={"e10": SourceConfig(
            adapter="sqlite_readonly", path="source.sqlite", tables={},
        )},
    )
    violations = cfg.production_violations()
    assert any("sink.type=http" in item for item in violations)
    assert any("temporary_file" in item for item in violations)
    with pytest.raises(ValueError, match="生产配置未就绪"):
        assert_production_ready(cfg)


def test_strict_stream_rejects_file_spool_only_options():
    base = {
        "adapter": "sqlite_readonly",
        "path": "source.sqlite",
        "tables": {},
        "spool": {"policy": "strict_stream"},
    }
    assert SourceConfig.model_validate(base).spool.policy == "strict_stream"

    with pytest.raises(ValueError, match="directory"):
        SourceConfig.model_validate({
            **base,
            "spool": {"policy": "strict_stream", "directory": "spool"},
        })
    with pytest.raises(ValueError, match="encrypted_at_rest"):
        SourceConfig.model_validate({
            **base,
            "spool": {"policy": "strict_stream", "encrypted_at_rest": True},
        })


def test_state_db_and_legacy_landing_must_not_diverge(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "state_db: new.sqlite\nlanding: old.sqlite\n"
        "sources:\n  e10:\n    adapter: sqlite_readonly\n"
        "    path: x\n    tables: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="值不一致"):
        load_config(cfg_file)


def test_load_config_sync_start_at(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "landing: l.sqlite\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        "    path: s.sqlite\n"
        "    tables: {}\n"
        "    sync_every: 1d\n"
        "    sync_start_at: \"02:00\"\n",
        encoding="utf-8")
    cfg = load_config(cfg_file)
    s = cfg.sources["digiwin_e10"]
    assert s.sync_start_at == "02:00"
    assert s.sync_start_datetime_after(
        datetime(2026, 7, 26, 1, 30)) == datetime(2026, 7, 26, 2, 0)
    assert s.sync_start_datetime_after(
        datetime(2026, 7, 26, 3, 0)) == datetime(2026, 7, 27, 2, 0)


def test_config_rejects_bad_sync_start_at(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
        "    tables: {}\n"
        "    sync_start_at: 深夜\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sync_start_at"):
        load_config(cfg_file)


def test_config_rejects_mssql_without_dsn_env(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "sources:\n  e10:\n    adapter: mssql_readonly\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dsn_env"):
        load_config(cfg_file)


def test_config_accepts_reconcile_at_in_push_mode(tmp_path):
    """E6b 已实现后，推送模式允许安排 L1/deep 跨机对账。"""
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "sources:\n  e10:\n    adapter: mssql_readonly\n    dsn_env: D2A_E10_DSN\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n"
        "    reconcile_at: \"05:30\"\n"
        "    reconcile_deep_at: \"02:10\"\n"
        "    sink: { type: http, url: \"http://platform:8850\", "
        "token_env: D2A_INGEST_TOKEN, allow_insecure_http: true }\n",
        encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.sources["e10"].reconcile_at == "05:30"
    assert cfg.sources["e10"].reconcile_deep_at == "02:10"


def test_config_rejects_bad_window(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: UPD\n"
        "    windows: [\"深夜到清晨\"]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="窗口格式"):
        load_config(cfg_file)


@pytest.mark.parametrize("fragment, message", [
    ("sync_every: 0s", "sync_every"),
    ("windows: [\"02:00-02:00\"]", "起止相同"),
    ("rate: {batch_size: 50001, rows_per_second: 1}", "less than or equal"),
    ("rate: {batch_size: 1, rows_per_second: 0}", "greater than or equal"),
])
def test_config_rejects_unsafe_schedule_and_rate_bounds(
    tmp_path, fragment, message,
):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
        "    tables: {}\n    " + fragment + "\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_config(cfg_file)


# ---- 批次边界暂停 ----

@pytest.fixture(scope="module")
def pack():
    return load_pack(ROOT / "templates")


@pytest.fixture()
def env(tmp_path, pack):
    src = tmp_path / "source.sqlite"
    write_db(src, build(seed=42, asof=date(2026, 7, 10)))
    return src, LandingStore(tmp_path / "landing.sqlite")


def test_pause_at_batch_boundary_then_resume(env, pack):
    src, landing = env
    adapter = SqliteReadOnlyAdapter(str(src), whitelist_from_pack(pack, SOURCE),
                                    batch_size=50)
    budget = iter([True] * 3 + [False] * 100)  # 三次检查后"窗口关闭"
    report = incremental_sync(adapter, landing, SOURCE,
                              watermarks_from_pack(pack, SOURCE),
                              should_continue=lambda: next(budget))
    assert report.paused, "窗口越界应优雅暂停"
    run = landing.con.execute(
        "SELECT status FROM d2a_sync_run ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "paused"
    done_tables = {t.table for t in report.tables}
    assert "SALES_ORDER_D" not in done_tables, "后续表不应开始"
    assert landing.get_high_water(SOURCE, "SALES_ORDER_D") is None, "未完成表不得有水位"

    report2 = incremental_sync(SqliteReadOnlyAdapter(
        str(src), whitelist_from_pack(pack, SOURCE)), landing, SOURCE,
        watermarks_from_pack(pack, SOURCE))  # 下窗口续跑
    assert not report2.paused
    assert landing.count(SOURCE, "SALES_ORDER_D") == 239, "续跑后数据完整"
    assert landing.get_high_water(SOURCE, "SALES_ORDER_D") is not None


# ---- 调度周期 ----

def test_run_sync_cycle_respects_window(env, pack, tmp_path, monkeypatch):
    from data2agent.middle.extract import scheduler as sched
    from data2agent.shared.config import SourceConfig

    from datetime import datetime, timedelta

    src, landing = env
    # 取"从现在起 2~3 小时后"的窗口:任何时刻跑测试都必然在窗口外(含跨零点)
    t2, t3 = datetime.now() + timedelta(hours=2), datetime.now() + timedelta(hours=3)

        # 六张基线表加上已核对的呆滞库存输入表
    baseline_tables = {
        "CUSTOMER": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
        "CURRENCY": {"mode": "full_refresh"},
        "ITEM": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
        "QUOTATION": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
        "SALES_ORDER": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
        "SALES_ORDER_D": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
        "ITEM_WAREHOUSE": {"mode": "incremental", "watermark": "LAST_MODIFIED_DATE"},
    }

    scfg = SourceConfig(adapter="sqlite_readonly", path=str(src),
                        tables=baseline_tables,
                        windows=[f"{t2:%H:%M}-{t3:%H:%M}"])
    # run_sync_cycle 现在返回 SyncCycleResult,不再是布尔值
    result = sched.run_sync_cycle(SOURCE, scfg, landing.db_path)
    assert result.executed is False, f"窗口外不发起, got reason={result.reason}"

    scfg_open = SourceConfig(adapter="sqlite_readonly", path=str(src),
                             tables=baseline_tables)
    result = sched.run_sync_cycle(SOURCE, scfg_open, landing.db_path)
    assert result.executed is True, f"应执行, got reason={result.reason}"
    assert landing.count(SOURCE, "SALES_ORDER") == 97
    pub = landing.get_published_dataset(SOURCE)
    assert pub is not None and pub.status == "published", "apply_after_sync 应自动发布数据集"


def test_serve_once(env, pack, tmp_path):
    pytest.importorskip("apscheduler")
    from data2agent.shared.config import load_config
    from data2agent.middle.extract.scheduler import serve

    src, _ = env
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {tmp_path / 'serve_landing.sqlite'}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    tables:\n"
        "      CUSTOMER:\n"
        "        mode: incremental\n"
        "        watermark: LAST_MODIFIED_DATE\n"
        "      CURRENCY:\n"
        "        mode: full_refresh\n"
        "      ITEM:\n"
        "        mode: incremental\n"
        "        watermark: LAST_MODIFIED_DATE\n"
        "      QUOTATION:\n"
        "        mode: incremental\n"
        "        watermark: LAST_MODIFIED_DATE\n"
        "      SALES_ORDER:\n"
        "        mode: incremental\n"
        "        watermark: LAST_MODIFIED_DATE\n"
            "      SALES_ORDER_D:\n"
            "        mode: incremental\n"
            "        watermark: LAST_MODIFIED_DATE\n"
            "      ITEM_WAREHOUSE:\n"
            "        mode: incremental\n"
            "        watermark: LAST_MODIFIED_DATE\n"
            "    reconcile_at: \"05:30\"\n",
        encoding="utf-8")
    serve(load_config(cfg_file), once=True)
    landing = LandingStore(tmp_path / "serve_landing.sqlite")
    assert landing.count(SOURCE, "QUOTATION") == 180
    runs = landing.con.execute("SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0]
    assert runs >= 3, "一轮 serve --once 应含 sync + apply + reconcile"


def test_serve_schedules_first_run_at_sync_start_at(tmp_path, monkeypatch):
    """自动调度应把首轮锚定到 sync_start_at，而不是服务启动即跑。"""
    captured = []

    class FakeBlockingScheduler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def add_job(self, func, trigger, args=(), id=None,
                    max_instances=None, coalesce=None, next_run_time=None):
            captured.append({
                "id": id,
                "func": func.__name__,
                "seconds": trigger.seconds,
                "next_run_time": next_run_time,
                "max_instances": max_instances,
                "coalesce": coalesce,
            })

        def start(self):
            return None

    class FakeIntervalTrigger:
        def __init__(self, seconds):
            self.seconds = seconds

    class FakeCronTrigger:
        def __init__(self, hour, minute):
            self.hour = hour
            self.minute = minute

    monkeypatch.setitem(sys.modules, "apscheduler", types.ModuleType("apscheduler"))
    monkeypatch.setitem(
        sys.modules, "apscheduler.schedulers",
        types.ModuleType("apscheduler.schedulers"))
    monkeypatch.setitem(
        sys.modules, "apscheduler.triggers",
        types.ModuleType("apscheduler.triggers"))
    monkeypatch.setitem(
        sys.modules, "apscheduler.schedulers.blocking",
        types.SimpleNamespace(BlockingScheduler=FakeBlockingScheduler))
    monkeypatch.setitem(
        sys.modules, "apscheduler.triggers.interval",
        types.SimpleNamespace(IntervalTrigger=FakeIntervalTrigger))
    monkeypatch.setitem(
        sys.modules, "apscheduler.triggers.cron",
        types.SimpleNamespace(CronTrigger=FakeCronTrigger))

    from data2agent.middle.extract.scheduler import serve

    cfg = ConnectConfig(
        landing=str(tmp_path / "landing.sqlite"),
        sources={
            SOURCE: SourceConfig(
                adapter="sqlite_readonly",
                path=str(tmp_path / "source.sqlite"),
                tables={},
                sync_every="1d",
                sync_start_at="02:00",
            )
        },
    )
    before = datetime.now()
    serve(cfg)

    assert len(captured) == 1
    job = captured[0]
    assert job["id"] == f"sync:{SOURCE}"
    assert job["func"] == "run_sync_cycle"
    assert job["seconds"] == 86400
    assert job["max_instances"] == 1
    assert job["coalesce"] is True
    assert job["next_run_time"] >= before
    assert job["next_run_time"].time() == dtime(2, 0)
