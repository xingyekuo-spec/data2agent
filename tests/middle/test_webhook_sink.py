"""通用 HTTP 批量推送(sink.type=webhook / d2a-push v1)测试。"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.middle.extract.increment import incremental_sync
from data2agent.middle.extract.scheduler import build_sink, unsupported_reconcile
from data2agent.middle.extract.webhook_sink import (
    EVENT_TYPE_PREFIX,
    WebhookSink,
    sign_body,
    verify_signature,
)
from data2agent.shared.config import (
    ConnectConfig,
    SinkConfig,
    SourceConfig,
    load_config,
)
from data2agent.shared.store.landing import LandingStore
from data2agent.shared.store.table import TableInfo
from tests.fixtures.e10.seed import build, write_db
from tests.helpers import e10_watermarks, e10_whitelist
from tests.support.webhook_receiver import WebhookReceiver

SOURCE = "digiwin_e10"
URL = "https://receiver.example/d2a/events"


@pytest.fixture()
def source_db(tmp_path) -> Path:
    db = tmp_path / "source.sqlite"
    write_db(db, build(seed=42, asof=date(2026, 7, 10)))
    return db


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(
        "data2agent.middle.extract.webhook_sink.time.sleep", lambda _s: None)


def _adapter(source_db):
    return SqliteReadOnlyAdapter(str(source_db), e10_whitelist())


def _sink(receiver: WebhookReceiver, landing: LandingStore | None = None,
          **kw) -> WebhookSink:
    return WebhookSink(
        URL, receiver.token, hmac_secret=receiver.secret,
        send=receiver.send, landing=landing, source=SOURCE, **kw)


def test_push_matches_direct_sync_and_middle_holds_no_raw(source_db, tmp_path):
    direct = LandingStore(tmp_path / "direct.sqlite")
    incremental_sync(_adapter(source_db), direct, SOURCE, e10_watermarks())

    receiver = WebhookReceiver(token="tok", secret="s3cret")
    middle = LandingStore(tmp_path / "middle.sqlite")
    report = incremental_sync(
        _adapter(source_db), middle, SOURCE, e10_watermarks(),
        sink=_sink(receiver, middle))
    assert not report.paused
    for table in e10_whitelist():
        assert receiver.count(table) == direct.count(SOURCE, table) > 0, table

    raw_tables = [r[0] for r in middle.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'")]
    assert raw_tables == [], "webhook 模式中间机不落 raw"
    assert middle.get_high_water(SOURCE, "SALES_ORDER") is not None

    types = receiver.event_types
    assert types[0] == "sync.begin" and types[-1] == "sync.complete"
    assert types.count("table.begin") == types.count("table.complete") == len(e10_whitelist())
    sync_ids = {e["data"]["sync_id"] for e in receiver.events}
    assert len(sync_ids) == 1
    run = middle.con.execute(
        "SELECT generation_id FROM d2a_sync_run WHERE id = ?",
        (report.run_id,)).fetchone()
    assert run["generation_id"] == sync_ids.pop(), "运行记录关联 sync_id 便于对端排障"


def test_events_are_cloudevents_with_idempotency_key(tmp_path):
    captured: list[tuple[dict, dict]] = []

    def send(url, body, headers, timeout):
        captured.append((json.loads(body), headers))
        return 200, b""

    sink = WebhookSink(URL, "tok", send=send)
    info = TableInfo("T", [("ID", "int"), ("V", "text")], ["ID"], schema="dbo")
    sink.begin_sync(SOURCE, ["T"], 1)
    sink.begin_table(SOURCE, info, mode="incremental")
    sink.write(SOURCE, info, [{"ID": 1, "V": "a"}], "b-0", table_run_id="r1")
    sink.complete_table(SOURCE, info, "r1", 1, 1)
    sink.complete_sync(SOURCE)

    event, headers = captured[2]
    assert event["specversion"] == "1.0"
    assert event["type"] == EVENT_TYPE_PREFIX + "batch"
    assert event["source"] == f"data2agent/{SOURCE}"
    assert event["subject"] == "T"
    assert event["id"] == headers["Idempotency-Key"]
    assert event["id"] == f"{event['data']['sync_id']}:b-0", "幂等键以 sync_id 限定作用域"
    assert headers["Content-Type"].startswith("application/cloudevents+json")
    assert headers["Authorization"] == "Bearer tok"
    data = event["data"]
    assert data["columns"] == [["ID", "int"], ["V", "text"]]
    assert data["pk"] == ["ID"] and data["schema"] == "dbo"
    assert data["rows"] == [{"ID": 1, "V": "a"}] and data["row_count"] == 1
    assert len({e["id"] for e, _ in captured}) == len(captured), "事件 id 唯一"


def test_retry_reuses_event_id_and_receiver_dedups(tmp_path):
    receiver = WebhookReceiver(fail_first=2, fail_status=503)
    landing = LandingStore(tmp_path / "middle.sqlite")
    sink = _sink(receiver, landing, retries=3)
    info = TableInfo("T", [("ID", "int")], ["ID"])
    sink.begin_sync(SOURCE, ["T"], landing.start_run(SOURCE, "sync"))
    assert receiver.deliveries == 3 and receiver.event_types == ["sync.begin"]
    # 对端已生效但回执丢失时的重投:同 id 只生效一次
    sink.begin_table(SOURCE, info, mode="incremental")
    sink.write(SOURCE, info, [{"ID": 1}], "b-0", table_run_id="r")
    sink.write(SOURCE, info, [{"ID": 1}], "b-0", table_run_id="r")
    assert receiver.event_types.count("batch") == 1
    statuses = [r["status"] for r in landing.con.execute(
        "SELECT status FROM d2a_http_push_log WHERE step_kind = 'begin_generation' "
        "ORDER BY id")]
    assert statuses == ["retrying", "retrying", "ok"]


def test_client_error_fails_fast_and_is_logged(tmp_path):
    receiver = WebhookReceiver(token="right")
    landing = LandingStore(tmp_path / "middle.sqlite")
    sink = WebhookSink(URL, "wrong", send=receiver.send, landing=landing,
                       source=SOURCE, retries=5)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        sink.begin_sync(SOURCE, ["T"], landing.start_run(SOURCE, "sync"))
    assert receiver.deliveries == 1, "4xx 不重试"
    row = landing.con.execute(
        "SELECT status, error_category, retryable FROM d2a_http_push_log").fetchone()
    assert (row["status"], row["error_category"], row["retryable"]) == (
        "failed", "auth", 0)


def test_full_refresh_replaces_only_on_complete_and_abort_discards(tmp_path):
    receiver = WebhookReceiver()
    sink = _sink(receiver)
    info = TableInfo("DIM", [("ID", "int"), ("NAME", "text")], [])
    sink.begin_sync(SOURCE, ["DIM"], 1)
    sink.begin_table(SOURCE, info, mode="full_refresh", snapshot_id="s1")
    sink.write(SOURCE, info, [{"ID": 1, "NAME": "a"}, {"ID": 2, "NAME": "b"}],
               "s1-0", mode="full_refresh", snapshot_id="s1", table_run_id="r1")
    assert receiver.count("DIM") == 0, "complete 前不得替换"
    sink.complete_table(SOURCE, info, "r1", 2, 1, mode="full_refresh",
                        snapshot_id="s1")
    assert receiver.count("DIM") == 2

    sink.begin_table(SOURCE, info, mode="full_refresh", snapshot_id="s2")
    sink.write(SOURCE, info, [{"ID": 9, "NAME": "z"}], "s2-0",
               mode="full_refresh", snapshot_id="s2", table_run_id="r2")
    sink.abort_table(SOURCE, info, mode="full_refresh", snapshot_id="s2")
    assert receiver.count("DIM") == 2, "abort 后保留上一版快照"
    assert receiver.event_types[-1] == "table.abort"


def test_blob_is_base64_tagged_and_round_trips():
    receiver = WebhookReceiver()
    sink = _sink(receiver)
    info = TableInfo("BIN", [("ID", "int"), ("PAYLOAD", "blob")], ["ID"])
    raw = b"\x00\xffbinary;not-text\x80"
    sink.begin_table(SOURCE, info, mode="incremental")
    sink.write(SOURCE, info, [{"ID": 1, "PAYLOAD": raw}], "b-0", table_run_id="r")
    batch = receiver.events[-1]["data"]
    assert batch["binary_encoding"] == "base64-v1"
    assert isinstance(batch["rows"][0]["PAYLOAD"], dict)
    assert receiver.tables["BIN"][(1,)]["PAYLOAD"] == raw


def test_pause_aborts_sync(source_db, tmp_path):
    receiver = WebhookReceiver()
    middle = LandingStore(tmp_path / "middle.sqlite")
    calls = {"n": 0}

    def should_continue():
        calls["n"] += 1
        return calls["n"] < 3

    report = incremental_sync(
        _adapter(source_db), middle, SOURCE, e10_watermarks(),
        sink=_sink(receiver, middle), should_continue=should_continue)
    assert report.paused
    assert receiver.event_types[-1] == "sync.abort"


def test_signature_roundtrip_and_replay_window():
    body = b'{"id":"x"}'
    now = int(time.time())
    header = sign_body("k", now, body)
    assert verify_signature("k", header, body, now=now)
    assert not verify_signature("other", header, body, now=now)
    assert not verify_signature("k", header, body + b" ", now=now)
    assert not verify_signature("k", header, body, now=now + 3600), "超出容差拒绝重放"
    assert not verify_signature("k", "garbage", body, now=now)


# ---- 配置与装配 ----

def _webhook_yaml(tmp_path, sink_lines: str, extra: str = "",
                  mode: str = "development") -> Path:
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        f"deployment_mode: {mode}\n"
        f"state_db: {tmp_path / 'state.sqlite'}\n"
        "sources:\n"
        "  e10:\n"
        "    adapter: sqlite_readonly\n"
        "    path: source.sqlite\n"
        "    tables: {}\n"
        "    sink:\n"
        "      type: webhook\n"
        + sink_lines + extra,
        encoding="utf-8")
    return cfg


def test_webhook_requires_auth_for_remote_endpoint(tmp_path):
    with pytest.raises(ValueError, match="token_env 或 hmac_secret_env"):
        load_config(_webhook_yaml(tmp_path, f"      url: {URL}\n"))
    cfg = load_config(_webhook_yaml(
        tmp_path, f"      url: {URL}\n      hmac_secret_env: D2A_WEBHOOK_SECRET\n"))
    assert cfg.sources["e10"].sink.type == "webhook"


def test_webhook_rejects_plain_http_and_reconcile(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        load_config(_webhook_yaml(
            tmp_path, "      url: http://receiver.example/x\n      token_env: T\n"))
    with pytest.raises(ValueError, match="webhook 不支持对账"):
        load_config(_webhook_yaml(
            tmp_path, f"      url: {URL}\n      token_env: T\n",
            extra='    reconcile_at: "05:30"\n'))


def test_hmac_secret_env_only_for_webhook():
    with pytest.raises(ValueError, match="hmac_secret_env 仅用于"):
        SinkConfig(type="http", url=URL, hmac_secret_env="S")


def test_production_accepts_webhook_but_not_loopback():
    def cfg(url: str) -> ConnectConfig:
        return ConnectConfig(
            deployment_mode="production",
            sources={"e10": SourceConfig(
                adapter="sqlite_readonly", path="s.sqlite", tables={},
                sink=SinkConfig(type="webhook", url=url, token_env="T"),
                spool={"policy": "strict_stream"},
            )},
        )
    assert cfg(URL).production_violations() == []
    assert any("回环" in v for v in cfg("https://127.0.0.1/x").production_violations())


def test_legacy_templates_and_apply_keys_are_ignored(tmp_path):
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        "templates: templates\n"
        "landing: l.sqlite\n"
        "sources:\n"
        "  e10:\n"
        "    adapter: sqlite_readonly\n"
        "    path: s.sqlite\n"
        "    tables: {}\n"
        "    apply_after_sync: true\n",
        encoding="utf-8")
    loaded = load_config(cfg)
    assert not hasattr(loaded, "templates")
    assert loaded.state_db == "l.sqlite"


def test_build_sink_wires_webhook_secrets(tmp_path, monkeypatch):
    landing = LandingStore(tmp_path / "state.sqlite")
    scfg = SourceConfig(
        adapter="sqlite_readonly", path="s.sqlite", tables={},
        sink=SinkConfig(type="webhook", url=URL, token_env="D2A_WH_TOKEN",
                        hmac_secret_env="D2A_WH_SECRET", retries=2),
    )
    monkeypatch.setenv("D2A_WH_TOKEN", "tok")
    monkeypatch.delenv("D2A_WH_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="D2A_WH_SECRET"):
        build_sink(scfg, landing, source="e10")
    monkeypatch.setenv("D2A_WH_SECRET", "sec")
    sink = build_sink(scfg, landing, source="e10")
    assert isinstance(sink, WebhookSink)
    assert (sink.url, sink.token, sink.hmac_secret, sink.retries) == (
        URL, "tok", "sec", 2)
    assert unsupported_reconcile(scfg) is not None


def test_keyless_snapshot_keeps_duplicate_rows():
    receiver = WebhookReceiver()
    sink = _sink(receiver)
    info = TableInfo("LOG", [("MSG", "text")], [])
    sink.begin_table(SOURCE, info, mode="full_refresh", snapshot_id="s1")
    sink.write(SOURCE, info, [{"MSG": "x"}, {"MSG": "x"}], "s1-0",
               mode="full_refresh", snapshot_id="s1", table_run_id="r1")
    sink.complete_table(SOURCE, info, "r1", 2, 1, mode="full_refresh",
                        snapshot_id="s1")
    assert receiver.count("LOG") == 2, "无主键快照的重复行不得被去重"


def test_retryable_failure_keeps_http_status_as_cause():
    receiver = WebhookReceiver(fail_first=1, fail_status=503)
    sink = _sink(receiver, retries=1)
    with pytest.raises(RuntimeError) as exc:
        sink.ping(SOURCE)
    assert getattr(exc.value.__cause__, "code", None) == 503
