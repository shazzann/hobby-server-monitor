"""Audit trail. Records actor/target snapshots; never secrets or command output."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from ..timeutil import iso, utcnow, utcnow_iso

SYSTEM = "system"


def record(conn: sqlite3.Connection, *, action: str, outcome: str, actor_id: str | None = None,
           actor_email: str | None = None, target_type: str | None = None, target_id: str | None = None,
           target_label: str | None = None, details: dict | None = None, request_id: str | None = None,
           operation_id: str | None = None) -> None:
    if actor_id and actor_email is None:
        row = conn.execute("SELECT email FROM users WHERE id = ?", (actor_id,)).fetchone()
        actor_email = row["email"] if row else None
    conn.execute(
        "INSERT INTO audit_events(at, actor_id, actor_email, action, target_type, target_id, target_label,"
        " outcome, details, request_id, operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (utcnow_iso(), actor_id, actor_email or SYSTEM, action, target_type, target_id, target_label,
         outcome, json.dumps(details or {}, sort_keys=True), request_id, operation_id),
    )


def page(conn: sqlite3.Connection, *, before_id: int | None, limit: int) -> list[dict]:
    limit = max(1, min(limit, 200))
    if before_id:
        rows = conn.execute("SELECT * FROM audit_events WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
    else:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d["details"] or "{}")
        out.append(d)
    return out


def purge(conn: sqlite3.Connection, retention_days: int) -> int:
    return conn.execute("DELETE FROM audit_events WHERE at < ?",
                        (iso(utcnow() - timedelta(days=retention_days)),)).rowcount
