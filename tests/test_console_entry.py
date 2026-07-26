"""Console module entry helpers."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from data2agent.admin_common import windows_asyncio
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


def test_windows_socketpair_patch_only_on_windows(monkeypatch):
    original = socket.socketpair
    monkeypatch.setattr(windows_asyncio.sys, "platform", "darwin")
    monkeypatch.setattr(windows_asyncio.socket, "socketpair", original)

    patched = windows_asyncio.patch_windows_socketpair()

    assert patched is False
    assert windows_asyncio.socket.socketpair is original


def test_windows_socketpair_patch_can_be_disabled(monkeypatch):
    original = socket.socketpair
    monkeypatch.setattr(windows_asyncio.sys, "platform", "win32")
    monkeypatch.setenv("D2A_PATCH_SOCKETPAIR", "0")
    monkeypatch.setattr(windows_asyncio.socket, "socketpair", original)

    patched = windows_asyncio.patch_windows_socketpair()

    assert patched is False
    assert windows_asyncio.socket.socketpair is original


def test_windows_socketpair_patch_installs_bounded_pair(monkeypatch):
    original = socket.socketpair
    monkeypatch.setattr(windows_asyncio.sys, "platform", "win32")
    monkeypatch.delenv("D2A_PATCH_SOCKETPAIR", raising=False)
    monkeypatch.setattr(windows_asyncio.socket, "socketpair", original)

    patched = windows_asyncio.patch_windows_socketpair()

    assert patched is True
    assert windows_asyncio.socket.socketpair is windows_asyncio.bounded_socketpair


def test_bounded_socketpair_connects_loopback_pair():
    try:
        left, right = windows_asyncio.bounded_socketpair()
    except PermissionError as exc:
        pytest.skip(f"loopback bind not permitted in sandbox: {exc}")
    try:
        left.sendall(b"x")
        assert right.recv(1) == b"x"
    finally:
        left.close()
        right.close()
