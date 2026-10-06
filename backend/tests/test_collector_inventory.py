"""Inventory reconciliation and the collection cycle against a fake LXD source."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from conftest import grant, make_container, make_user
from test_collector_fakes import FakeSource, make_instance

from hsm.collector.cycle import Collector
from hsm.services import state
from hsm.timeutil import utcnow_iso

T0 = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.i = 0

    def tick(self, c: Collector, dt: float = 10.0):
        self.i += 1
        return c.run_cycle(wall=T0 + timedelta(seconds=dt * self.i), mono=1000.0 + dt * self.i)


def row(conn, cid):
    return conn.execute("SELECT * FROM containers WHERE id = ?", (cid,)).fetchone()


def live_rows(conn):
    return conn.execute("SELECT * FROM containers WHERE status != 'deleted' ORDER BY name").fetchall()


def test_unmanaged_instances_are_inserted_once_and_metrics_upserted(cfg, conn):
    src = FakeSource([make_instance("ext-1", project="default", cpu_ns=0),
                      make_instance("ext-2", project="other", status="Stopped")])
    c, clk = Collector(cfg, conn, src), Clock()
    hb = clk.tick(c)
    rows = live_rows(conn)
    assert [(r["project"], r["name"], r["managed"], r["owner_id"]) for r in rows] == \
           [("default", "ext-1", 0, None), ("other", "ext-2", 0, None)]
    ids = {r["id"] for r in rows}
    src.instances[0]["state"]["cpu"]["usage"] = 2_000_000_000
    clk.tick(c)
    assert {r["id"] for r in live_rows(conn)} == ids       # stable identity, no duplicates
    m = conn.execute("SELECT * FROM latest_metrics WHERE container_id = ?",
                     (next(r["id"] for r in rows if r["name"] == "ext-1"),)).fetchone()
    assert m["state"] == "Running" and m["cpu_pct"] == 20.0 and m["ipv4"] == "10.0.0.2"
    assert m["memory_limit_bytes"] == 512 * 2 ** 20 and m["disk_limit_bytes"] == 2 * 1024 ** 3
    hb = state.get(conn, state.COLLECTOR_HEARTBEAT)
    assert hb["lxd_available"] is True and hb["containers"] == 2 and hb["last_success_at"]
    r = next(r for r in rows if r["name"] == "ext-1")
    assert r["safety"] == "safe" and r["observed_cpu_cores"] == 1 and r["os"] == "Alpine"
    assert state.get(conn, state.LXD_CAPABILITIES)["host"]["cpu_count"] == 8


def test_rename_preserves_identity_and_grants(cfg, conn):
    owner = make_user(conn, "u@example.com")
    cid = make_container(conn, "web-1", owner_id=owner)
    grant(conn, cid, owner)
    src = FakeSource([make_instance("web-1", marker=cid)])
    c, clk = Collector(cfg, conn, src), Clock()
    clk.tick(c)
    src.instances = [make_instance("web-renamed", marker=cid)]
    clk.tick(c)
    r = row(conn, cid)
    assert r["name"] == "web-renamed" and r["status"] == "active" and r["owner_id"] == owner
    assert conn.execute("SELECT COUNT(*) FROM container_access WHERE container_id = ?", (cid,)).fetchone()[0] == 1
    assert len(live_rows(conn)) == 1


def test_rename_swap_does_not_trip_unique_index(cfg, conn):
    a, b = make_container(conn, "a"), make_container(conn, "b")
    src = FakeSource([make_instance("b", marker=a), make_instance("a", marker=b)])
    Clock().tick(Collector(cfg, conn, src))
    assert row(conn, a)["name"] == "b" and row(conn, b)["name"] == "a"


def test_external_delete_then_name_reuse_gets_new_uuid_and_no_grants(cfg, conn):
    user = make_user(conn, "u@example.com")
    cid = make_container(conn, "web", owner_id=user)
    grant(conn, cid, user)
    src = FakeSource([make_instance("web", marker=cid)])
    c, clk = Collector(cfg, conn, src), Clock()
    clk.tick(c)
    assert conn.execute("SELECT 1 FROM latest_metrics WHERE container_id = ?", (cid,)).fetchone()
    src.instances = []
    clk.tick(c)
    r = row(conn, cid)
    assert r["status"] == "deleted" and r["deleted_at"]
    assert not conn.execute("SELECT 1 FROM container_access WHERE container_id = ?", (cid,)).fetchone()
    assert not conn.execute("SELECT 1 FROM latest_metrics WHERE container_id = ?", (cid,)).fetchone()
    ev = conn.execute("SELECT * FROM audit_events WHERE action = 'container.external_delete'").fetchone()
    assert ev["target_id"] == cid and ev["actor_email"] == "system"
    src.instances = [make_instance("web")]          # same name, no marker
    clk.tick(c)
    (new,) = live_rows(conn)
    assert new["id"] != cid and new["managed"] == 0 and new["owner_id"] is None
    assert not conn.execute("SELECT 1 FROM container_access WHERE container_id = ?", (new["id"],)).fetchone()


def test_same_cycle_reuse_distinguished_by_volatile_uuid(cfg, conn):
    cid = make_container(conn, "web")
    conn.execute("UPDATE containers SET lxd_volatile_uuid = 'vu-old' WHERE id = ?", (cid,))
    src = FakeSource([make_instance("web", vuuid="vu-new")])     # marker gone, different instance
    Clock().tick(Collector(cfg, conn, src))
    assert row(conn, cid)["status"] == "deleted"
    (new,) = live_rows(conn)
    assert new["id"] != cid and new["name"] == "web"


def test_stripped_marker_without_identity_evidence_is_quarantined(cfg, conn):
    cid = make_container(conn, "web")
    src = FakeSource([make_instance("web")])
    Clock().tick(Collector(cfg, conn, src))
    assert row(conn, cid)["status"] == "quarantined"
    assert len(live_rows(conn)) == 1


def test_lxd_outage_keeps_running_and_does_not_tombstone(cfg, conn):
    cid = make_container(conn, "web")
    src = FakeSource([make_instance("web", marker=cid)])
    c, clk = Collector(cfg, conn, src), Clock()
    clk.tick(c)
    src.fail = True
    for _ in range(3):
        hb = clk.tick(c)
    assert hb["lxd_available"] is False and hb["error"] == "LXD unavailable"
    assert row(conn, cid)["status"] == "active"
    assert conn.execute("SELECT 1 FROM latest_metrics WHERE container_id = ?", (cid,)).fetchone()  # kept, ages stale
    src.fail = False
    hb = clk.tick(c)
    assert hb["lxd_available"] is True and row(conn, cid)["status"] == "active"


def test_active_operation_blocks_tombstone_and_creating_rows_untouched(cfg, conn):
    admin = make_user(conn, "a@example.com", role="admin")
    busy = make_container(conn, "busy")
    creating = make_container(conn, "new-one", status="creating")
    conn.execute("INSERT INTO operations(id, actor_id, container_id, kind, request_hash, idempotency_key, state,"
                 " created_at) VALUES ('op1', ?, ?, 'delete', 'h', 'k', 'running', ?)", (admin, busy, utcnow_iso()))
    src = FakeSource([make_instance("new-one", marker=creating, limits_cpu="4")])
    Clock().tick(Collector(cfg, conn, src))
    assert row(conn, busy)["status"] == "active"
    r = row(conn, creating)
    assert r["status"] == "creating" and r["observed_cpu_cores"] == 1 and r["last_seen_at"] is None


def test_duplicate_marker(cfg, conn):
    cid = make_container(conn, "orig")
    conn.execute("UPDATE containers SET lxd_volatile_uuid = 'vu-1' WHERE id = ?", (cid,))
    src = FakeSource([make_instance("orig", marker=cid, vuuid="vu-1"), make_instance("copy", marker=cid, vuuid="vu-2")])
    c, clk = Collector(cfg, conn, src), Clock()
    clk.tick(c)
    clk.tick(c)
    assert row(conn, cid)["status"] == "active" and row(conn, cid)["name"] == "orig"
    assert len(live_rows(conn)) == 1                      # the copy gets no row with our id
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE action = 'container.duplicate_marker'"
                        ).fetchone()[0] == 1              # audited once, not every cycle
    # Neither carrier matches the recorded identity -> quarantine.
    src.instances = [make_instance("x", marker=cid, vuuid="vu-3"), make_instance("y", marker=cid, vuuid="vu-4")]
    clk.tick(c)
    assert row(conn, cid)["status"] == "quarantined"


def test_malformed_container_does_not_break_others(cfg, conn):
    good = make_instance("good", cpu_ns=0)
    bad = make_instance("bad")
    bad["state"]["cpu"] = "garbage"
    src = FakeSource([good, bad])
    hb = Clock().tick(Collector(cfg, conn, src))
    assert hb["containers"] == 1 and "unreadable" in hb["error"]
    names = {r["name"] for r in conn.execute(
        "SELECT c.name FROM latest_metrics m JOIN containers c ON c.id = m.container_id")}
    assert names == {"good"}


def test_unsafe_unmanaged_container_outside_project_is_labelled(cfg, conn):
    inst = make_instance("host-thing", project="default")
    inst["expanded_config"]["security.privileged"] = "true"
    Clock().tick(Collector(cfg, conn, FakeSource([inst])))
    (r,) = live_rows(conn)
    assert r["safety"] == "unsafe" and "security.privileged is enabled." in json.loads(r["safety_reasons"])
