"""Durable operation queue in SQLite.

API side:   ``submit`` validates idempotency, excludes conflicting work on the
            same container, reserves quota and records audit intent -- all in
            one short BEGIN IMMEDIATE transaction -- then returns 202.
Worker side: ``claim_next`` takes a lease; ``finish`` records the outcome. The
            worker's per-kind finalizers commit accounting in the same
            transaction that releases the reservation.

States: queued -> running -> succeeded | failed | reconciling -> (succeeded | failed)
        queued -> cancelled
"Timed out" is never treated as proof of failure: an uncertain outcome goes to
``reconciling`` and keeps its reservation until LXD state proves the result.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from typing import Callable

from ..config import Config
from ..db import write_tx
from ..errors import BadRequest, Conflict
from ..security import new_id
from ..timeutil import iso, utcnow, utcnow_iso
from . import audit, quotas

ACTIVE_STATES = ("queued", "running", "reconciling")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def request_hash(kind: str, container_id: str | None, payload: dict) -> str:
    body = json.dumps({"kind": kind, "container_id": container_id, "payload": payload}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_idempotency_key(key: str | None) -> str:
    if not key or not IDEMPOTENCY_RE.match(key):
        raise BadRequest("An Idempotency-Key header (8-128 chars of A-Z a-z 0-9 _ -) is required.",
                         code="IDEMPOTENCY_KEY_REQUIRED")
    return key


def submit(conn: sqlite3.Connection, cfg: Config, *, actor_id: str, kind: str, container_id: str | None,
           payload: dict, idempotency_key: str, request_id: str | None = None,
           target_label: str | None = None, audit_details: dict | None = None,
           before_insert: Callable[[sqlite3.Connection], None] | None = None,
           reserve: tuple[str, str | None, quotas.Res] | None = None) -> tuple[sqlite3.Row, bool]:
    """Create an operation. Returns (row, created). A replay of the same key and
    request returns the original operation with created=False."""
    validate_idempotency_key(idempotency_key)
    rhash = request_hash(kind, container_id, payload)
    with write_tx(conn):
        existing = conn.execute("SELECT * FROM operations WHERE actor_id = ? AND idempotency_key = ?",
                                (actor_id, idempotency_key)).fetchone()
        if existing is not None:
            if existing["request_hash"] != rhash:
                raise Conflict("This Idempotency-Key was already used for a different request.",
                               code="IDEMPOTENCY_KEY_REUSED")
            return existing, False
        if container_id and kind != "create":
            busy = conn.execute(
                "SELECT id, kind FROM operations WHERE container_id = ? AND state IN ('queued','running','reconciling')",
                (container_id,)).fetchone()
            if busy is not None:
                raise Conflict(f"Another operation ({busy['kind']}) is in progress for this container.",
                               code="OPERATION_IN_PROGRESS", extra={"operation_id": busy["id"]})
        if before_insert:
            before_insert(conn)
        op_id = new_id()
        conn.execute(
            "INSERT INTO operations(id, actor_id, container_id, kind, payload, request_hash, idempotency_key,"
            " state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (op_id, actor_id, container_id, kind, json.dumps(payload, sort_keys=True), rhash,
             idempotency_key, utcnow_iso()))
        if reserve is not None:
            owner_id, pool, delta = reserve
            quotas.check_and_reserve(conn, cfg, operation_id=op_id, owner_id=owner_id, pool=pool, delta=delta)
        if kind != "exec":   # exec submissions are high-volume and carry user text; outcome is audited instead
            audit.record(conn, action=f"container.{kind}", outcome="requested", actor_id=actor_id,
                         target_type="container", target_id=container_id, target_label=target_label,
                         details=audit_details, request_id=request_id, operation_id=op_id)
        row = conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()
    return row, True


def get(conn: sqlite3.Connection, op_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()


def active_for_container(conn: sqlite3.Connection, container_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, kind, state FROM operations WHERE container_id = ? AND state IN ('queued','running','reconciling')"
        " ORDER BY created_at LIMIT 1", (container_id,)).fetchone()


# ---------------------------------------------------------------- worker side

def claim_next(conn: sqlite3.Connection, worker_id: str, lease_seconds: int) -> sqlite3.Row | None:
    with write_tx(conn):
        row = conn.execute("SELECT id FROM operations WHERE state = 'queued' ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            return None
        now = utcnow()
        conn.execute(
            "UPDATE operations SET state = 'running', lease_owner = ?, lease_expires_at = ?, attempts = attempts + 1,"
            " started_at = COALESCE(started_at, ?) WHERE id = ? AND state = 'queued'",
            (worker_id, iso(now + timedelta(seconds=lease_seconds)), iso(now), row["id"]))
        return conn.execute("SELECT * FROM operations WHERE id = ?", (row["id"],)).fetchone()


def renew_lease(conn: sqlite3.Connection, op_id: str, worker_id: str, lease_seconds: int) -> None:
    conn.execute("UPDATE operations SET lease_expires_at = ? WHERE id = ? AND lease_owner = ? AND state = 'running'",
                 (iso(utcnow() + timedelta(seconds=lease_seconds)), op_id, worker_id))


def expired_running(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Operations whose worker died mid-flight: candidates for reconciliation."""
    return conn.execute("SELECT * FROM operations WHERE state = 'running' AND lease_expires_at < ?",
                        (utcnow_iso(),)).fetchall()


