"""Host and per-owner *allocation* accounting (consumption comes from TinyFlux)."""
from __future__ import annotations

import sqlite3

from ..config import Config
from . import quotas


def overview(conn: sqlite3.Connection, cfg: Config) -> dict:
    h = quotas.host_view(conn, cfg)
    host = {
        "known": h.known,
        "cpu": {"total": h.cpu_total, "reserve": cfg.host_cpu_reserve + cfg.external_reserve_cpu_cores,
                "budget": h.cpu_budget, "allocated": h.cpu_allocated, "pending": h.cpu_pending,
                "remaining": h.cpu_remaining},
        "memory": {"total": h.memory_total,
                   "reserve": cfg.host_memory_reserve_bytes + cfg.external_reserve_memory_bytes,
                   "budget": h.memory_budget, "allocated": h.memory_allocated, "pending": h.memory_pending,
                   "remaining": h.memory_remaining},
        "pools": [{"name": p["name"], "driver": p["driver"], "quota_capable": p["quota_capable"],
                   "total": p["total"], "used": p["used"], "reserve": p["reserve"], "budget": p["budget"],
                   "allocated": p["allocated"], "pending": p["pending"], "remaining": h.pool_remaining(name)}
                  for name, p in sorted(h.pools.items())],
    }
    owners = []
    for u in conn.execute("SELECT id, email, status FROM users WHERE status != 'revoked'"
                          " OR id IN (SELECT owner_id FROM containers WHERE status != 'deleted') ORDER BY email"):
        owners.append({"user_id": u["id"], "email": u["email"], "status": u["status"],
                       **quotas.owner_summary(conn, u["id"])})
    return {"host": host, "incomplete": h.incomplete,
            "blocked_reason": quotas.accounting_blocker(cfg, h, None), "owners": owners}
