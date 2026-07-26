"""Console module entry helpers."""

from __future__ import annotations

from types import SimpleNamespace

from data2agent.console import __main__ as console_main


def test_startup_trace_uses_add_event_handler(monkeypatch):
    calls: list[tuple[str, object]] = []
    app = SimpleNamespace(add_event_handler=lambda event, func: calls.append((event, func)))
    monkeypatch.setattr(console_main.faulthandler, "enable", lambda: None)
    monkeypatch.setattr(console_main.faulthandler, "dump_traceback_later", lambda *a, **k: None)

    console_main._enable_startup_trace(app)

    assert calls and calls[0][0] == "startup"


def test_startup_trace_falls_back_to_router_on_startup(monkeypatch):
    app = SimpleNamespace(router=SimpleNamespace(on_startup=[]))
    monkeypatch.setattr(console_main.faulthandler, "enable", lambda: None)
    monkeypatch.setattr(console_main.faulthandler, "dump_traceback_later", lambda *a, **k: None)

    console_main._enable_startup_trace(app)

    assert len(app.router.on_startup) == 1


def test_startup_trace_without_supported_hook_does_not_crash(monkeypatch):
    app = SimpleNamespace()
    monkeypatch.setattr(console_main.faulthandler, "enable", lambda: None)
    monkeypatch.setattr(console_main.faulthandler, "dump_traceback_later", lambda *a, **k: None)

    console_main._enable_startup_trace(app)
