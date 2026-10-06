"""Private Unix-socket endpoint answering bounded history/usage requests.

Protocol (docs/api-contract.md "history socket"): one JSON line (<= 4096 bytes)
per connection, one JSON line back. Typed requests only - never paths, pickles
or query expressions.

Defence in depth: the API authorizes before forwarding, and this server
1. checks the peer's OS uid (SO_PEERCRED) against HSM_HISTORY_ALLOWED_UIDS,
2. validates the request strictly,
3. re-checks the user's *current* container access in SQLite (own connection),
4. only then consults the small result cache or queues work on the store thread.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
import time
from collections import OrderedDict

from .. import db
from ..auth import policy
from ..errors import Forbidden, HSMError, NotFound
from ..security import is_uuid
from .store import HISTORY_METRICS, MAX_POINTS, StoreBusy, StoreClosed, StoreTimeout

log = logging.getLogger("hsm.collector.history")

MAX_REQUEST_BYTES = 4096
READ_TIMEOUT = 2.0
CACHE_ENTRIES = 32


class InvalidRequest(ValueError):
    pass


def _int(obj: dict, key: str) -> int:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise InvalidRequest(f"{key} must be an integer")
    return v


def validate(obj, cfg, now: float | None = None) -> dict:
    """Return a normalized request or raise InvalidRequest."""
    if not isinstance(obj, dict):
        raise InvalidRequest("request must be an object")
    if obj.get("v") != 1 or isinstance(obj.get("v"), bool):
        raise InvalidRequest("unsupported protocol version")
    kind = obj.get("type")
    allowed = {"history": {"v", "type", "user_id", "container_id", "start", "end", "metrics", "max_points"},
               "usage": {"v", "type", "user_id", "container_id", "start", "end"}}
    if kind not in allowed:
        raise InvalidRequest("type must be history or usage")
    if set(obj) != allowed[kind]:
        raise InvalidRequest("unexpected or missing fields")
    for key in ("user_id", "container_id"):
        if not isinstance(obj[key], str) or not is_uuid(obj[key]):
            raise InvalidRequest(f"{key} must be a UUID")
    start, end = _int(obj, "start"), _int(obj, "end")
    now = time.time() if now is None else now
    if not 0 < start < end or end > now + 300:
        raise InvalidRequest("invalid time range")
    if end - start > cfg.history_max_range_days * 86400:
        raise InvalidRequest("range too long")
    req = {"type": kind, "user_id": obj["user_id"], "container_id": obj["container_id"], "start": start, "end": end}
    if kind == "history":
        metrics = obj["metrics"]
        if (not isinstance(metrics, list) or not metrics or len(metrics) > len(HISTORY_METRICS)
                or not all(isinstance(m, str) and m in HISTORY_METRICS for m in metrics)
                or len(set(metrics)) != len(metrics)):
            raise InvalidRequest("invalid metrics")
        max_points = _int(obj, "max_points")
        if not 1 <= max_points <= MAX_POINTS:
            raise InvalidRequest("max_points out of range")
        req["metrics"] = sorted(metrics)
        req["max_points"] = min(max_points, cfg.history_max_points)
    return req


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class RequestHandler:
    """Socket-independent request handling (unit-tested on every platform)."""

    def __init__(self, cfg, store_thread, *, request_timeout: float = 4.0):
        self.cfg = cfg
        self.store = store_thread
        self.request_timeout = request_timeout
        self._cache: "OrderedDict[tuple, bytes]" = OrderedDict()
        self._lock = threading.Lock()

    def _authorize(self, user_id: str, container_id: str) -> None:
        conn = db.connect(self.cfg.sqlite_path)
        try:
            policy.container_for(conn, user_id, container_id, "view")
        finally:
            conn.close()

    def handle(self, raw: bytes) -> bytes:
        try:
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise InvalidRequest("malformed JSON") from None
            req = validate(obj, self.cfg)
        except InvalidRequest as exc:
            return self._encode(_error("INVALID", str(exc)))
        try:
            self._authorize(req["user_id"], req["container_id"])
        except (Forbidden, NotFound):
            # An unknown/deleted container is also a denial here; the API already
            # distinguished 404 before forwarding.
            return self._encode(_error("FORBIDDEN", "You do not have access to this container."))
        except HSMError as exc:
            return self._encode(_error("FORBIDDEN", exc.message))
        except Exception:
            log.exception("access check failed")
            return self._encode(_error("UNAVAILABLE", "Access check failed."))

        # The access check above runs before every cache hit.
        key = (req["type"], req["container_id"], req["start"], req["end"], tuple(req.get("metrics", ())),
               req.get("max_points"), self.store.generation)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        cid, start, end = req["container_id"], req["start"], req["end"]
        if req["type"] == "history":
            metrics, max_points = req["metrics"], req["max_points"]
            fn = lambda s: s.history(cid, start, end, metrics, max_points)  # noqa: E731
        else:
            fn = lambda s: s.usage(cid, start, end)  # noqa: E731
        try:
            data = self.store.call(fn, self.request_timeout)
        except (StoreBusy, StoreTimeout):
            return self._encode(_error("BUSY", "History is busy; try again shortly."))
        except StoreClosed:
            return self._encode(_error("UNAVAILABLE", "History service is stopping."))
        except Exception:
            log.exception("history query failed")
            return self._encode(_error("UNAVAILABLE", "History query failed."))
        body = self._encode({"ok": True, "data": data})
        with self._lock:
            self._cache[key] = body
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return body

    @staticmethod
    def _encode(obj: dict) -> bytes:
        return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def peer_uid(sock) -> int | None:
    """Peer uid via SO_PEERCRED (Linux). None where unsupported."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def read_line(sock, limit: int = MAX_REQUEST_BYTES) -> bytes | None:
    """Read one newline-terminated request; None if it exceeds ``limit``."""
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(limit + 1 - len(buf))
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            return None
    return buf.split(b"\n", 1)[0]