def reconciling(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM operations WHERE state = 'reconciling' ORDER BY created_at").fetchall()


def set_state(conn: sqlite3.Connection, op_id: str, state: str, *, error_code: str | None = None,
              error_message: str | None = None, result: dict | None = None,
              result_ttl_minutes: int | None = None) -> None:
    """Update an operation (caller owns the transaction when combining with accounting)."""
    finished = utcnow_iso() if state in TERMINAL_STATES else None
    expires = iso(utcnow() + timedelta(minutes=result_ttl_minutes)) if result is not None and result_ttl_minutes else None
    conn.execute(
        "UPDATE operations SET state = ?, error_code = ?, error_message = ?,"
        " result = COALESCE(?, result), result_expires_at = COALESCE(?, result_expires_at),"
        " finished_at = COALESCE(?, finished_at), lease_owner = CASE WHEN ? IS NULL THEN lease_owner ELSE NULL END"
        " WHERE id = ?",
        (state, error_code, (error_message or "")[:500] or None,
         json.dumps(result) if result is not None else None, expires, finished, finished, op_id))
    if state in ("failed", "cancelled"):
        quotas.release(conn, op_id)


def cancel_queued(conn: sqlite3.Connection, *, actor_id: str | None = None, container_id: str | None = None,
                  reason: str) -> int:
    """Cancel not-yet-dispatched work (e.g. on revocation). Caller owns the transaction.
    Work already dispatched to LXD may still complete; that boundary is documented."""
    clauses, args = ["state = 'queued'"], []
    if actor_id:
        clauses.append("actor_id = ?")
        args.append(actor_id)
    if container_id:
        clauses.append("container_id = ?")
        args.append(container_id)
    ids = [r[0] for r in conn.execute(f"SELECT id FROM operations WHERE {' AND '.join(clauses)}", args)]
    for op_id in ids:
        set_state(conn, op_id, "cancelled", error_code="CANCELLED", error_message=reason)
    return len(ids)


def purge(conn: sqlite3.Connection, cfg: Config) -> None:
    """Expire exec output quickly; delete old finished operations."""
    now = utcnow_iso()
    conn.execute("UPDATE operations SET result = NULL, payload = '{}' WHERE kind = 'exec' AND result_expires_at < ?",
                 (now,))
    cutoff = iso(utcnow() - timedelta(days=cfg.operation_retention_days))
    old = "SELECT id FROM operations WHERE state IN ('succeeded','failed','cancelled') AND finished_at < ?"
    conn.execute(f"DELETE FROM quota_reservations WHERE state = 'released' AND operation_id IN ({old})", (cutoff,))
    conn.execute(f"DELETE FROM operations WHERE id IN ({old})"
                 " AND id NOT IN (SELECT operation_id FROM quota_reservations)", (cutoff,))


def serialize(op: sqlite3.Row, *, include_result: bool) -> dict:
    out = {
        "id": op["id"], "kind": op["kind"], "state": op["state"], "container_id": op["container_id"],
        "created_at": op["created_at"], "started_at": op["started_at"], "finished_at": op["finished_at"],
        "error": {"code": op["error_code"], "message": op["error_message"]} if op["error_code"] else None,
        "result": None,
    }
    if include_result and op["result"]:
        out["result"] = json.loads(op["result"])
    return out
