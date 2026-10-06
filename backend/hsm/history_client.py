"""API-side client for the collector's private history socket.

The API process never opens TinyFlux; it forwards one typed request per
connection and maps protocol errors to application errors.
"""
from __future__ import annotations

import json
import socket
import time

from .errors import Forbidden, Invalid, Unavailable

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _unavailable() -> Unavailable:
    return Unavailable("Metrics history is temporarily unavailable.", code="HISTORY_UNAVAILABLE")


def query(cfg, request: dict, timeout: float = 5.0) -> dict:
    """Send ``request`` (contract shape) and return the ``data`` object."""
    if not hasattr(socket, "AF_UNIX"):
        raise _unavailable()
    deadline = time.monotonic() + timeout
    payload = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(cfg.history_socket))
        sock.sendall(payload)
        buf = bytearray()
        while not buf.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_RESPONSE_BYTES:
                raise _unavailable()
    except (OSError, socket.timeout):
        raise _unavailable() from None
    finally:
        sock.close()
    try:
        resp = json.loads(bytes(buf).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _unavailable() from None
    if not isinstance(resp, dict):
        raise _unavailable()
    if resp.get("ok") is True and isinstance(resp.get("data"), dict):
        return resp["data"]
    err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
    code = err.get("code")
    if code == "BUSY":
        raise Unavailable("Metrics history is busy; try again shortly.", code="HISTORY_BUSY")
    if code == "FORBIDDEN":
        raise Forbidden("You do not have access to this container.")
    if code == "INVALID":
        raise Invalid(str(err.get("message") or "Invalid history request."))
    raise _unavailable()
