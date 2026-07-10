"""抽取框架 E5 测试:配置解析、错峰窗口、批次边界暂停、调度周期。"""

from datetime import date
from datetime import time as dtime
from pathlib import Path

import pytest

from data2agent.connect.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.connect.config import (
    in_window,
    load_config,
    parse_duration_seconds,
)
from data2agent.connect.increment import incremental_sync, watermarks_from_pack
from data2agent.connect.landing import LandingStore
from data2agent.connect.sync import whitelist_from_pack
from data2agent.metamodel.loader import load_pack
from data2agent.showroom.seed import build, write_db

ROOT = Path(__file__).resolve().parents[1]
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
        "    windows: [\"22:00-06:30\"]\n"
        "    lookback: 2d\n"
        "    sync_every: 15m\n",
        encoding="utf-8")
    cfg = load_config(cfg_file)
    s = cfg.sources["digiwin_e10"]
    assert s.lookback_days() == 2 and s.sync_every_seconds() == 900


def test_config_rejects_mssql_without_dsn_env(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "sources:\n  e10:\n    adapter: mssql_readonly\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dsn_env"):
        load_config(cfg_file)


def test_config_rejects_bad_window(tmp_path):
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        "sources:\n  e10:\n    adapter: sqlite_readonly\n    path: x\n"
        "    windows: [\"深夜到清晨\"]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="窗口格式"):
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
    from data2agent.connect import scheduler as sched
    from data2agent.connect.config import SourceConfig

    from datetime import datetime, timedelta

    src, landing = env
    # 取"从现在起 2~3 小时后"的窗口:任何时刻跑测试都必然在窗口外(含跨零点)
    t2, t3 = datetime.now() + timedelta(hours=2), datetime.now() + timedelta(hours=3)
    scfg = SourceConfig(adapter="sqlite_readonly", path=str(src),
                        windows=[f"{t2:%H:%M}-{t3:%H:%M}"])
    assert sched.run_sync_cycle(SOURCE, scfg, pack, landing.db_path) is False, "窗口外不发起"

    scfg_open = SourceConfig(adapter="sqlite_readonly", path=str(src))
    assert sched.run_sync_cycle(SOURCE, scfg_open, pack, landing.db_path) is True
    assert landing.count(SOURCE, "SALES_ORDER") == 97
    (n,) = landing.con.execute('SELECT COUNT(*) FROM "obj_SalesOrder"').fetchone()
    assert n > 0, "apply_after_sync 应自动物化对象层"


def test_serve_once(env, pack, tmp_path):
    pytest.importorskip("apscheduler")
    from data2agent.connect.config import load_config
    from data2agent.connect.scheduler import serve

    src, _ = env
    cfg_file = tmp_path / "connect.yaml"
    cfg_file.write_text(
        f"templates: {ROOT / 'templates'}\n"
        f"landing: {tmp_path / 'serve_landing.sqlite'}\n"
        "sources:\n"
        "  digiwin_e10:\n"
        "    adapter: sqlite_readonly\n"
        f"    path: {src}\n"
        "    reconcile_at: \"05:30\"\n",
        encoding="utf-8")
    serve(load_config(cfg_file), once=True)
    landing = LandingStore(tmp_path / "serve_landing.sqlite")
    assert landing.count(SOURCE, "QUOTATION") == 180
    runs = landing.con.execute("SELECT COUNT(*) FROM d2a_sync_run").fetchone()[0]
    assert runs >= 3, "一轮 serve --once 应含 sync + apply + reconcile"
