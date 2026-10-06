"""Worker behaviour against FakeAdapter and a real (temporary) SQLite DB.

Focus: reservations are released exactly once, uncertainty keeps them until
LXD state proves the outcome, crashes never double-charge, authorization and
live safety are re-checked right before dispatch, and exec is never replayed.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from conftest import GIB, grant, make_container, make_user, publish_capabilities

from hsm import worker as worker_mod
from hsm.integrations.fake_lxd import FakeAdapter
from hsm.integrations.lxd_errors import LxdRejected, LxdUncertain
from hsm.services import operations, quotas, state
from hsm.timeutil import utcnow_iso
from hsm.worker import Worker


def key() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def env(cfg, conn):
    publish_capabilities(conn)

    class Env:
        pass
    e = Env()
    e.cfg, e.conn = cfg, conn
    e.admin = make_user(conn, "admin@example.com", role="admin")
    e.user = make_user(conn, "user@example.com")
    e.fake = FakeAdapter(cfg)
    e.worker = Worker(cfg, e.fake, max_threads=0, worker_id="w-test")
    return e


def op_row(conn, op_id):
    return operations.get(conn, op_id)


def reservation(conn, op_id):
    r = conn.execute("SELECT state FROM quota_reservations WHERE operation_id = ?", (op_id,)).fetchone()
    return r["state"] if r else None


def audits(conn, op_id):
    return [dict(r) for r in conn.execute("SELECT * FROM audit_events WHERE operation_id = ? ORDER BY id", (op_id,))]


def submit_create(e, name="web-1", owner=None):
    owner = owner or e.user
    cid = str(uuid.uuid4())
    e.conn.execute("INSERT INTO containers(id, project, name, lxd_marker, managed, status, owner_id, created_at)"
                   " VALUES (?, 'hsm', ?, ?, 1, 'creating', ?, ?)", (cid, name, cid, owner, utcnow_iso()))
    payload = {"name": name, "image_alias": "hsm/alpine-3.22", "image_fingerprint": "f" * 64, "pool": "hsm-btrfs",
               "network": "hsmbr0", "cpu_cores": 2, "cpu_allowance_pct": 50, "memory_bytes": GIB,
               "disk_bytes": 2 * GIB, "owner_id": owner, "ephemeral": False, "autostart": True, "description": ""}
    op, _ = operations.submit(e.conn, e.cfg, actor_id=e.admin, kind="create", container_id=cid, payload=payload,
                              idempotency_key=key(), reserve=(owner, "hsm-btrfs", quotas.Res(2, GIB, 2 * GIB)))
    return cid, op["id"]


def managed(e, name="box-1", owner=None, status="Running", **inst_kw):
    cid = make_container(e.conn, name, owner_id=owner or e.user)
    e.fake.add_instance("hsm", name, marker=cid, status=status, **inst_kw)
    return cid


def submit(e, kind, cid, payload, actor=None, reserve=None):
    op, _ = operations.submit(e.conn, e.cfg, actor_id=actor or e.admin, kind=kind, container_id=cid,
                              payload=payload, idempotency_key=key(), reserve=reserve)
    return op["id"]


def summary(e, owner=None):
    return quotas.owner_summary(e.conn, owner or e.user)


def expire_lease(conn, op_id):
    conn.execute("UPDATE operations SET lease_expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (op_id,))


def reconcile(e):
    e.worker.reconcile_pass(e.conn)


# ------------------------------------------------------------------ create

def test_create_success_commits_and_releases_once(env):
    cid, op_id = submit_create(env)
    assert summary(env)["pending"]["cpu_cores"] == 2
    assert env.worker.run_until_idle() == 1
    op = op_row(env.conn, op_id)
    assert op["state"] == "succeeded"
    c = env.conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()
    assert c["status"] == "active" and c["cpu_cores"] == 2 and c["memory_bytes"] == GIB and c["version"] == 2
    assert c["safety"] == "safe" and c["lxd_volatile_uuid"]
    assert reservation(env.conn, op_id) == "released"
    s = summary(env)
    assert s["allocated"]["cpu_cores"] == 2 and s["pending"]["cpu_cores"] == 0
    raw = env.fake.instances[("hsm", "web-1")]
    assert raw["status"] == "Running" and raw.get("guest_user")
    assert raw["config"]["limits.cpu.allowance"] == "100ms/100ms"
    assert [a["outcome"] for a in audits(env.conn, op_id)] == ["requested", "succeeded"]


def test_rejected_create_releases_exactly_once_and_tombstones(env):
    cid, op_id = submit_create(env)
    env.fake.fail("create", LxdRejected("create: image not found"))
    env.worker.run_until_idle()
    op = op_row(env.conn, op_id)
    assert op["state"] == "failed" and op["error_code"] == "LXD_REJECTED"
    assert reservation(env.conn, op_id) == "released"
    released_at = env.conn.execute("SELECT released_at FROM quota_reservations WHERE operation_id = ?",
                                   (op_id,)).fetchone()[0]
    assert env.conn.execute("SELECT status FROM containers WHERE id = ?", (cid,)).fetchone()[0] == "deleted"
    s = summary(env)
    assert s["allocated"]["cpu_cores"] == 0 and s["pending"]["cpu_cores"] == 0
    # Later passes must not touch it again.
    reconcile(env)
    env.worker.run_until_idle()
    assert env.conn.execute("SELECT released_at FROM quota_reservations WHERE operation_id = ?",
                            (op_id,)).fetchone()[0] == released_at
    assert [a["outcome"] for a in audits(env.conn, op_id)] == ["requested", "failed"]


def test_uncertain_create_keeps_reservation_until_reconciled_absent(env):
    cid, op_id = submit_create(env)
    env.fake.fail("create", LxdUncertain("create: read timeout"))
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "reconciling"
    assert reservation(env.conn, op_id) == "pending"
    reconcile(env)                                   # within the settle window: unchanged
    assert op_row(env.conn, op_id)["state"] == "reconciling"
    assert summary(env)["pending"]["cpu_cores"] == 2
    env.fake.available = False                       # LXD down: still unchanged
    env.worker.settle_seconds["create"] = 0
    reconcile(env)
    assert op_row(env.conn, op_id)["state"] == "reconciling"
    env.fake.available = True
    reconcile(env)
    op = op_row(env.conn, op_id)
    assert op["state"] == "failed" and op["error_code"] == "LXD_NOT_CREATED"
    assert reservation(env.conn, op_id) == "released"
    assert summary(env)["allocated"]["cpu_cores"] == 0


def test_uncertain_create_that_happened_is_finished_by_reconcile(env):
    cid, op_id = submit_create(env)
    env.fake.fail("create", LxdUncertain("create: connection reset"), apply=True)
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "reconciling"
    reconcile(env)
    assert op_row(env.conn, op_id)["state"] == "succeeded"
    s = summary(env)
    assert s["allocated"]["cpu_cores"] == 2 and s["pending"]["cpu_cores"] == 0
    assert env.fake.instances[("hsm", "web-1")]["status"] == "Running"


def _crash_mid_create(env, *, effect_happened: bool):
    cid, op_id = submit_create(env)
    claimed = operations.claim_next(env.conn, "dead-worker", 60)
    assert claimed["id"] == op_id
    if effect_happened:
        p = json.loads(claimed["payload"])
        env.fake.create("hsm", "web-1", image_fingerprint=p["image_fingerprint"], pool=p["pool"],
                        network=p["network"], cpu_cores=2, cpu_allowance_pct=50, memory_bytes=GIB,
                        disk_bytes=2 * GIB, ephemeral=False, autostart=True, description="", marker=cid)
    expire_lease(env.conn, op_id)
    return cid, op_id


def test_crash_after_external_effect_does_not_double_charge(env):
    cid, op_id = _crash_mid_create(env, effect_happened=True)
    reconcile(env)
    assert op_row(env.conn, op_id)["state"] == "succeeded"
    reconcile(env)
    env.worker.run_until_idle()
    s = summary(env)
    assert s["allocated"]["cpu_cores"] == 2 and s["pending"]["cpu_cores"] == 0
    assert len([c for c in env.fake.calls if c[0] == "create"]) == 1     # never re-created


def test_crash_before_external_effect_releases_after_settle(env):
    cid, op_id = _crash_mid_create(env, effect_happened=False)
    reconcile(env)
    op = op_row(env.conn, op_id)
    assert op["state"] == "reconciling" and op["error_code"] == "LEASE_EXPIRED"
    assert reservation(env.conn, op_id) == "pending"
    env.worker.settle_seconds["create"] = 0
    reconcile(env)
    assert op_row(env.conn, op_id)["state"] == "failed"
    s = summary(env)
    assert s["allocated"]["cpu_cores"] == 0 and s["pending"]["cpu_cores"] == 0
    assert not [c for c in env.fake.calls if c[0] == "create"]           # no blind retry


def test_rejected_create_that_exists_anyway_is_completed(env):
    cid, op_id = submit_create(env)
    env.fake.fail("create", LxdRejected("create: odd error"), apply=True)
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "succeeded"


def test_create_refuses_taken_name_and_full_pool(env):
    env.fake.add_instance("hsm", "web-1", marker=None)
    _, op_id = submit_create(env)
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "NAME_TAKEN"
    env.fake.pool_free = 1
    _, op2 = submit_create(env, name="web-2")
    env.worker.run_until_idle()
    assert op_row(env.conn, op2)["error_code"] == "POOL_FULL"
    assert summary(env)["pending"]["cpu_cores"] == 0


def test_create_with_lxd_down_fails_cleanly(env):
    _, op_id = submit_create(env)
    env.fake.available = False
    env.worker.run_until_idle()
    op = op_row(env.conn, op_id)
    assert op["state"] == "failed" and op["error_code"] == "LXD_UNAVAILABLE"
    assert reservation(env.conn, op_id) == "released"


# ------------------------------------------------------------------ authorization at dispatch

def _exec_op(env, cid, actor, command="id", as_root=False):
    return submit(env, "exec", cid, {"command": command, "as_root": as_root}, actor=actor)


def test_revoked_actor_queued_exec_is_not_dispatched(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    op_id = _exec_op(env, cid, env.user)
    env.conn.execute("UPDATE users SET status = 'revoked' WHERE id = ?", (env.user,))
    env.worker.run_until_idle()
    op = op_row(env.conn, op_id)
    assert op["state"] == "failed" and op["error_code"] == "ACTOR_NOT_AUTHORIZED"
    assert not [c for c in env.fake.calls if c[0] == "exec_command"]
    assert audits(env.conn, op_id)[-1]["outcome"] == "denied"


def test_unassigned_actor_queued_exec_is_not_dispatched(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    op_id = _exec_op(env, cid, env.user)
    env.conn.execute("DELETE FROM container_access WHERE user_id = ?", (env.user,))
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "ACTOR_NOT_AUTHORIZED"
    assert not [c for c in env.fake.calls if c[0] == "exec_command"]


def test_demoted_admin_cannot_exec_as_root(env):
    admin2 = make_user(env.conn, "admin2@example.com", role="admin")
    cid = managed(env)
    grant(env.conn, cid, admin2)                     # still assigned after demotion
    op_id = _exec_op(env, cid, admin2, as_root=True)
    env.conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (admin2,))
    env.worker.run_until_idle()
    op = op_row(env.conn, op_id)
    assert op["state"] == "failed" and op["error_code"] == "ACTOR_NOT_AUTHORIZED"
    assert not [c for c in env.fake.calls if c[0] == "exec_command"]


def test_demoted_admin_cannot_manage(env):
    admin2 = make_user(env.conn, "admin2@example.com", role="admin")
    cid = managed(env)
    op_id = submit(env, "stop", cid, {"force": False}, actor=admin2)
    env.conn.execute("UPDATE users SET role = 'user' WHERE id = ?", (admin2,))
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "ACTOR_NOT_AUTHORIZED"
    assert env.fake.instances[("hsm", "box-1")]["status"] == "Running"


# ------------------------------------------------------------------ live safety

def test_unsafe_live_config_blocks_exec_and_mutation(env):
    cid = managed(env, extra_config={"security.privileged": "true"})
    grant(env.conn, cid, env.user)
    ex = _exec_op(env, cid, env.user)
    env.worker.run_until_idle()
    assert op_row(env.conn, ex)["error_code"] == "CONTAINER_UNSAFE"
    lim = submit(env, "update_limits", cid,
                 {"before": {"cpu_cores": 1, "cpu_allowance_pct": 100, "memory_bytes": GIB, "disk_bytes": 2 * GIB},
                  "after": {"cpu_cores": 2, "cpu_allowance_pct": 100, "memory_bytes": GIB, "disk_bytes": 2 * GIB}},
                 reserve=(env.user, "hsm-btrfs", quotas.Res(1, 0, 0)))
    env.worker.run_until_idle()
    assert op_row(env.conn, lim)["error_code"] == "CONTAINER_UNSAFE"
    assert reservation(env.conn, lim) == "released"
    assert not [c for c in env.fake.calls if c[0] in ("exec_command", "update_limits")]
    # Stopping an unsafe container is still allowed (it reduces exposure).
    st = submit(env, "stop", cid, {"force": True})
    env.worker.run_until_idle()
    assert op_row(env.conn, st)["state"] == "succeeded"


def test_host_mount_added_by_profile_blocks_exec(env):
    cid = managed(env, extra_devices={"data": {"type": "disk", "source": "/srv", "path": "/mnt"}})
    grant(env.conn, cid, env.user)
    ex = _exec_op(env, cid, env.user)
    env.worker.run_until_idle()
    assert op_row(env.conn, ex)["error_code"] == "CONTAINER_UNSAFE"


def test_copied_marker_is_ambiguous(env):
    cid = managed(env)
    env.fake.add_instance("hsm", "box-copy", marker=cid)
    op_id = submit(env, "restart", cid, {})
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "IDENTITY_AMBIGUOUS"


# ------------------------------------------------------------------ exec

def test_exec_result_stored_verbatim_and_not_audited(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    cmd = "echo '<script>alert(1)</script> & \"x\"'"
    op_id = _exec_op(env, cid, env.user, command=cmd)
    env.worker.run_until_idle()
    op = op_row(env.conn, op_id)
    assert op["state"] == "succeeded"
    result = json.loads(op["result"])
    assert result["stdout"] == cmd and result["user"] == "hsm"          # fake echoes; no escaping applied
    assert op["result_expires_at"]
    events = audits(env.conn, op_id)
    assert [e["action"] for e in events] == ["container.exec"]
    details = json.loads(events[0]["details"])
    assert set(details) == {"exit_code", "outcome", "duration_ms", "user"}
    assert "alert" not in json.dumps(events)


def test_exec_not_running(env):
    cid = managed(env, status="Stopped")
    grant(env.conn, cid, env.user)
    op_id = _exec_op(env, cid, env.user)
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "NOT_RUNNING"


def test_exec_unknown_outcome_is_failed_with_partial_result(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    env.fake.exec_handler = lambda command, as_root: {
        "outcome": "unknown", "exit_code": None, "stdout": "partial", "stderr": "", "stdout_truncated": False,
        "stderr_truncated": False, "duration_ms": 33000, "user": "hsm"}
    op_id = _exec_op(env, cid, env.user, command="sleep 999")
    env.worker.run_until_idle()
    op = op_row(env.conn, op_id)
    assert op["state"] == "failed" and op["error_code"] == "OUTCOME_UNKNOWN"
    assert json.loads(op["result"])["stdout"] == "partial"


def test_exec_transport_uncertainty_is_never_reconciled_or_retried(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    env.fake.fail("exec_command", LxdUncertain("exec: websocket dropped"))
    op_id = _exec_op(env, cid, env.user)
    env.worker.run_until_idle()
    reconcile(env)
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "OUTCOME_UNKNOWN"
    assert len([c for c in env.fake.calls if c[0] == "exec_command"]) == 1


def test_exec_in_expired_lease_is_failed_not_replayed(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    op_id = _exec_op(env, cid, env.user)
    operations.claim_next(env.conn, "dead-worker", 60)
    expire_lease(env.conn, op_id)
    reconcile(env)
    env.worker.run_until_idle()
    op = op_row(env.conn, op_id)
    assert op["state"] == "failed" and op["error_code"] == "OUTCOME_UNKNOWN"
    assert not [c for c in env.fake.calls if c[0] == "exec_command"]


def test_stale_queued_exec_is_dropped(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    op_id = _exec_op(env, cid, env.user)
    env.conn.execute("UPDATE operations SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?", (op_id,))
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "EXEC_EXPIRED"


def test_admin_exec_as_root(env):
    cid = managed(env)
    op_id = _exec_op(env, cid, env.admin, as_root=True)
    env.worker.run_until_idle()
    assert json.loads(op_row(env.conn, op_id)["result"])["user"] == "root"


# ------------------------------------------------------------------ limits

def _limits(cores=1, pct=100, mem=GIB, disk=2 * GIB):
    return {"cpu_cores": cores, "cpu_allowance_pct": pct, "memory_bytes": mem, "disk_bytes": disk}


def test_update_limits_success(env):
    cid = managed(env)
    op_id = submit(env, "update_limits", cid, {"before": _limits(), "after": _limits(2, 50, 2 * GIB, 4 * GIB)},
                   reserve=(env.user, "hsm-btrfs", quotas.Res(1, GIB, 2 * GIB)))
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "succeeded"
    c = env.conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()
    assert (c["cpu_cores"], c["cpu_allowance_pct"], c["memory_bytes"], c["disk_bytes"], c["version"]) == \
        (2, 50, 2 * GIB, 4 * GIB, 2)
    raw = env.fake.instances[("hsm", "box-1")]
    assert raw["config"]["limits.cpu.allowance"] == "100ms/100ms" and raw["devices"]["root"]["size"] == "4096MiB"
    s = summary(env)
    assert s["allocated"]["cpu_cores"] == 2 and s["pending"]["cpu_cores"] == 0
    details = json.loads(audits(env.conn, op_id)[-1]["details"])
    assert details["before"]["cpu_cores"] == 1 and details["after"]["cpu_cores"] == 2


def test_disk_shrink_rejected_before_lxd(env):
    cid = managed(env, disk_bytes=4 * GIB)
    env.conn.execute("UPDATE containers SET disk_bytes = ? WHERE id = ?", (4 * GIB, cid))
    op_id = submit(env, "update_limits", cid, {"before": _limits(disk=4 * GIB), "after": _limits(disk=2 * GIB)})
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["error_code"] == "DISK_SHRINK"
    assert not [c for c in env.fake.calls if c[0] == "update_limits"]


def test_update_limits_uncertain_reconciles_by_observed_config(env):
    cid = managed(env)
    applied = submit(env, "update_limits", cid, {"before": _limits(), "after": _limits(2)},
                     reserve=(env.user, None, quotas.Res(1, 0, 0)))
    env.fake.fail("update_limits", LxdUncertain("patch: timeout"), apply=True)
    env.worker.run_until_idle()
    assert op_row(env.conn, applied)["state"] == "reconciling" and reservation(env.conn, applied) == "pending"
    reconcile(env)
    assert op_row(env.conn, applied)["state"] == "succeeded"
    assert summary(env)["allocated"]["cpu_cores"] == 2 and summary(env)["pending"]["cpu_cores"] == 0

    not_applied = submit(env, "update_limits", cid, {"before": _limits(2), "after": _limits(3)},
                         reserve=(env.user, None, quotas.Res(1, 0, 0)))
    env.fake.fail("update_limits", LxdUncertain("patch: timeout"))
    env.worker.run_until_idle()
    reconcile(env)
    op = op_row(env.conn, not_applied)
    assert op["state"] == "failed" and op["error_code"] == "LXD_NOT_APPLIED"
    assert reservation(env.conn, not_applied) == "released"


# ------------------------------------------------------------------ lifecycle / delete

def test_stop_ephemeral_tombstones(env):
    cid = managed(env, ephemeral=True)
    grant(env.conn, cid, env.user)
    op_id = submit(env, "stop", cid, {"force": False})
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "succeeded"
    assert env.conn.execute("SELECT status FROM containers WHERE id = ?", (cid,)).fetchone()[0] == "deleted"
    assert env.conn.execute("SELECT COUNT(*) FROM container_access WHERE container_id = ?", (cid,)).fetchone()[0] == 0


def test_lifecycle_rejected_and_uncertain(env):
    cid = managed(env)
    env.fake.fail("freeze", LxdRejected("freeze: cgroup error"))
    a = submit(env, "freeze", cid, {})
    env.worker.run_until_idle()
    assert op_row(env.conn, a)["error_code"] == "LXD_REJECTED"
    env.fake.fail("restart", LxdUncertain("restart: timeout"), apply=True)
    b = submit(env, "restart", cid, {})
    env.worker.run_until_idle()
    assert op_row(env.conn, b)["state"] == "reconciling"
    reconcile(env)
    assert op_row(env.conn, b)["state"] == "succeeded"


def test_lifecycle_not_observed_fails_after_settle(env):
    cid = managed(env)
    env.fake.fail("freeze", LxdUncertain("freeze: timeout"))
    op_id = submit(env, "freeze", cid, {})
    env.worker.run_until_idle()
    reconcile(env)
    assert op_row(env.conn, op_id)["state"] == "reconciling"
    env.worker.settle_seconds["freeze"] = 0
    reconcile(env)
    assert op_row(env.conn, op_id)["error_code"] == "OUTCOME_NOT_OBSERVED"


def test_delete_running_container(env):
    cid = managed(env)
    grant(env.conn, cid, env.user)
    op_id = submit(env, "delete", cid, {"name": "box-1"})
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "succeeded"
    assert ("hsm", "box-1") not in env.fake.instances
    names = [c[0] for c in env.fake.calls]
    assert names.index("stop") < names.index("delete")
    c = env.conn.execute("SELECT status, deleted_at FROM containers WHERE id = ?", (cid,)).fetchone()
    assert c["status"] == "deleted" and c["deleted_at"]
    assert summary(env)["allocated"]["cpu_cores"] == 0


def test_delete_uncertain_reconciles_by_marker(env):
    cid = managed(env, status="Stopped")
    env.fake.fail("delete", LxdUncertain("delete: timeout"), apply=True)
    op_id = submit(env, "delete", cid, {"name": "box-1"})
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "reconciling"
    reconcile(env)
    assert op_row(env.conn, op_id)["state"] == "succeeded"


# ------------------------------------------------------------------ adopt

def test_adopt_unmanaged(env):
    cid = make_container(env.conn, "legacy", managed=False)
    env.fake.add_instance("hsm", "legacy", marker=None)
    op_id = submit(env, "adopt", cid, {"owner_id": env.user, **_limits(1, 50, GIB, 4 * GIB)},
                   reserve=(env.user, "hsm-btrfs", quotas.Res(1, GIB, 4 * GIB)))
    env.worker.run_until_idle()
    assert op_row(env.conn, op_id)["state"] == "succeeded"
    c = env.conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()
    assert c["managed"] == 1 and c["lxd_marker"] == cid and c["owner_id"] == env.user and c["cpu_cores"] == 1
    assert env.fake.instances[("hsm", "legacy")]["config"]["user.hsm.id"] == cid
    assert reservation(env.conn, op_id) == "released"


# ------------------------------------------------------------------ loop

def test_threaded_loop_heartbeat_and_lease(env, monkeypatch):
    monkeypatch.setattr(worker_mod, "IDLE_POLL", 0.05)
    w = Worker(env.cfg, env.fake, max_threads=2, worker_id="w-loop")
    a, b = managed(env, "box-a"), managed(env, "box-b")
    ops = [submit(env, "restart", a, {}), submit(env, "freeze", b, {})]
    stop = threading.Event()
    t = threading.Thread(target=w.run, args=(stop,), daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and any(op_row(env.conn, o)["state"] != "succeeded" for o in ops):
        time.sleep(0.05)
    stop.set()
    t.join(10)
    assert not t.is_alive()
    assert all(op_row(env.conn, o)["state"] == "succeeded" for o in ops)
    hb = state.get(env.conn, state.WORKER_HEARTBEAT)
    assert hb["worker_id"] == "w-loop" and hb["at"]


def test_finalize_refuses_when_op_was_taken_over(env):
    cid = managed(env)
    op_id = submit(env, "restart", cid, {})
    claimed = operations.claim_next(env.conn, "w-test", 60)
    # Simulate the reconciler having taken it over while this thread was slow.
    env.conn.execute("UPDATE operations SET state = 'reconciling' WHERE id = ?", (op_id,))
    ctx = worker_mod.Ctx(env.conn, claimed, {}, "running")
    with pytest.raises(worker_mod.LeaseLost):
        env.worker._succeed(ctx)
    assert op_row(env.conn, op_id)["state"] == "reconciling"
