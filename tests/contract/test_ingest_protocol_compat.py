"""ingest 协议兼容:健康声明、supported 列表、旧中间机行为。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from data2agent.middle.extract.adapters.base import TableInfo
from data2agent.shared.store.landing import LandingStore
from data2agent.middle.extract.sink import HttpPushSink, ProtocolVersionError
from data2agent.platform.ingest.app import create_app
from data2agent.protocol.ingest import (
    INGEST_PROTOCOL_VERSION,
    SUPPORTED_INGEST_PROTOCOL_VERSIONS,
    health_protocol_fields,
    is_supported_protocol,
)
from scripts.check_ingest_compat import check_constants, render_release_notes


SOURCE = "digiwin_e10"


def test_health_declares_active_and_supported_list(tmp_path: Path):
    client = TestClient(create_app(LandingStore(tmp_path / "p.sqlite").db_path))
    body = client.get("/ingest/health").json()
    assert body["ok"] is True
    assert body["ingest_protocol_version"] == "2"
    assert body["active_ingest_protocol_version"] == "3"
    assert body["supported_ingest_protocol_versions"] == ["2", "3"]
    assert health_protocol_fields()["supported_ingest_protocol_versions"] == ["2", "3"]


def test_middle_accepts_when_send_protocol_in_supported_list():
    """平台可声明支持多协议;中间机只要自己发送的版本在列表中即可。"""
    sink = HttpPushSink(
        "http://platform",
        post=lambda *a, **k: None,
        get_json=lambda *a, **k: {
            "ok": True,
            "ingest_protocol_version": "9",
            "active_ingest_protocol_version": "9",
            "supported_ingest_protocol_versions": ["2", "3", "9"],
        },
    )
    sink.ensure_protocol()
    assert sink._protocol_checked is True


def test_middle_rejects_when_send_protocol_not_supported():
    sink = HttpPushSink(
        "http://platform",
        post=lambda *a, **k: None,
        get_json=lambda *a, **k: {
            "ok": True,
            "active_ingest_protocol_version": "2",
            "supported_ingest_protocol_versions": ["2"],
        },
    )
    with pytest.raises(ProtocolVersionError, match="不兼容"):
        sink.ensure_protocol()


def test_legacy_middle_equality_against_current_platform_health(tmp_path: Path):
    """模拟上一正式版中间机:只读 ingest_protocol_version 并精确相等。

    当前平台仍声明 v2 时,旧中间机必须能通过(平台可单独升级的契约门禁)。
    """
    client = TestClient(create_app(LandingStore(tmp_path / "p.sqlite").db_path))
    health = client.get("/ingest/health").json()

    def legacy_ensure(health_body: dict, mine: str = "2") -> None:
        remote = health_body.get("ingest_protocol_version")
        if remote != mine:
            raise ProtocolVersionError(
                f"ingest 协议版本不一致:中间机要求 {mine}, 平台返回 {remote!r}"
            )

    assert INGEST_PROTOCOL_VERSION == "3"
    assert "2" in SUPPORTED_INGEST_PROTOCOL_VERSIONS
    legacy_ensure(health, mine="2")


def test_legacy_middle_survives_new_active_when_v2_is_still_supported(monkeypatch):
    """平台 active 升至 v3 但仍支持 v2 时，已部署 v2 中间机可继续推送。"""
    import data2agent.protocol.ingest as protocol

    monkeypatch.setattr(protocol, "INGEST_PROTOCOL_VERSION", "3")
    monkeypatch.setattr(protocol, "SUPPORTED_INGEST_PROTOCOL_VERSIONS", ("2", "3"))
    monkeypatch.setattr(protocol, "LEGACY_HEALTH_INGEST_PROTOCOL_VERSION", "2")
    health = protocol.health_protocol_fields()

    assert health["active_ingest_protocol_version"] == "3"
    assert health["supported_ingest_protocol_versions"] == ["2", "3"]
    # 模拟上一版中间机：它只读取遗留字段，不知道 supported 列表。
    assert health["ingest_protocol_version"] == "2"


def test_legacy_middle_fails_if_platform_drops_v2():
    def legacy_ensure(health_body: dict, mine: str = "2") -> None:
        remote = health_body.get("ingest_protocol_version")
        if remote != mine:
            raise ProtocolVersionError("协议版本不一致")

    with pytest.raises(ProtocolVersionError):
        legacy_ensure({"ingest_protocol_version": "3"}, mine="2")


def test_push_contract_current_platform_with_v2_middle(
    tmp_path: Path, monkeypatch,
):
    """当前平台 + 发送 v2 的中间机:端到端 begin 可通(协议门禁通过)。"""
    platform = LandingStore(tmp_path / "platform.sqlite")
    client = TestClient(create_app(platform.db_path))

    def post(url, payload, tok, timeout):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        r = client.post(path, json=payload)
        r.raise_for_status()

    def get_json(url, tok, timeout):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        r = client.get(path)
        r.raise_for_status()
        return r.json()

    import data2agent.middle.extract.sink as sink_module
    monkeypatch.setattr(sink_module, "INGEST_PROTOCOL_VERSION", "2")
    sink = HttpPushSink("http://platform", post=post, get_json=get_json)
    info = TableInfo("CURRENCY", [("CODE", "text")], ["CODE"])
    sink.begin_table(SOURCE, info, mode="incremental")
    assert sink._protocol_checked is True


def test_compat_script_ok_for_current_v3():
    assert check_constants() == []
    assert is_supported_protocol("2")
    assert is_supported_protocol("3")
    notes = render_release_notes(release_version="v0.5.1")
    assert "兼容中间机发送协议" in notes
    assert "无需升级" in notes
    assert "破坏性发布" not in notes


def test_compat_script_requires_manifest_when_dropping_baseline(tmp_path: Path, monkeypatch):
    import scripts.check_ingest_compat as mod

    monkeypatch.setattr(mod, "SUPPORTED_INGEST_PROTOCOL_VERSIONS", ("3",))
    monkeypatch.setattr(mod, "INGEST_PROTOCOL_VERSION", "3")
    monkeypatch.setattr(mod, "LEGACY_HEALTH_INGEST_PROTOCOL_VERSION", "3")
    bare = {
        "schema_version": 1,
        "field_baseline_send_protocols": ["2", "3"],
        "unsupported": {},
    }
    errors = mod.check_constants(bare)
    assert errors and "ingest_protocol_compat.json" in errors[0]

    declared = {
        "schema_version": 1,
        "field_baseline_send_protocols": ["2", "3"],
        "unsupported": {
            "2": {
                "reason": "v3 snapshot framing replaces v2",
                "since_release": "v0.6.0",
            }
        },
    }
    assert mod.check_constants(declared) == []
    notes = mod.render_release_notes(release_version="v0.6.0", manifest=declared)
    assert "破坏性发布" in notes
    assert "必须升级" in notes
    assert "v2" in notes


def test_compat_script_keeps_legacy_health_on_oldest_supported_baseline(monkeypatch):
    import scripts.check_ingest_compat as mod

    manifest = {
        "schema_version": 1,
        "field_baseline_send_protocols": ["2", "3"],
        "unsupported": {},
    }
    monkeypatch.setattr(mod, "SUPPORTED_INGEST_PROTOCOL_VERSIONS", ("2", "3"))
    monkeypatch.setattr(mod, "INGEST_PROTOCOL_VERSION", "3")
    monkeypatch.setattr(mod, "LEGACY_HEALTH_INGEST_PROTOCOL_VERSION", "2")
    assert mod.check_constants(manifest) == []

    monkeypatch.setattr(mod, "LEGACY_HEALTH_INGEST_PROTOCOL_VERSION", "3")
    errors = mod.check_constants(manifest)
    assert any("最早仍受支持" in error for error in errors)


def test_compat_script_rejects_silent_baseline_shrink(monkeypatch):
    """将基线从 [2] 改成 [3] 且 supported=[3] 不得绕过 unsupported 声明。"""
    import scripts.check_ingest_compat as mod

    monkeypatch.setattr(mod, "SUPPORTED_INGEST_PROTOCOL_VERSIONS", ("3",))
    monkeypatch.setattr(mod, "INGEST_PROTOCOL_VERSION", "3")
    monkeypatch.setattr(mod, "LEGACY_HEALTH_INGEST_PROTOCOL_VERSION", "3")
    previous = {
        "schema_version": 1,
        "field_baseline_send_protocols": ["2"],
        "unsupported": {},
    }
    shrunk = {
        "schema_version": 1,
        "field_baseline_send_protocols": ["3"],
        "unsupported": {},
    }
    errors = mod.check_constants(shrunk, previous_manifest=previous)
    assert errors
    assert any("缩短" in e for e in errors)

    proper = {
        "schema_version": 1,
        "field_baseline_send_protocols": ["2", "3"],
        "unsupported": {
            "2": {
                "reason": "v3 replaces v2",
                "since_release": "v0.6.0",
            }
        },
    }
    assert mod.check_constants(proper, previous_manifest=previous) == []
    notes = mod.render_release_notes(release_version="v0.6.0", manifest=proper)
    assert "破坏性发布" in notes


def test_compat_script_rejects_stale_unsupported_still_in_supported():
    import scripts.check_ingest_compat as mod

    stale = {
        "schema_version": 1,
        "field_baseline_send_protocols": ["2"],
        "unsupported": {
            "2": {"reason": "gone", "since_release": "v0.6.0"},
        },
    }
    errors = mod.check_constants(stale)
    assert any("仍在" in e for e in errors)
