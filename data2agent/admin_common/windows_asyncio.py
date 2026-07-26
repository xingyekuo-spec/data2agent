"""Windows asyncio startup compatibility helpers."""

from __future__ import annotations

import os
import socket
import sys


def bounded_socketpair(
    family: socket.AddressFamily = socket.AF_INET,
    type: socket.SocketKind = socket.SOCK_STREAM,
    proto: int = 0,
) -> tuple[socket.socket, socket.socket]:
    """socketpair fallback with timeouts to avoid hanging before uvicorn listens."""
    if family == socket.AF_INET:
        host = "127.0.0.1"
    elif family == socket.AF_INET6:
        host = "::1"
    else:
        raise ValueError("Only AF_INET and AF_INET6 socket address families are supported")
    if type != socket.SOCK_STREAM:
        raise ValueError("Only SOCK_STREAM socket type is supported")
    if proto != 0:
        raise ValueError("Only protocol zero is supported")

    lsock = socket.socket(family, type, proto)
    lsock.settimeout(5.0)
    try:
        lsock.bind((host, 0))
        lsock.listen(1)
        addr, port = lsock.getsockname()[:2]
        csock = socket.socket(family, type, proto)
        try:
            csock.settimeout(5.0)
            csock.connect((addr, port))
            ssock, _ = lsock.accept()
            ssock.settimeout(None)
            csock.settimeout(None)
        except Exception:
            csock.close()
            raise
    finally:
        lsock.close()

    try:
        if ssock.getsockname() != csock.getpeername() or csock.getsockname() != ssock.getpeername():
            raise ConnectionError("Unexpected peer connection")
    except Exception:
        ssock.close()
        csock.close()
        raise
    return ssock, csock


def patch_windows_socketpair() -> bool:
    """Install bounded socketpair on Windows when explicitly enabled."""
    if sys.platform != "win32":
        return False
    if os.environ.get("D2A_PATCH_SOCKETPAIR", "0").strip().lower() not in {"1", "true", "yes"}:
        return False
    socket.socketpair = bounded_socketpair
    print("Windows socketpair startup patch enabled", flush=True)
    return True
