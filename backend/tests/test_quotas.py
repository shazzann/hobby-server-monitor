"""Quota accounting and the concurrency invariant: racing requests cannot overspend."""
from __future__ import annotations

import threading

import pytest

from hsm import db
from hsm.auth import sessions
from hsm.errors import Conflict
from hsm.services import containers, operations, quotas

from conftest import GIB, make_container, make_user, publish_capabilities

MIB = 1024 ** 2


def admin_principal(conn, cfg):
    uid = make_user(conn, "admin@example.com", role="admin", quota=(0, 0, 0))
    return sessions.resolve(conn, cfg, sessions.create(conn, cfg, uid))


def body(owner, name, cpu=1, mem=512 * MIB, disk=2 * GIB):
    return {"name": name, "image": "hsm/alpine-3.22", "pool": "hsm-btrfs", "network": "hsmbr0",
            "cpu_cores": cpu, "cpu_allowance_pct": 100, "memory_bytes": mem, "disk_bytes": disk, "owner_id": owner}


def test_two_racing_creates_cannot_jointly_exceed_quota(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com", quota=(2, 4 * GIB, 20 * GIB))
    barrier = threading.Barrier(2)
    results = []

    def attempt(i):
        c = db.connect(cfg.sqlite_path)       # separate connections, as separate API threads would have
        barrier.wait()
        try:
            containers.submit_create(c, cfg, admin, body(owner, f"race-{i}", cpu=2), f"race-key-{i:04d}", "rid")
            results.append("ok")
        except Conflict as exc:
            results.append(exc.code)
        finally:
            c.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["QUOTA_EXCEEDED", "ok"]
    pending = quotas.owner_pending(conn, owner)
    assert pending.cpu_cores == 2


def test_pending_reservation_counts_until_released(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com", quota=(2, 4 * GIB, 20 * GIB))
    op, _, _ = containers.submit_create(conn, cfg, admin, body(owner, "one", cpu=2), "key-one-0001", "rid")
    with pytest.raises(Conflict) as exc:
        containers.submit_create(conn, cfg, admin, body(owner, "two", cpu=1), "key-two-0001", "rid")
    # Rejected by the fast bounds check (OUT_OF_BOUNDS) or the transactional one (QUOTA_EXCEEDED).
    assert exc.value.code in ("OUT_OF_BOUNDS", "QUOTA_EXCEEDED") and "cpu_cores" in exc.value.fields
    assert exc.value.extra["bounds"]["cpu_cores"]["max"] == 0
    operations.set_state(conn, op["id"], "failed", error_code="X")          # failure releases exactly once
    operations.set_state(conn, op["id"], "failed", error_code="X")
    assert quotas.owner_pending(conn, owner).cpu_cores == 0
    containers.submit_create(conn, cfg, admin, body(owner, "two", cpu=1), "key-two-0002", "rid")


def test_stopped_and_frozen_containers_still_count(cfg, conn):
    owner = make_user(conn, "owner@example.com", quota=(2, 4 * GIB, 20 * GIB))
    make_container(conn, "stopped-one", owner, cpu=2)
    conn.execute("INSERT INTO latest_metrics(container_id, state, sampled_at) SELECT id, 'Stopped', 'x' FROM containers")
    assert quotas.owner_committed(conn, owner).cpu_cores == 2


def test_host_budget_applies_reserves(cfg, conn):
    publish_capabilities(conn, cpu=4, mem=4 * GIB)        # reserves: 1 CPU, 1 GiB
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com", quota=(16, 64 * GIB, 100 * GIB))
    b = quotas.bounds(conn, cfg, owner)
    assert b["cpu_cores"]["max"] == 3
    assert b["memory_bytes"]["max"] == 3 * GIB
    with pytest.raises(Conflict) as exc:
        containers.submit_create(conn, cfg, admin, body(owner, "big", mem=4 * GIB), "key-big-0001", "rid")
    assert exc.value.code in ("OUT_OF_BOUNDS", "HOST_CAPACITY")


def test_disk_bound_uses_pool_capacity_minus_reserve(cfg, conn):
    publish_capabilities(conn, pool_total=10 * GIB, pool_used=1 * GIB)
    owner = make_user(conn, "owner@example.com", quota=(4, 4 * GIB, 100 * GIB))
    b = quotas.bounds(conn, cfg, owner)
    # logical: 10 - 2 reserve = 8 GiB; physical: 10 - 1 used - 2 reserve = 7 GiB
    assert b["disk_bytes"]["hsm-btrfs"]["max"] == 7 * GIB


def test_unlimited_unmanaged_container_blocks_new_commitments(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com")
    cid = make_container(conn, "legacy", managed=False)
    conn.execute("UPDATE containers SET observed_cpu_cores = NULL WHERE id = ?", (cid,))
    with pytest.raises(Conflict) as exc:
        containers.submit_create(conn, cfg, admin, body(owner, "new-one"), "key-new-0001", "rid")
    assert exc.value.code == "ACCOUNTING_INCOMPLETE"


def test_quota_cannot_drop_below_allocation(cfg, conn):
    owner = make_user(conn, "owner@example.com", quota=(4, 4 * GIB, 20 * GIB))
    make_container(conn, "c1", owner, cpu=2, mem=GIB, disk=2 * GIB)
    with pytest.raises(Conflict) as exc:
        quotas.check_quota_change(conn, owner, quotas.Res(1, 4 * GIB, 20 * GIB))
    assert exc.value.code == "QUOTA_BELOW_ALLOCATION"
    quotas.check_quota_change(conn, owner, quotas.Res(2, GIB, 2 * GIB))


def test_limit_increase_reserves_only_the_delta(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com", quota=(3, 4 * GIB, 20 * GIB))
    cid = make_container(conn, "c1", owner, cpu=2, mem=GIB, disk=2 * GIB)
    conn.execute("INSERT INTO latest_metrics(container_id, state, memory_bytes, sampled_at) VALUES (?, 'Running', ?, 'x')",
                 (cid, 100 * MIB))
    c = conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()
    with pytest.raises(Conflict):
        containers.submit_limits(conn, cfg, admin, c, {"version": c["version"], "cpu_cores": 4}, "lim-key-0001", "r")
    op, _ = containers.submit_limits(conn, cfg, admin, c, {"version": c["version"], "cpu_cores": 3}, "lim-key-0002", "r")
    r = conn.execute("SELECT cpu_cores, memory_bytes FROM quota_reservations WHERE operation_id = ?", (op["id"],)).fetchone()
    assert tuple(r) == (1, 0)


def test_memory_reduction_needs_confirmation_and_headroom(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com")
    cid = make_container(conn, "c1", owner, mem=GIB)
    conn.execute("INSERT INTO latest_metrics(container_id, state, memory_bytes, sampled_at) VALUES (?, 'Running', ?, 'x')",
                 (cid, 400 * MIB))
    c = conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()
    with pytest.raises(Conflict) as exc:
        containers.submit_limits(conn, cfg, admin, c, {"version": 1, "memory_bytes": 512 * MIB}, "mem-key-0001", "r")
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    with pytest.raises(Conflict) as exc:
        containers.submit_limits(conn, cfg, admin, c, {"version": 1, "memory_bytes": 448 * MIB,
                                                        "confirm_reduction": True}, "mem-key-0002", "r")
    assert exc.value.code == "BELOW_USAGE"
    containers.submit_limits(conn, cfg, admin, c, {"version": 1, "memory_bytes": 512 * MIB,
                                                   "confirm_reduction": True}, "mem-key-0003", "r")


def test_disk_is_expansion_only_and_stale_version_conflicts(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com")
    cid = make_container(conn, "c1", owner, disk=4 * GIB)
    c = conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()
    with pytest.raises(Conflict) as exc:
        containers.submit_limits(conn, cfg, admin, c, {"version": 1, "disk_bytes": 2 * GIB}, "disk-key-001", "r")
    assert exc.value.code == "OUT_OF_BOUNDS"
    with pytest.raises(Conflict) as exc:
        containers.submit_limits(conn, cfg, admin, c, {"version": 0, "disk_bytes": 5 * GIB}, "disk-key-002", "r")
    assert exc.value.code == "VERSION_CONFLICT"


def test_conflicting_operations_on_one_container_are_rejected(cfg, conn):
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com")
    cid = make_container(conn, "c1", owner)
    conn.execute("INSERT INTO latest_metrics(container_id, state, sampled_at) VALUES (?, 'Running', 'x')", (cid,))
    c = conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()
    containers.submit_action(conn, cfg, admin, c, {"action": "restart"}, "act-key-0001", "r")
    with pytest.raises(Conflict) as exc:
        containers.submit_action(conn, cfg, admin, c, {"action": "stop"}, "act-key-0002", "r")
    assert exc.value.code == "OPERATION_IN_PROGRESS"
    # Same key + same request is a replay, not a conflict.
    op, created = containers.submit_action(conn, cfg, admin, c, {"action": "restart"}, "act-key-0001", "r")
    assert created is False


def test_adoption_requires_safe_unmanaged_and_owner_quota(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com", quota=(2, 2 * GIB, 10 * GIB))
    unsafe = make_container(conn, "legacy-unsafe", managed=False, safety="unsafe")
    safe = make_container(conn, "legacy-safe", managed=False)
    req = {"owner_id": owner, "cpu_cores": 1, "cpu_allowance_pct": 50, "memory_bytes": GIB, "disk_bytes": 2 * GIB}
    row = lambda cid: conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()  # noqa: E731
    with pytest.raises(Conflict) as exc:
        containers.submit_adopt(conn, cfg, admin, row(unsafe), dict(req), "adopt-key-001", "r")
    assert exc.value.code == "CONTAINER_UNSAFE"
    with pytest.raises(Conflict) as exc:
        containers.submit_adopt(conn, cfg, admin, row(safe), {**req, "cpu_cores": 3}, "adopt-key-002", "r")
    assert exc.value.code == "QUOTA_EXCEEDED"
    op, created = containers.submit_adopt(conn, cfg, admin, row(safe), dict(req), "adopt-key-003", "r")
    assert created and op["kind"] == "adopt"
    assert quotas.owner_pending(conn, owner) == quotas.Res(1, GIB, 2 * GIB)
    managed = make_container(conn, "already", owner)
    with pytest.raises(Conflict) as exc:
        containers.submit_adopt(conn, cfg, admin, row(managed), dict(req), "adopt-key-004", "r")
    assert exc.value.code == "ALREADY_MANAGED"


def test_create_for_a_user_grants_the_owner_access_admin_owner_not(cfg, conn):
    publish_capabilities(conn)
    admin = admin_principal(conn, cfg)
    owner = make_user(conn, "owner@example.com")
    _, _, cid = containers.submit_create(conn, cfg, admin, body(owner, "for-user"), "grant-key-001", "r")
    rows = conn.execute("SELECT user_id, granted_by FROM container_access WHERE container_id = ?", (cid,)).fetchall()
    assert [tuple(r) for r in rows] == [(owner, admin.user_id)]
    conn.execute("UPDATE users SET quota_cpu_cores = 4, quota_memory_bytes = ?, quota_disk_bytes = ? WHERE id = ?",
                 (4 * GIB, 20 * GIB, admin.user_id))
    _, _, cid2 = containers.submit_create(conn, cfg, admin, body(admin.user_id, "for-admin"), "grant-key-002", "r")
    assert conn.execute("SELECT COUNT(*) FROM container_access WHERE container_id = ?", (cid2,)).fetchone()[0] == 0
