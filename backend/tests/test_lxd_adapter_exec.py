"""Exec runner (``lxd_exec.run_command``) with fake ``execute`` callables:
bounds, identity, truncation, deadline handling and the uid-1500 kill."""
from __future__ import annotations

import os
import threading
import time

import pytest

from hsm.integrations import lxd_exec
from hsm.integrations.lxd_errors import LxdRejected, LxdUncertain


class Recorder:
    """Fake execute: the main command runs ``behaviour``; the kill exec is recorded."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []
        self.killed = threading.Event()

    def __call__(self, argv, ident, out, err):
        self.calls.append((list(argv), ident))
        if argv == lxd_exec.CLEANUP_ARGV:
            self.killed.set()
            return 0
        return self.behaviour(argv, ident, out, err, self)


def run(rec, command="echo hi", *, as_root=False, deadline=20, cap=65536, overrun=0.2, grace=0.5):
    return lxd_exec.run_command(rec, command, as_root=as_root, deadline_seconds=deadline, max_output_bytes=cap,
                                overrun=overrun, grace=grace)


def test_fixed_argv_identity_and_environment(monkeypatch):
    monkeypatch.setenv("HSM_SECRET_SHOULD_NOT_LEAK", "x")
    rec = Recorder(lambda argv, ident, out, err, r: 0)
    res = run(rec, "ls; rm -rf / # $(evil)")
    argv, ident = rec.calls[0]
    assert argv == ["timeout", "-s", "KILL", "20", "/bin/sh", "-c", "ls; rm -rf / # $(evil)"]
    assert (ident.uid, ident.gid, ident.cwd) == (1500, 1500, "/home/hsm")
    env = ident.environment()
    assert env == {"PATH": lxd_exec.SAFE_PATH, "HOME": "/home/hsm", "LANG": "C.UTF-8", "TERM": "dumb",
                   "USER": "hsm"}
    assert not set(env) & (set(os.environ) - {"PATH", "HOME", "LANG", "TERM", "USER"})
    assert res["user"] == "hsm" and res["outcome"] == "completed" and res["exit_code"] == 0


def test_root_identity():
    rec = Recorder(lambda argv, ident, out, err, r: 0)
    res = run(rec, as_root=True)
    ident = rec.calls[0][1]
    assert (ident.uid, ident.gid, ident.cwd, ident.environment()["HOME"]) == (0, 0, "/root", "/root")
    assert res["user"] == "root"
    assert len(rec.calls) == 1                        # no kill -1 as root


def test_guest_cleanup_runs_after_every_command():
    rec = Recorder(lambda argv, ident, out, err, r: 3)
    res = run(rec)
    assert res["exit_code"] == 3
    assert rec.calls[1][0] == ["/bin/sh", "-c", "kill -KILL -1"]
    assert rec.calls[1][1].uid == 1500


def test_output_is_verbatim_plain_text():
    payload = b"<script>alert('x')</script>&amp;\n\xe2\x9c\x93"
    rec = Recorder(lambda argv, ident, out, err, r: (out(payload), err(b"warn\n"), 0)[2])
    res = run(rec)
    assert res["stdout"] == payload.decode("utf-8")
    assert res["stderr"] == "warn\n"
    bad = Recorder(lambda argv, ident, out, err, r: (out(b"ok\xff"), 0)[1])
    assert run(bad)["stdout"] == "ok�"


def test_combined_output_cap_and_truncation_flags():
    def chatty(argv, ident, out, err, r):
        for _ in range(64):
            out(b"o" * 16384)          # 1 MiB of stdout
        err(b"e" * 10)
        return 0
    res = run(Recorder(chatty), cap=65536)
    assert len(res["stdout"]) == 65536 and res["stdout_truncated"] is True
    assert res["stderr"] == "" and res["stderr_truncated"] is True

    def small(argv, ident, out, err, r):
        out(b"12345678")
        err(b"abcdefgh")
        return 0
    res = run(Recorder(small), cap=10)
    assert res["stdout"] == "12345678" and not res["stdout_truncated"]
    assert res["stderr"] == "ab" and res["stderr_truncated"]


def test_guest_timeout_issues_kill_and_reports_timed_out():
    def hang_until_killed(argv, ident, out, err, r):
        out(b"started\n")
        r.killed.wait(5)
        return 137
    rec = Recorder(hang_until_killed)
    res = run(rec, "sleep 60", deadline=1)
    assert rec.killed.is_set()
    assert res["outcome"] == "timed_out" and res["exit_code"] == 137 and res["stdout"] == "started\n"
    assert res["duration_ms"] >= 1000


def test_timeout_detected_by_clock_not_exit_code():
    def slow_zero(argv, ident, out, err, r):
        time.sleep(1.1)
        return 0
    res = run(Recorder(slow_zero), as_root=True, deadline=1)
    assert res["outcome"] == "timed_out" and res["exit_code"] == 0
    fast_137 = Recorder(lambda argv, ident, out, err, r: 137)
    assert run(fast_137, as_root=True)["outcome"] == "completed"


def test_root_command_that_never_returns_is_unknown():
    stuck = threading.Event()

    def forever(argv, ident, out, err, r):
        stuck.wait(30)
        return 0
    rec = Recorder(forever)
    res = run(rec, as_root=True, deadline=1, overrun=0.1, grace=0.2)
    stuck.set()
    assert res["outcome"] == "unknown" and res["exit_code"] is None
    assert not rec.killed.is_set()                    # never kill -1 as root


def test_guest_command_that_survives_kill_is_unknown():
    stuck = threading.Event()

    def ignores_kill(argv, ident, out, err, r):
        stuck.wait(30)
        return 0
    rec = Recorder(ignores_kill)
    res = run(rec, deadline=1, overrun=0.1, grace=0.2)
    stuck.set()
    assert rec.killed.is_set() and res["outcome"] == "unknown"


def test_rejected_start_raises_and_skips_cleanup():
    def refused(argv, ident, out, err, r):
        raise LxdRejected("exec: instance is not running")
    rec = Recorder(refused)
    with pytest.raises(LxdRejected):
        run(rec)
    assert len(rec.calls) == 1


def test_transport_error_after_start_is_unknown():
    def dropped(argv, ident, out, err, r):
        out(b"partial")
        raise LxdUncertain("websocket closed")
    rec = Recorder(dropped)
    res = run(rec)
    assert res["outcome"] == "unknown" and res["exit_code"] is None and res["stdout"] == "partial"
    assert rec.killed.is_set()                        # still reap the guest's processes
