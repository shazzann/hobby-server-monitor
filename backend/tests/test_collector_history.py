"""History socket: strict validation, access re-check before cache, error mapping, scheduler."""
from __future__ import annotations

import json
import os
import socket
import sys
import time

import pytest

from conftest import grant, make_container, make_user
from test_collector_store import A, T, feed

from hsm import history_client
from hsm.collector.__main__ import next_slot
from hsm.collector.history_server import HistoryServer, RequestHandler, validate
from hsm.collector.store import MetricsStore, StoreBusy, StoreThread
from hsm.errors import Forbidden, Invalid, Unavailable

unix_only = pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32",
                               reason="Unix sockets / SO_PEERCRED need Linux")


class InlineStore:
    """Runs store calls synchronously (the thread wrapper is tested separately)."""

    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.busy = False

    @property
    def generation(self):
        return self.store.generation

    def call(self, fn, timeout):
        if self.busy:
            raise StoreBusy()
        self.calls += 1
        return fn(self.store)


@pytest.fixture
def setup(cfg, conn):
    st = MetricsStore(cfg)
    st.open()
    admin = make_user(conn, "admin@example.com", role="admin")
    alice = make_user(conn, "alice@example.com")
    bob = make_user(conn, "bob@example.com")
    cid = make_container(conn, "web", owner_id=alice)
    grant(conn, cid, alice)
    feed(st, cid, T, T + 600)
    yield {"store": InlineStore(st), "admin": admin, "alice": alice, "bob": bob, "cid": cid, "conn": conn}
    st.close()


def req(user, cid, **kw):
    r = {"v": 1, "type": "history", "user_id": user, "container_id": cid, "start": T, "end": T + 600,
         "metrics": ["cpu_pct"], "max_points": 60}
    r.update(kw)
    return json.dumps(r).encode()


def test_unassigned_user_is_forbidden_even_with_a_cached_result(cfg, setup):
    h = RequestHandler(cfg, setup["store"])
    ok = json.loads(h.handle(req(setup["alice"], setup["cid"])))
    assert ok["ok"] is True and ok["data"]["container_id"] == setup["cid"]
    assert ok["data"]["series"]["cpu_pct"]["points"][1][1] == 50.0
    denied = json.loads(h.handle(req(setup["bob"], setup["cid"])))
    assert denied == {"ok": False, "error": {"code": "FORBIDDEN",
                                             "message": "You do not have access to this container."}}
    # Cache hit for alice ...
    calls = setup["store"].calls
    assert json.loads(h.handle(req(setup["alice"], setup["cid"])))["ok"] is True
    assert setup["store"].calls == calls
    # ... but revoking her assignment is honoured before the cache is consulted.
    setup["conn"].execute("DELETE FROM container_access WHERE user_id = ?", (setup["alice"],))
    assert json.loads(h.handle(req(setup["alice"], setup["cid"])))["error"]["code"] == "FORBIDDEN"
    assert json.loads(h.handle(req(setup["admin"], setup["cid"])))["ok"] is True


def test_usage_request(cfg, setup):
    body = json.dumps({"v": 1, "type": "usage", "user_id": setup["alice"], "container_id": setup["cid"],
                       "start": T, "end": T + 600}).encode()
    data = json.loads(RequestHandler(cfg, setup["store"]).handle(body))["data"]
    assert data["coverage"] == 1.0 and data["cpu_core_hours"] == pytest.approx(300 / 3600, abs=1e-5)


@pytest.mark.parametrize("bad", [
    b"not json", b"[]",
    req("x", A), req(A, A, v=2), req(A, A, v=True), req(A, A, type="sql"), req(A, A, metrics=["password"]),
    req(A, A, metrics=[]), req(A, A, metrics=["cpu_pct", "cpu_pct"]), req(A, A, max_points=601),
    req(A, A, max_points="10"), req(A, A, start=T + 10, end=T), req(A, A, start=1, end=int(time.time())),
    req(A, A, extra="../../etc/passwd"),
])
def test_strict_validation(cfg, setup, bad):
    out = json.loads(RequestHandler(cfg, setup["store"]).handle(bad))
    assert out["ok"] is False and out["error"]["code"] == "INVALID"


def test_busy_store(cfg, setup):
    setup["store"].busy = True
    out = json.loads(RequestHandler(cfg, setup["store"]).handle(req(setup["alice"], setup["cid"])))
    assert out["error"]["code"] == "BUSY"


def test_validate_clamps_to_config_max(cfg):
    import dataclasses
    small = dataclasses.replace(cfg, history_max_points=100)
    r = validate(json.loads(req(A, A, max_points=600)), small)
    assert r["max_points"] == 100


def test_store_thread_runs_requests_and_flushes_on_stop(cfg, conn):
    st = MetricsStore(cfg)
    st.open()
    th = StoreThread(st, maintain_every=3600)
    th.start()
    assert th.submit_ingest([(A, t, {"dt": 10.0, "cpu_pct": 1.0}) for t in range(T, T + 100, 10)])
    pts = th.call(lambda s: s._raw_points(A, T, T + 100), timeout=5)
    assert len(pts) == 10
    th.submit_ingest([(A, T + 100, {"dt": 10.0})])
    th.stop()
    assert not th.is_alive()
    s2 = MetricsStore(cfg)
    s2.open()
    assert len(s2._raw_points(A, T, T + 200)) == 11
    s2.close()


def test_history_client_without_server_is_unavailable(cfg):
    with pytest.raises(Unavailable) as e:
        history_client.query(cfg, {"v": 1}, timeout=0.5)
    assert e.value.code == "HISTORY_UNAVAILABLE" and e.value.status == 503


def test_scheduler_skips_missed_slots_without_burst():
    assert next_slot(100.0, 105.0, 10.0) == (110.0, 0)
    assert next_slot(100.0, 135.0, 10.0) == (140.0, 3)    # overran 2.5 periods: skip, don't catch up


@unix_only
def test_socket_round_trip_and_error_mapping(cfg, setup, monkeypatch):
    import dataclasses
    import tempfile
    import uuid
    from pathlib import Path
    # Short path in the OS temp dir: AF_UNIX paths are limited to ~108 bytes and
    # some filesystems (e.g. WSL drvfs) cannot hold sockets.
    sock_path = Path(tempfile.gettempdir()) / f"hsm-{uuid.uuid4().hex[:12]}.sock"
    sock_cfg = dataclasses.replace(cfg, history_socket=sock_path, history_allowed_uids=(os.getuid(),))
    th = StoreThread(setup["store"].store, maintain_every=3600)
    th.start()
    server = HistoryServer(sock_cfg, th)
    server.start()
    try:
        assert oct(os.stat(sock_cfg.history_socket).st_mode & 0o777) == "0o660"
        data = history_client.query(sock_cfg, json.loads(req(setup["alice"], setup["cid"])))
        assert data["source"] == "raw"
        with pytest.raises(Forbidden):
            history_client.query(sock_cfg, json.loads(req(setup["bob"], setup["cid"])))
        with pytest.raises(Invalid):
            history_client.query(sock_cfg, json.loads(req(setup["bob"], setup["cid"], max_points=0)))
        # A peer uid outside the allow-list is refused.
        server.cfg = dataclasses.replace(sock_cfg, history_allowed_uids=(os.getuid() + 1,))
        with pytest.raises(Forbidden):
            history_client.query(sock_cfg, json.loads(req(setup["alice"], setup["cid"])))
    finally:
        server.stop()
        th.stop()
