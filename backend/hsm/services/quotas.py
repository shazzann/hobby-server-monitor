"""Quota and capacity accounting.

Quota = configured *allocations*, not instantaneous consumption. A container's
allocation is charged once, to its single resource owner; access grants are
free. Running, stopped and frozen containers all count, as do pending positive
reservations of queued/running operations.

    owner_remaining  = owner_quota - committed(owner) - pending(owner)
    host_remaining   = host_budget - committed(all)   - pending(all)
    allowed_increase = min(owner_remaining, host_remaining)

All functions here read inside the caller's transaction. ``check_and_reserve``
must be called inside BEGIN IMMEDIATE so that two racing requests are
serialized by SQLite's write lock and cannot jointly overspend.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

from ..config import Config
from ..errors import Conflict
from . import state

MIB = 1024 ** 2
GIB = 1024 ** 3
MIN_MEMORY_BYTES = 128 * MIB
MIN_DISK_BYTES = 1 * GIB
MIN_ALLOWANCE_PCT = 10


@dataclass(frozen=True)
class Res:
    cpu_cores: int = 0
    memory_bytes: int = 0
    disk_bytes: int = 0

    def __add__(self, o: "Res") -> "Res":
        return Res(self.cpu_cores + o.cpu_cores, self.memory_bytes + o.memory_bytes, self.disk_bytes + o.disk_bytes)

    def __sub__(self, o: "Res") -> "Res":
        return Res(self.cpu_cores - o.cpu_cores, self.memory_bytes - o.memory_bytes, self.disk_bytes - o.disk_bytes)

    def positive(self) -> "Res":
        return Res(max(0, self.cpu_cores), max(0, self.memory_bytes), max(0, self.disk_bytes))

    def as_dict(self) -> dict:
        return asdict(self)


def owner_quota(conn: sqlite3.Connection, owner_id: str) -> Res:
    r = conn.execute("SELECT quota_cpu_cores, quota_memory_bytes, quota_disk_bytes FROM users WHERE id = ?",
                     (owner_id,)).fetchone()
    return Res(r[0], r[1], r[2]) if r else Res()


def owner_committed(conn: sqlite3.Connection, owner_id: str) -> Res:
    r = conn.execute(
        "SELECT COALESCE(SUM(cpu_cores),0), COALESCE(SUM(memory_bytes),0), COALESCE(SUM(disk_bytes),0)"
        " FROM containers WHERE owner_id = ? AND status != 'deleted' AND cpu_cores IS NOT NULL",
        (owner_id,)).fetchone()
    return Res(r[0], r[1], r[2])


def owner_pending(conn: sqlite3.Connection, owner_id: str) -> Res:
    r = conn.execute(
        "SELECT COALESCE(SUM(cpu_cores),0), COALESCE(SUM(memory_bytes),0), COALESCE(SUM(disk_bytes),0)"
        " FROM quota_reservations WHERE owner_id = ? AND state = 'pending'", (owner_id,)).fetchone()
    return Res(r[0], r[1], r[2])


def owner_summary(conn: sqlite3.Connection, owner_id: str) -> dict:
    q, c, p = owner_quota(conn, owner_id), owner_committed(conn, owner_id), owner_pending(conn, owner_id)
    return {"quota": q.as_dict(), "allocated": c.as_dict(), "pending": p.as_dict(),
            "remaining": (q - c - p).as_dict()}


@dataclass
class HostView:
    known: bool                    # False until the collector has published capabilities
    cpu_total: int
    memory_total: int
    cpu_budget: int
    memory_budget: int
    cpu_allocated: int
    memory_allocated: int
    cpu_pending: int
    memory_pending: int
    pools: dict                    # name -> {total, used, budget, allocated, pending, driver, quota_capable}
    incomplete: list               # [{container_id, name, missing: [...]}]

    @property
    def cpu_remaining(self) -> int:
        return self.cpu_budget - self.cpu_allocated - self.cpu_pending

    @property
    def memory_remaining(self) -> int:
        return self.memory_budget - self.memory_allocated - self.memory_pending

    def pool_remaining(self, pool: str) -> int:
        """Logical budget left, also capped by real free space minus the reserve
        (no deliberate overcommit of a thin pool)."""
        p = self.pools.get(pool)
        if not p:
            return 0
        logical = p["budget"] - p["allocated"] - p["pending"]
        physical = p["total"] - p["used"] - p["reserve"]
        return max(0, min(logical, physical))


def host_view(conn: sqlite3.Connection, cfg: Config) -> HostView:
    caps = state.get(conn, state.LXD_CAPABILITIES) or {}
    host = caps.get("host") or {}
    cpu_total = int(host.get("cpu_count") or 0)
    mem_total = int(host.get("memory_bytes") or 0)

    incomplete = []
    cpu_alloc = mem_alloc = 0
    disk_alloc: dict[str, int] = {}
    rows = conn.execute(
        "SELECT id, name, managed, cpu_cores, memory_bytes, disk_bytes, pool, observed_cpu_cores,"
        " observed_memory_bytes, observed_disk_bytes, observed_pool, status FROM containers"
        " WHERE status IN ('active', 'quarantined')").fetchall()
    for r in rows:
        cpu = r["cpu_cores"] if r["cpu_cores"] is not None else r["observed_cpu_cores"]
        mem = r["memory_bytes"] if r["memory_bytes"] is not None else r["observed_memory_bytes"]
        disk = r["disk_bytes"] if r["disk_bytes"] is not None else r["observed_disk_bytes"]
        pool = r["pool"] or r["observed_pool"]
        missing = [k for k, v in (("cpu", cpu), ("memory", mem), ("disk", disk)) if v is None]
        if missing:
            incomplete.append({"container_id": r["id"], "name": r["name"], "missing": missing})
        cpu_alloc += cpu or 0
        mem_alloc += mem or 0
        if pool and disk:
            disk_alloc[pool] = disk_alloc.get(pool, 0) + disk

    pend_cpu, pend_mem = conn.execute(
        "SELECT COALESCE(SUM(cpu_cores),0), COALESCE(SUM(memory_bytes),0) FROM quota_reservations"
        " WHERE state = 'pending'").fetchone()
    disk_pend = {r[0]: r[1] for r in conn.execute(
        "SELECT pool, COALESCE(SUM(disk_bytes),0) FROM quota_reservations WHERE state = 'pending'"
        " AND pool IS NOT NULL GROUP BY pool")}

    pools = {}
    for p in caps.get("pools") or []:
        total = int(p.get("total_bytes") or 0)
        pools[p["name"]] = {
            "name": p["name"], "driver": p.get("driver"), "quota_capable": bool(p.get("quota_capable")),
            "total": total, "used": int(p.get("used_bytes") or 0), "reserve": cfg.pool_reserve_bytes,
            "budget": max(0, total - cfg.pool_reserve_bytes),
            "allocated": disk_alloc.get(p["name"], 0), "pending": disk_pend.get(p["name"], 0),
        }

    return HostView(
        known=bool(cpu_total and mem_total),
        cpu_total=cpu_total, memory_total=mem_total,
        cpu_budget=max(0, cpu_total - cfg.host_cpu_reserve - cfg.external_reserve_cpu_cores),
        memory_budget=max(0, mem_total - cfg.host_memory_reserve_bytes - cfg.external_reserve_memory_bytes),
        cpu_allocated=cpu_alloc, memory_allocated=mem_alloc,
        cpu_pending=pend_cpu, memory_pending=pend_mem, pools=pools, incomplete=incomplete,
    )


def accounting_blocker(cfg: Config, host: HostView, pool: str | None) -> str | None:
    """Reason new commitments are blocked, or None."""
    if not host.known:
        return "Host capacity is not known yet; the collector has not reported."
    for item in host.incomplete:
        missing = set(item["missing"])
        if "cpu" in missing and cfg.external_reserve_cpu_cores <= 0:
            return f"Container '{item['name']}' has no CPU limit, so host CPU accounting is incomplete."
        if "memory" in missing and cfg.external_reserve_memory_bytes <= 0:
            return f"Container '{item['name']}' has no memory limit, so host memory accounting is incomplete."
    return None


def bounds(conn: sqlite3.Connection, cfg: Config, owner_id: str, *, current: Res = Res(),
           pool: str | None = None) -> dict:
    """Allowed ranges for a container owned by ``owner_id``.

    ``current`` is the container's existing committed allocation (zero for a new
    container) so that a resize may keep what it already has.
    """
    host = host_view(conn, cfg)
    q, c, p = owner_quota(conn, owner_id), owner_committed(conn, owner_id), owner_pending(conn, owner_id)
    owner_rem = q - c - p
    cpu_max = current.cpu_cores + min(owner_rem.cpu_cores, host.cpu_remaining)
    cpu_max = min(cpu_max, max(host.cpu_total - cfg.host_cpu_reserve, 0)) if host.known else current.cpu_cores
    mem_max = current.memory_bytes + min(owner_rem.memory_bytes, host.memory_remaining)
    mem_max = (mem_max // MIB) * MIB
    out = {
        "cpu_cores": {"min": 1, "max": max(cpu_max, 0)},
        "cpu_allowance_pct": {"min": MIN_ALLOWANCE_PCT, "max": 100},
        "memory_bytes": {"min": MIN_MEMORY_BYTES, "max": max(mem_max, 0), "step": MIB},
        "disk_bytes": {},
        "blocked_reason": accounting_blocker(cfg, host, pool),
    }
    for name in host.pools:
        if name not in cfg.allowed_pools:
            continue
        disk_max = current.disk_bytes + min(owner_rem.disk_bytes, host.pool_remaining(name))
        out["disk_bytes"][name] = {"min": max(MIN_DISK_BYTES, current.disk_bytes),
                                   "max": max((disk_max // MIB) * MIB, 0), "step": MIB}
    return out


def check_and_reserve(conn: sqlite3.Connection, cfg: Config, *, operation_id: str, owner_id: str,
                      pool: str | None, delta: Res) -> None:
    """Validate a positive delta against owner quota and host budget and insert
    a pending reservation. MUST run inside BEGIN IMMEDIATE."""
    need = delta.positive()
    host = host_view(conn, cfg)
    blocked = accounting_blocker(cfg, host, pool)
    if blocked and (need.cpu_cores or need.memory_bytes or need.disk_bytes):
        raise Conflict(blocked, code="ACCOUNTING_INCOMPLETE")
    q, c, p = owner_quota(conn, owner_id), owner_committed(conn, owner_id), owner_pending(conn, owner_id)
    rem = q - c - p
    fields = {}
    if need.cpu_cores > rem.cpu_cores:
        fields["cpu_cores"] = f"Owner has {max(rem.cpu_cores, 0)} CPU core(s) of quota remaining."
    if need.memory_bytes > rem.memory_bytes:
        fields["memory_bytes"] = f"Owner has {max(rem.memory_bytes, 0) // MIB} MiB of memory quota remaining."
    if need.disk_bytes > rem.disk_bytes:
        fields["disk_bytes"] = f"Owner has {max(rem.disk_bytes, 0) // MIB} MiB of disk quota remaining."
    if fields:
        raise Conflict("The request exceeds the owner's remaining quota.", code="QUOTA_EXCEEDED", fields=fields)
    if need.cpu_cores > host.cpu_remaining:
        fields["cpu_cores"] = f"Host has {max(host.cpu_remaining, 0)} unallocated CPU core(s) after reserves."
    if need.memory_bytes > host.memory_remaining:
        fields["memory_bytes"] = f"Host has {max(host.memory_remaining, 0) // MIB} MiB unallocated after reserves."
    if need.disk_bytes and (pool is None or need.disk_bytes > host.pool_remaining(pool)):
        fields["disk_bytes"] = f"Pool has {host.pool_remaining(pool) // MIB if pool else 0} MiB available after reserves."
    if fields:
        raise Conflict("The host does not have enough unallocated capacity.", code="HOST_CAPACITY", fields=fields)
    if need.cpu_cores or need.memory_bytes or need.disk_bytes:
        conn.execute(
            "INSERT INTO quota_reservations(operation_id, owner_id, pool, cpu_cores, memory_bytes, disk_bytes,"
            " state, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (operation_id, owner_id, pool, need.cpu_cores, need.memory_bytes, need.disk_bytes))


def release(conn: sqlite3.Connection, operation_id: str) -> None:
    conn.execute("UPDATE quota_reservations SET state = 'released',"
                 " released_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE operation_id = ? AND state = 'pending'",
                 (operation_id,))


def check_quota_change(conn: sqlite3.Connection, user_id: str, new_quota: Res) -> None:
    """Reject quota reductions below what is already allocated or reserved."""
    used = owner_committed(conn, user_id) + owner_pending(conn, user_id)
    fields = {}
    for key in ("cpu_cores", "memory_bytes", "disk_bytes"):
        if getattr(new_quota, key) < getattr(used, key):
            fields[key] = f"Already allocated or reserved: {getattr(used, key)}."
    if fields:
        raise Conflict("Quota cannot be reduced below current allocations.", code="QUOTA_BELOW_ALLOCATION",
                       fields=fields)
