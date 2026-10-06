"""Typed JSON values in service_state: heartbeats, LXD capability snapshots,
retention checkpoints and the bootstrap record."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import utcnow_iso

# Keys (the complete set; add here, not ad hoc)
COLLECTOR_HEARTBEAT = "collector.heartbeat"   # {last_cycle_at, last_success_at, cycle_ms, missed_cycles, lxd_available, error, containers}
WORKER_HEARTBEAT = "worker.heartbeat"         # {at, worker_id, in_flight}
LXD_CAPABILITIES = "lxd.capabilities"         # see collector.source.build_capabilities
ROLLUP_CHECKPOINT = "metrics.rollup_checkpoint"
METRICS_STORAGE = "metrics.storage"           # {bytes, raw_from, rollup_from (epoch s), removed_shards, quarantined, rollup_checkpoint, dropped_ingest}
BOOTSTRAP = "auth.bootstrap"                  # {secret_hash, expires_at, completed_at, user_id}


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM service_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def get_with_time(conn: sqlite3.Connection, key: str) -> tuple[Any, str | None]:
    row = conn.execute("SELECT value, updated_at FROM service_state WHERE key = ?", (key,)).fetchone()
    return (json.loads(row[0]), row[1]) if row else (None, None)


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO service_state(key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value, sort_keys=True), utcnow_iso()),
    )