class HistoryServer:
    def __init__(self, cfg, store_thread, *, max_handlers: int = 4, request_timeout: float = 4.0):
        self.cfg = cfg
        self.handler = RequestHandler(cfg, store_thread, request_timeout=request_timeout)
        self._slots = threading.BoundedSemaphore(max_handlers)
        self._sock = None
        self._thread = None
        self._stopping = threading.Event()

    def start(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Unix sockets are not available on this platform")
        path = str(self.cfg.history_socket)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            os.unlink(path)          # stale socket from a previous run (singleton lock held)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        os.chmod(path, 0o660)
        sock.listen(16)
        sock.settimeout(1.0)         # lets the accept loop notice stop()
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="hsm-history-accept", daemon=True)
        self._thread.start()
        log.info("history socket listening on %s", path)

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    break
                raise
            if not self._slots.acquire(blocking=False):
                self._reply_and_close(conn, RequestHandler._encode(_error("BUSY", "Too many concurrent requests.")))
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        try:
            conn.settimeout(READ_TIMEOUT)
            uid = peer_uid(conn)
            if uid is None or uid not in self.cfg.history_allowed_uids:
                log.warning("rejected history connection from uid %s", uid)
                self._reply_and_close(conn, RequestHandler._encode(_error("FORBIDDEN", "Peer not allowed.")))
                return
            line = read_line(conn)
            if line is None:
                body = RequestHandler._encode(_error("INVALID", "Request too large."))
            elif self._stopping.is_set():
                body = RequestHandler._encode(_error("UNAVAILABLE", "History service is stopping."))
            else:
                body = self.handler.handle(line)
            self._reply_and_close(conn, body)
        except (OSError, socket.timeout):
            try:
                conn.close()
            except OSError:
                pass
        finally:
            self._slots.release()

    @staticmethod
    def _reply_and_close(conn, body: bytes) -> None:
        try:
            conn.sendall(body)
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self) -> None:
        """Stop accepting new requests; in-flight handlers finish or time out."""
        self._stopping.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(3.0)
        try:
            os.unlink(str(self.cfg.history_socket))
        except OSError:
            pass
