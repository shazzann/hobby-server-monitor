"""Control worker: executes queued operations against LXD and reconciles
uncertain outcomes.  Entry point: ``python -m hsm.worker``.

Lifecycle of one operation (see plan section 8):

    queued --claim (lease)--> running --LXD confirmed--> succeeded | failed
                                   \\--LXD outcome uncertain---> reconciling
    running with an expired lease (worker died) ------------> reconciling
    reconciling --observed LXD state--> succeeded | failed (or stays)

Rules that keep accounting correct
----------------------------------
* No SQLite transaction is ever held across an LXD call. Each finalisation is
  ONE short ``BEGIN IMMEDIATE`` that commits the container's new committed
  values, releases the quota reservation and records the outcome together, so
  a crash can never commit one without the other (no double charge, no leak).
* ``set_state(failed|cancelled)`` releases the reservation; ``reconciling``
  keeps it. "Timed out" is never treated as failure.
* Every finalisation first re-reads the operation and aborts (``LeaseLost``)
  unless it is still in the state this thread expects (and, for ``running``,
  still leased by this worker), so a slow thread and the reconciler can never
  both finalise the same operation.
* Right before dispatch the actor and target are re-checked through
  ``auth.policy`` (revocation, demotion and unassignment take effect even for
  already-queued work) and the live, *expanded* LXD config is re-evaluated by
  ``lxd_safety`` because an operator may have changed a profile.
* Exec is never retried or replayed: a command whose outcome is unknown, or
  that was running when the worker died, is finalised as failed /
  ``OUTCOME_UNKNOWN``. Its audit record never contains the command or output.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from . import config as config_mod
from . import db, lxd_safety
from .auth import policy, sessions
from .errors import Forbidden, HSMError, NotFound
from .integrations.lxd import limits_match
from .integrations.lxd_errors import AmbiguousIdentity, LxdError, LxdNotFound, LxdRejected, LxdUnavailable
from .services import audit, operations, quotas, state
from .timeutil import parse, utcnow, utcnow_iso

log = logging.getLogger("hsm.worker")

LEASE_SECONDS = 60          # renewed every RENEW_EVERY while the op is in flight
RENEW_EVERY = 15
IDLE_POLL = 0.75            # cheap indexed query on operations_queue
HEARTBEAT_EVERY = 5
RECONCILE_EVERY = 30
PURGE_EVERY = 60
MAX_THREADS = 4             # concurrent ops on *different* containers (the API excludes same-container conflicts)
EXEC_MAX_QUEUE_AGE = 300    # a command still queued after 5 min is dropped, not run late
SETTLE_SECONDS = {"create": 300}   # how long LXD may still be working before "not observed" means "not applied"
DEFAULT_SETTLE = 120

EXPECTED_STATUS = {"start": "Running", "stop": "Stopped", "restart": "Running",
                   "freeze": "Frozen", "unfreeze": "Running"}
LIMIT_KEYS = ("cpu_cores", "cpu_allowance_pct", "memory_bytes", "disk_bytes")


class OpFailed(Exception):
    """Confirmed not applied: finalise as failed (releases the reservation)."""

    def __init__(self, code: str, message: str, *, outcome: str = "failed", result: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.outcome, self.result = code, message, outcome, result


class Uncertain(Exception):
    """The outcome is unknown: go to / stay in reconciling, keep the reservation."""


class LeaseLost(Exception):
    """The operation is no longer ours to finalise."""


@dataclass
class Ctx:
    conn: sqlite3.Connection
    op: sqlite3.Row
    payload: dict
    expect: str                     # 'running' (dispatch) or 'reconciling'

    @property
    def kind(self) -> str:
        return self.op["kind"]

    @property
    def op_id(self) -> str:
        return self.op["id"]


class Worker:
    def __init__(self, cfg: config_mod.Config, adapter: Any, *, worker_id: str | None = None,
                 max_threads: int = MAX_THREADS, connect: Callable[[], sqlite3.Connection] | None = None):
        self.cfg = cfg
        self.adapter = adapter
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._connect = connect or (lambda: db.connect(cfg.sqlite_path))
        self.max_threads = max_threads
        self._pool = ThreadPoolExecutor(max_threads, thread_name_prefix="hsm-op") if max_threads > 0 else None
        self._in_flight: dict[str, str] = {}          # op_id -> 'run' | 'reconcile'
        self._lock = threading.Lock()
        self.settle_seconds = dict(SETTLE_SECONDS)

    # ================================================================ loop
    def run(self, stop: threading.Event) -> None:
        conn = self._connect()
        last = {"hb": -1e9, "renew": time.monotonic(), "reconcile": -1e9, "purge": time.monotonic()}
        log.info("worker %s started", self.worker_id)
        try:
            while not stop.is_set():
                claimed = False
                try:
                    now = time.monotonic()
                    if now - last["hb"] >= HEARTBEAT_EVERY:
                        self.heartbeat(conn)
                        last["hb"] = now
                    if now - last["renew"] >= RENEW_EVERY:
                        self.renew_leases(conn)
                        last["renew"] = now
                    if now - last["reconcile"] >= RECONCILE_EVERY:     # also runs at start-up
                        self.reconcile_pass(conn)
                        last["reconcile"] = now
                    if now - last["purge"] >= PURGE_EVERY:
                        self.purge(conn)
                        last["purge"] = now
                    claimed = self.claim_and_dispatch(conn)
                except sqlite3.Error:
                    log.exception("database error in worker loop")
                if not claimed:
                    stop.wait(IDLE_POLL)
        finally:
            log.info("worker stopping; waiting for in-flight operations")
            if self._pool:
                self._pool.shutdown(wait=True)
            conn.close()

    def heartbeat(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            n = len(self._in_flight)
        state.put(conn, state.WORKER_HEARTBEAT, {"at": utcnow_iso(), "worker_id": self.worker_id, "in_flight": n})

    def renew_leases(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            running = [op_id for op_id, mode in self._in_flight.items() if mode == "run"]
        for op_id in running:
            operations.renew_lease(conn, op_id, self.worker_id, LEASE_SECONDS)

    def purge(self, conn: sqlite3.Connection) -> None:
        with db.write_tx(conn):
            operations.purge(conn, self.cfg)
            sessions.purge_expired(conn)
            audit.purge(conn, self.cfg.audit_retention_days)

    def _busy(self) -> bool:
        with self._lock:
            return self.max_threads > 0 and len(self._in_flight) >= self.max_threads

    def _submit(self, op_id: str, mode: str, fn: Callable[[str], None]) -> bool:
        with self._lock:
            if op_id in self._in_flight:
                return False
            self._in_flight[op_id] = mode

        def task() -> None:
            try:
                fn(op_id)
            except Exception:
                log.exception("operation %s crashed in %s", op_id, mode)
            finally:
                with self._lock:
                    self._in_flight.pop(op_id, None)

        if self._pool is None:
            task()                  # inline mode (tests, --once)
        else:
            self._pool.submit(task)
        return True

    def claim_and_dispatch(self, conn: sqlite3.Connection) -> bool:
        if self._busy():
            return False
        op = operations.claim_next(conn, self.worker_id, LEASE_SECONDS)
        if op is None:
            return False
        return self._submit(op["id"], "run", self.process)

    def run_until_idle(self, max_ops: int = 100) -> int:
        """Inline helper: claim and process until the queue is empty."""
        conn = self._connect()
        try:
            n = 0
            while n < max_ops and self.claim_and_dispatch(conn):
                n += 1
            return n
        finally:
            conn.close()

    # ============================================================ reconcile
    def reconcile_pass(self, conn: sqlite3.Connection) -> None:
        """Expired leases -> reconciling (exec -> failed), then try every
        reconciling op. Ops this process is still working on are skipped."""
        for op in operations.expired_running(conn):
            with self._lock:
                if op["id"] in self._in_flight:
                    continue
            self._expire(conn, op["id"])
        for op in operations.reconciling(conn):
            if self._busy():
                break
            self._submit(op["id"], "reconcile", self.reconcile)

    def _expire(self, conn: sqlite3.Connection, op_id: str) -> None:
        with db.write_tx(conn):
            op = operations.get(conn, op_id)
            if op is None or op["state"] != "running" or (op["lease_expires_at"] or "") >= utcnow_iso():
                return
            if op["kind"] == "exec":
                operations.set_state(conn, op_id, "failed", error_code="OUTCOME_UNKNOWN",
                                     error_message="The worker stopped while this command was running. "
                                                   "It was not retried.")
                self._audit(conn, op, "failed", {"error_code": "OUTCOME_UNKNOWN", "outcome": "unknown"})
            else:
                operations.set_state(conn, op_id, "reconciling", error_code="LEASE_EXPIRED",
                                     error_message="The worker stopped mid-operation; checking LXD state.")
        log.warning("operation %s (%s) lease expired", op_id, op["kind"])

    def reconcile(self, op_id: str) -> None:
        conn = self._connect()
        try:
            op = operations.get(conn, op_id)
            if op is None or op["state"] != "reconciling":
                return
            ctx = Ctx(conn, op, self._payload(op), "reconciling")
            try:
                RECONCILERS[op["kind"]](self, ctx)
            except OpFailed as f:
                self._fail(ctx, f)
            except Uncertain as u:
                log.info("operation %s still reconciling: %s", op_id, u)
            except LeaseLost:
                log.info("operation %s changed while reconciling", op_id)
        finally:
            conn.close()

    # ============================================================ dispatch
    def process(self, op_id: str) -> None:
        conn = self._connect()
        try:
            op = operations.get(conn, op_id)
            if op is None or op["state"] != "running" or op["lease_owner"] != self.worker_id:
                return
            ctx = Ctx(conn, op, {}, "running")
            try:
                ctx.payload = self._payload(op)
                HANDLERS[op["kind"]](self, ctx)
            except OpFailed as f:
                self._fail(ctx, f)
            except Uncertain as u:
                self._to_reconciling(ctx, str(u))
            except LeaseLost:
                log.warning("operation %s was taken over before it could be finalised", op_id)
            except Exception as exc:     # a bug: never guess the outcome of a possibly-dispatched op
                log.exception("operation %s failed unexpectedly", op_id)
                if op["kind"] == "exec":
                    self._fail(ctx, OpFailed("OUTCOME_UNKNOWN", "Internal error; the command was not retried."))
                else:
                    self._to_reconciling(ctx, f"internal error: {type(exc).__name__}")
        finally:
            conn.close()

    @staticmethod
    def _payload(op: sqlite3.Row) -> dict:
        try:
            payload = json.loads(op["payload"] or "{}")
        except ValueError:
            raise OpFailed("INVALID_PAYLOAD", "Operation payload is not valid JSON.") from None
        if not isinstance(payload, dict):
            raise OpFailed("INVALID_PAYLOAD", "Operation payload is not an object.")
        return payload

    # ============================================================ finalisation
    @contextmanager
    def _finalize(self, ctx: Ctx) -> Iterator[sqlite3.Connection]:
        with db.write_tx(ctx.conn) as conn:
            row = conn.execute("SELECT state, lease_owner FROM operations WHERE id = ?", (ctx.op_id,)).fetchone()
            if row is None or row["state"] != ctx.expect or (
                    ctx.expect == "running" and row["lease_owner"] != self.worker_id):
                raise LeaseLost(ctx.op_id)
            yield conn

    def _audit(self, conn: sqlite3.Connection, op: sqlite3.Row, outcome: str, details: dict | None = None) -> None:
        label = None
        if op["container_id"]:
            row = conn.execute("SELECT name FROM containers WHERE id = ?", (op["container_id"],)).fetchone()
            label = row["name"] if row else None
        audit.record(conn, action=f"container.{op['kind']}", outcome=outcome, actor_id=op["actor_id"],
                     target_type="container", target_id=op["container_id"], target_label=label,
                     details=details, operation_id=op["id"])

    @staticmethod
    def _tombstone(conn: sqlite3.Connection, container_id: str) -> None:
        """Confirmed deletion: keep the row for audit/history, drop grants and
        latest metrics. Committed allocations stop counting (status='deleted')."""
        conn.execute("UPDATE containers SET status = 'deleted', deleted_at = ? WHERE id = ? AND status != 'deleted'",
                     (utcnow_iso(), container_id))
        conn.execute("DELETE FROM container_access WHERE container_id = ?", (container_id,))
        conn.execute("DELETE FROM latest_metrics WHERE container_id = ?", (container_id,))

    def _fail(self, ctx: Ctx, f: OpFailed) -> None:
        try:
            with self._finalize(ctx) as conn:
                if ctx.kind == "create" and ctx.op["container_id"]:
                    row = conn.execute("SELECT status FROM containers WHERE id = ?",
                                       (ctx.op["container_id"],)).fetchone()
                    if row and row["status"] == "creating":
                        self._tombstone(conn, ctx.op["container_id"])
                ttl = self.cfg.exec_result_ttl_minutes if f.result is not None else None
                operations.set_state(conn, ctx.op_id, "failed", error_code=f.code, error_message=f.message,
                                     result=f.result, result_ttl_minutes=ttl)   # also releases the reservation
                details: dict = {"error_code": f.code}
                if ctx.kind == "exec" and f.result:
                    details.update({k: f.result.get(k) for k in ("exit_code", "outcome", "duration_ms", "user")})
                self._audit(conn, ctx.op, f.outcome, details)
        except LeaseLost:
            log.warning("operation %s: could not record failure %s (no longer ours)", ctx.op_id, f.code)
            return
        log.info("operation %s (%s) failed: %s %s", ctx.op_id, ctx.kind, f.code, f.message)

    def _to_reconciling(self, ctx: Ctx, message: str) -> None:
        try:
            with self._finalize(ctx) as conn:
                operations.set_state(conn, ctx.op_id, "reconciling", error_code="LXD_UNCERTAIN",
                                     error_message=message)
        except LeaseLost:
            return
        log.warning("operation %s (%s) outcome uncertain: %s", ctx.op_id, ctx.kind, message)

    def _succeed(self, ctx: Ctx, details: dict | None = None, *, release: bool = False,
                 extra: Callable[[sqlite3.Connection], None] | None = None, result: dict | None = None) -> None:
        with self._finalize(ctx) as conn:
            if extra:
                extra(conn)
            if release:
                quotas.release(conn, ctx.op_id)
            ttl = self.cfg.exec_result_ttl_minutes if result is not None else None
            operations.set_state(conn, ctx.op_id, "succeeded", result=result, result_ttl_minutes=ttl)
            self._audit(conn, ctx.op, "succeeded", details)
        log.info("operation %s (%s) succeeded", ctx.op_id, ctx.kind)

    # ============================================================ shared checks
    def _authorize(self, ctx: Ctx, need: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        """Re-check the actor and the target *now*, not at submission time."""
        try:
            actor = policy.active_user(ctx.conn, ctx.op["actor_id"])
            container = policy.container_for(ctx.conn, ctx.op["actor_id"], ctx.op["container_id"] or "", need)
        except HSMError as e:            # Forbidden / NotFound / Conflict from the policy layer
            code = ("ACTOR_NOT_AUTHORIZED" if isinstance(e, Forbidden) else
                    "CONTAINER_NOT_FOUND" if isinstance(e, NotFound) else e.code)
            raise OpFailed(code, e.message, outcome="denied") from None
        return actor, container

    @staticmethod
    def _require_status(container: sqlite3.Row, *allowed: str) -> None:
        if container["status"] not in allowed:
            raise OpFailed("INVALID_STATE", f"The container is {container['status']}.")

    def _owner_usable(self, ctx: Ctx, owner_id: str | None) -> None:
        row = ctx.conn.execute("SELECT status FROM users WHERE id = ?", (owner_id,)).fetchone() if owner_id else None
        if row is None or row["status"] == "revoked":
            raise OpFailed("OWNER_INACTIVE", "The selected owner is not an active user.")

    def _read(self, fn: Callable, *args, **kwargs):
        """A pre-dispatch LXD read: nothing has been changed yet, so any failure
        is a clean, retryable failure."""
        try:
            return fn(*args, **kwargs)
        except AmbiguousIdentity as e:
            raise OpFailed("IDENTITY_AMBIGUOUS", e.message) from None
        except LxdRejected as e:
            raise OpFailed(e.code, e.message) from None
        except LxdError as e:
            raise OpFailed("LXD_UNAVAILABLE", f"LXD could not be read; nothing was changed. {e.message}") from None

    @staticmethod
    def _observe(fn: Callable, *args, **kwargs):
        """A read after (possible) dispatch: failure means 'unknown'."""
        try:
            return fn(*args, **kwargs)
        except LxdError as e:
            raise Uncertain(e.message) from None

    @staticmethod
    def _dispatch(fn: Callable, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except LxdRejected as e:
            raise OpFailed(e.code if e.code not in ("LXD_ERROR",) else "LXD_REJECTED", e.message) from None
        except LxdUnavailable as e:         # never sent: nothing changed
            raise OpFailed("LXD_UNAVAILABLE", e.message) from None
        except LxdError as e:
            raise Uncertain(e.message) from None

    def _evaluate(self, instance: dict) -> list[str]:
        return lxd_safety.evaluate(instance, allowed_pools=self.cfg.allowed_pools,
                                   allowed_networks=self.cfg.allowed_networks)

    def _live_instance(self, container: sqlite3.Row, *, check_safety: bool = True) -> dict:
        """Resolve by identity marker (names are labels), verify the identity
        has not been swapped, and re-run the safety policy on the live config."""
        if not container["managed"] or not container["lxd_marker"]:
            raise OpFailed("NOT_MANAGED", "This container is not managed by the app.")
        inst = self._read(self.adapter.find_by_marker, container["lxd_marker"])
        if inst is None:
            raise OpFailed("INSTANCE_MISSING", "LXD has no instance with this container's identity.")
        if container["lxd_volatile_uuid"] and inst.get("volatile_uuid") and \
                inst["volatile_uuid"] != container["lxd_volatile_uuid"]:
            raise OpFailed("IDENTITY_MISMATCH", "The LXD instance carrying this identity has changed (copy?).")
        if check_safety:
            reasons = self._evaluate(inst)
            if reasons:
                raise OpFailed("CONTAINER_UNSAFE", "Live LXD config failed the safety check: " + "; ".join(reasons))
        return inst

    def _age_seconds(self, op: sqlite3.Row) -> float:
        started = parse(op["started_at"] or op["created_at"])
        return (utcnow() - started).total_seconds() if started else 0.0

    def _settled(self, ctx: Ctx) -> bool:
        return self._age_seconds(ctx.op) >= self.settle_seconds.get(ctx.kind, DEFAULT_SETTLE)

    def _container(self, ctx: Ctx) -> sqlite3.Row:
        row = ctx.conn.execute("SELECT * FROM containers WHERE id = ?", (ctx.op["container_id"],)).fetchone()
        if row is None:
            raise OpFailed("CONTAINER_NOT_FOUND", "Container record not found.")
        return row

    @staticmethod
    def _limits(raw: Any) -> dict:
        if not isinstance(raw, dict):
            raise OpFailed("INVALID_PAYLOAD", "Limits are missing.")
        out = {}
        for key in LIMIT_KEYS:
            v = raw.get(key)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise OpFailed("INVALID_PAYLOAD", f"{key} must be a positive integer.")
            out[key] = v
        return out

    # ============================================================ create
    def _h_create(self, ctx: Ctx) -> None:
        _, c = self._authorize(ctx, "manage")
        self._require_status(c, "creating")
        p = ctx.payload
        self._owner_usable(ctx, c["owner_id"] or p.get("owner_id"))
        limits = self._limits(p)
        marker = c["lxd_marker"] or c["id"]
        # Idempotence: a previous attempt may already have created it.
        if self._read(self.adapter.find_by_marker, marker) is None:
            if self._read(self.adapter.get_instance, c["project"], p.get("name") or c["name"]) is not None:
                raise OpFailed("NAME_TAKEN", "An LXD instance with this name already exists.")
            free = self._read(self.adapter.pool_free_bytes, p.get("pool"))
            if free <= self.cfg.pool_reserve_bytes:
                raise OpFailed("POOL_FULL", "The storage pool is below its free-space reserve.")
            try:
                self.adapter.create(c["project"], p.get("name") or c["name"], image_fingerprint=p.get("image_fingerprint"),
                                    pool=p.get("pool"), network=p.get("network"), ephemeral=bool(p.get("ephemeral")),
                                    autostart=bool(p.get("autostart")), description=p.get("description") or "",
                                    marker=marker, **limits)
            except LxdUnavailable as e:
                raise OpFailed("LXD_UNAVAILABLE", e.message) from None
            except LxdRejected as e:
                # Trust but verify: only a confirmed absence lets us release.
                if self._observe(self.adapter.find_by_marker, marker) is None:
                    raise OpFailed(e.code, e.message) from None
            except LxdError as e:
                raise Uncertain(e.message) from None
        self._complete_create(ctx, c, marker, limits)

    def _complete_create(self, ctx: Ctx, c: sqlite3.Row, marker: str, limits: dict) -> None:
        """Idempotent tail of create: start, provision, read back, commit."""
        warnings: list[str] = []
        inst = self._observe(self.adapter.find_by_marker, marker)
        if inst is None:
            raise Uncertain("the created instance is not visible yet")
        if inst["status"] == "Stopped":
            try:
                self.adapter.start(inst["project"], inst["name"])
            except LxdRejected as e:
                warnings.append(f"start failed: {e.message}")
            except LxdError as e:
                raise Uncertain(e.message) from None
            inst = self._observe(self.adapter.find_by_marker, marker) or inst
        if inst["status"] == "Running":
            try:
                self.adapter.provision_guest_user(inst["project"], inst["name"])
            except LxdRejected as e:
                warnings.append(f"guest account not provisioned: {e.message}")
            except LxdError as e:
                raise Uncertain(e.message) from None
        else:
            warnings.append("guest account not provisioned: container is not running")
        reasons = self._evaluate(inst)
        pool = ((inst.get("expanded_devices") or {}).get("root") or {}).get("pool") or ctx.payload.get("pool")

        def commit(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE containers SET status = 'active', managed = 1, lxd_marker = COALESCE(lxd_marker, ?),"
                " lxd_volatile_uuid = ?, cpu_cores = ?, cpu_allowance_pct = ?, memory_bytes = ?, disk_bytes = ?,"
                " pool = ?, safety = ?, safety_reasons = ?, version = version + 1, last_seen_at = ?"
                " WHERE id = ? AND status = 'creating'",
                (marker, inst.get("volatile_uuid"), limits["cpu_cores"], limits["cpu_allowance_pct"],
                 limits["memory_bytes"], limits["disk_bytes"], pool, "unsafe" if reasons else "safe",
                 json.dumps(reasons), utcnow_iso(), c["id"]))

        self._succeed(ctx, {"limits": limits, "pool": pool, "warnings": warnings, "safety_reasons": reasons},
                      release=True, extra=commit)

    def _r_create(self, ctx: Ctx) -> None:
        c = self._container(ctx)
        marker = c["lxd_marker"] or c["id"]
        limits = self._limits(ctx.payload)
        try:
            inst = self.adapter.find_by_marker(marker)
        except LxdError as e:          # includes AmbiguousIdentity: needs a human, keep the reservation
            raise Uncertain(e.message) from None
        if inst is not None:
            self._complete_create(ctx, c, marker, limits)
        elif not self._settled(ctx):
            raise Uncertain("instance not visible yet; LXD may still be creating it")
        else:
            raise OpFailed("LXD_NOT_CREATED", "LXD has no instance with this identity; it was not created.")

    # ============================================================ lifecycle
    def _h_lifecycle(self, ctx: Ctx) -> None:
        _, c = self._authorize(ctx, "manage")
        self._require_status(c, "active")
        # Stopping reduces exposure, so an unsafe container may still be stopped.
        inst = self._live_instance(c, check_safety=ctx.kind != "stop")
        if ctx.kind != "restart" and inst["status"] == EXPECTED_STATUS[ctx.kind]:
            return self._lifecycle_done(ctx, c, note=f"already {inst['status']}")
        kwargs = {"force": bool(ctx.payload.get("force"))} if ctx.kind == "stop" else {}
        self._dispatch(getattr(self.adapter, ctx.kind), inst["project"], inst["name"], **kwargs)
        self._lifecycle_done(ctx, c)

    def _lifecycle_done(self, ctx: Ctx, c: sqlite3.Row, *, note: str | None = None,
                        gone: bool | None = None) -> None:
        if gone is None and ctx.kind == "stop":
            # Stopping an ephemeral container deletes it. Only a complete
            # listing without our marker counts as proof.
            try:
                gone = self.adapter.find_by_marker(c["lxd_marker"]) is None
            except LxdError:
                gone = False
        details = {"note": note} if note else {}
        if gone:
            details["deleted_by_stop"] = True
        self._succeed(ctx, details or None,
                      extra=(lambda conn: self._tombstone(conn, c["id"])) if gone else None)

    def _r_lifecycle(self, ctx: Ctx) -> None:
        c = self._container(ctx)
        inst = self._observe(self.adapter.find_by_marker, c["lxd_marker"])
        if inst is None:
            if ctx.kind == "stop":
                return self._lifecycle_done(ctx, c, gone=True)
            raise OpFailed("INSTANCE_MISSING", "LXD has no instance with this identity.")
        if inst["status"] == EXPECTED_STATUS[ctx.kind]:
            return self._lifecycle_done(ctx, c, gone=False, note="confirmed by observed state")
        if not self._settled(ctx):
            raise Uncertain(f"observed {inst['status']}, waiting")
        raise OpFailed("OUTCOME_NOT_OBSERVED", f"LXD reports the container as {inst['status']}.")

    # ============================================================ limits
    def _h_update_limits(self, ctx: Ctx) -> None:
        _, c = self._authorize(ctx, "manage")
        self._require_status(c, "active")
        after = self._limits(ctx.payload.get("after"))
        if c["disk_bytes"] and after["disk_bytes"] < c["disk_bytes"]:
            raise OpFailed("DISK_SHRINK", "Disk can only be expanded, not shrunk.")
        inst = self._live_instance(c)
        self._dispatch(self.adapter.update_limits, inst["project"], inst["name"], **after)
        self._limits_done(ctx, c, after)

    def _limits_done(self, ctx: Ctx, c: sqlite3.Row, after: dict) -> None:
        def commit(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE containers SET cpu_cores = ?, cpu_allowance_pct = ?, memory_bytes = ?,"
                         " disk_bytes = ?, version = version + 1 WHERE id = ?",
                         (after["cpu_cores"], after["cpu_allowance_pct"], after["memory_bytes"],
                          after["disk_bytes"], c["id"]))
        self._succeed(ctx, {"before": ctx.payload.get("before"), "after": after}, release=True, extra=commit)

    def _r_update_limits(self, ctx: Ctx) -> None:
        c = self._container(ctx)
        after = self._limits(ctx.payload.get("after"))
        inst = self._observe(self.adapter.find_by_marker, c["lxd_marker"])
        if inst is None:
            raise OpFailed("INSTANCE_MISSING", "LXD has no instance with this identity.")
        if limits_match(inst, after):
            return self._limits_done(ctx, c, after)
        if limits_match(inst, ctx.payload.get("before") or {}):
            raise OpFailed("LXD_NOT_APPLIED", "LXD still has the previous limits; the change was not applied.")
        # Matches neither: an external change. Keep the reservation and block
        # further mutations until an admin resolves it.
        raise Uncertain("live limits match neither the requested nor the previous values")

    # ============================================================ delete
    def _h_delete(self, ctx: Ctx) -> None:
        _, c = self._authorize(ctx, "manage")
        self._require_status(c, "active", "failed", "quarantined")
        if not c["lxd_marker"]:
            raise OpFailed("NOT_MANAGED", "This container is not managed by the app.")
        inst = self._read(self.adapter.find_by_marker, c["lxd_marker"])
        if inst is None:
            return self._delete_done(ctx, c, note="already absent from LXD")
        if c["lxd_volatile_uuid"] and inst.get("volatile_uuid") and inst["volatile_uuid"] != c["lxd_volatile_uuid"]:
            raise OpFailed("IDENTITY_MISMATCH", "The LXD instance carrying this identity has changed (copy?).")
        # No safety gate: deleting reduces exposure and never touches host paths.
        if inst["status"] == "Frozen":
            self._dispatch(self.adapter.unfreeze, inst["project"], inst["name"])
        if inst["status"] in ("Running", "Frozen"):
            self._dispatch(self.adapter.stop, inst["project"], inst["name"], force=True)
        try:
            self.adapter.delete(inst["project"], inst["name"])
        except LxdNotFound:
            pass                                    # ephemeral: the stop already removed it
        except (LxdRejected, LxdUnavailable) as e:
            raise OpFailed(e.code if isinstance(e, LxdRejected) else "LXD_UNAVAILABLE", e.message) from None
        except LxdError as e:
            raise Uncertain(e.message) from None
        self._delete_done(ctx, c)

    def _delete_done(self, ctx: Ctx, c: sqlite3.Row, *, note: str | None = None) -> None:
        self._succeed(ctx, {"note": note} if note else None, extra=lambda conn: self._tombstone(conn, c["id"]))

    def _r_delete(self, ctx: Ctx) -> None:
        c = self._container(ctx)
        inst = self._observe(self.adapter.find_by_marker, c["lxd_marker"])
        if inst is None:
            return self._delete_done(ctx, c, note="confirmed absent")
        if not self._settled(ctx):
            raise Uncertain("instance still present, waiting")
        raise OpFailed("DELETE_NOT_APPLIED", "The instance still exists in LXD; it was not deleted.")

    # ============================================================ exec
    def _h_exec(self, ctx: Ctx) -> None:
        actor, c = self._authorize(ctx, "exec")
        as_root = bool(ctx.payload.get("as_root"))
        if as_root and actor["role"] != "admin":
            # The API set as_root for an admin who has since been demoted.
            raise OpFailed("ACTOR_NOT_AUTHORIZED", "Only administrators may run commands as root.", outcome="denied")
        created = parse(ctx.op["created_at"])
        if created and (utcnow() - created).total_seconds() > EXEC_MAX_QUEUE_AGE:
            raise OpFailed("EXEC_EXPIRED", "The command waited too long in the queue and was not run.")
        command = ctx.payload.get("command")
        if not isinstance(command, str) or not command.strip() or "\x00" in command or \
                len(command.encode("utf-8")) > self.cfg.exec_max_command_bytes:
            raise OpFailed("INVALID_COMMAND", "The command is empty, too long or contains NUL.")
        inst = self._live_instance(c)
        if inst["status"] != "Running":
            raise OpFailed("NOT_RUNNING", f"The container is {inst['status']}; start it first.")
        try:
            result = self.adapter.exec_command(inst["project"], inst["name"], command, as_root=as_root)
        except LxdRejected as e:
            raise OpFailed("EXEC_FAILED", f"LXD did not start the command: {e.message}") from None
        except LxdUnavailable as e:
            raise OpFailed("LXD_UNAVAILABLE", e.message) from None
        except LxdError as e:   # sent but unobserved: NEVER retried
            raise OpFailed("OUTCOME_UNKNOWN", f"The command's outcome is unknown: {e.message}") from None
        if result.get("outcome") == "unknown":
            raise OpFailed("OUTCOME_UNKNOWN", "The command did not finish within the deadline and could not be "
                                              "confirmed stopped. It was not retried.", result=result)
        self._succeed(ctx, {k: result.get(k) for k in ("exit_code", "outcome", "duration_ms", "user")},
                      result=result)

    def _r_exec(self, ctx: Ctx) -> None:
        raise OpFailed("OUTCOME_UNKNOWN", "The command's outcome is unknown. It was not retried.")

    # ============================================================ adopt
    def _h_adopt(self, ctx: Ctx) -> None:
        _, c = self._authorize(ctx, "manage")
        self._require_status(c, "active")
        if c["managed"] or c["lxd_marker"]:
            raise OpFailed("ALREADY_MANAGED", "This container is already managed.")
        self._owner_usable(ctx, ctx.payload.get("owner_id"))
        limits = self._limits(ctx.payload)
        inst = self._read(self.adapter.get_instance, c["project"], c["name"])
        if inst is None:
            raise OpFailed("INSTANCE_MISSING", "LXD has no instance with this name.")
        if inst.get("marker") or self._read(self.adapter.find_by_marker, c["id"]) is not None:
            raise OpFailed("IDENTITY_CONFLICT", "The instance already carries an identity marker.")
        reasons = self._evaluate(inst)
        if reasons:
            raise OpFailed("CONTAINER_UNSAFE", "Needs manual remediation before adoption: " + "; ".join(reasons))
        self._dispatch(self.adapter.update_limits, inst["project"], inst["name"], marker=c["id"], **limits)
        self._adopt_done(ctx, c, limits)

    def _adopt_done(self, ctx: Ctx, c: sqlite3.Row, limits: dict) -> None:
        inst = self._observe(self.adapter.find_by_marker, c["id"])
        if inst is None:
            raise Uncertain("adopted instance not visible by marker yet")
        warnings = []
        if inst["status"] == "Running":
            try:
                self.adapter.provision_guest_user(inst["project"], inst["name"])
            except LxdRejected as e:
                warnings.append(f"guest account not provisioned: {e.message}")
            except LxdError as e:
                raise Uncertain(e.message) from None
        else:
            warnings.append("guest account not provisioned: container is not running")
        pool = ((inst.get("expanded_devices") or {}).get("root") or {}).get("pool")

        def commit(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE containers SET lxd_marker = ?, managed = 1, owner_id = ?, lxd_volatile_uuid = ?,"
                         " cpu_cores = ?, cpu_allowance_pct = ?, memory_bytes = ?, disk_bytes = ?, pool = ?,"
                         " version = version + 1 WHERE id = ?",
                         (c["id"], ctx.payload.get("owner_id"), inst.get("volatile_uuid"), limits["cpu_cores"],
                          limits["cpu_allowance_pct"], limits["memory_bytes"], limits["disk_bytes"], pool, c["id"]))
        self._succeed(ctx, {"after": limits, "owner_id": ctx.payload.get("owner_id"), "warnings": warnings},
                      release=True, extra=commit)

    def _r_adopt(self, ctx: Ctx) -> None:
        c = self._container(ctx)
        limits = self._limits(ctx.payload)
        inst = self._observe(self.adapter.find_by_marker, c["id"])
        if inst is not None and limits_match(inst, limits):
            return self._adopt_done(ctx, c, limits)
        if inst is None and self._settled(ctx):
            raise OpFailed("LXD_NOT_APPLIED", "The identity marker was not written; adoption did not happen.")
        raise Uncertain("adoption not confirmed yet")


_LIFECYCLE = ("start", "stop", "restart", "freeze", "unfreeze")
HANDLERS: dict[str, Callable[[Worker, Ctx], None]] = {
    "create": Worker._h_create, "update_limits": Worker._h_update_limits, "delete": Worker._h_delete,
    "exec": Worker._h_exec, "adopt": Worker._h_adopt, **{k: Worker._h_lifecycle for k in _LIFECYCLE}}
RECONCILERS: dict[str, Callable[[Worker, Ctx], None]] = {
    "create": Worker._r_create, "update_limits": Worker._r_update_limits, "delete": Worker._r_delete,
    "exec": Worker._r_exec, "adopt": Worker._r_adopt, **{k: Worker._r_lifecycle for k in _LIFECYCLE}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hsm.worker", description="HSM control worker")
    parser.add_argument("--once", action="store_true", help="reconcile, drain the queue inline, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("HSM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config_mod.load()
    conn = db.connect(cfg.sqlite_path)
    try:
        if db.schema_version(conn) == 0:
            log.error("database %s is not migrated; run the migration entry point first", cfg.sqlite_path)
            return 2
    finally:
        conn.close()
    from .integrations.lxd import LxdAdapter
    adapter = LxdAdapter(cfg)
    if args.once:
        worker = Worker(cfg, adapter, max_threads=0)
        conn = worker._connect()
        try:
            worker.reconcile_pass(conn)
        finally:
            conn.close()
        worker.run_until_idle()
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    Worker(cfg, adapter).run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
