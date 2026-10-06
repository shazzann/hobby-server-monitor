"""Users, invitations, roles, quotas, revocation, assignments and ownership.

Every mutation is one short BEGIN IMMEDIATE transaction that also writes its
audit record, so invariants such as "at least one active admin" cannot be
broken by two concurrent requests.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from ..auth import sessions
from ..config import Config
from ..db import write_tx
from ..errors import Conflict, Invalid, NotFound
from ..security import canonical_email, hash_secret, is_uuid, new_id, new_secret
from ..timeutil import iso, utcnow, utcnow_iso
from . import audit, operations, quotas

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,63}$")
ROLES = ("admin", "user")
MAX_QUOTA = {"cpu_cores": 1024, "memory_bytes": 1 << 50, "disk_bytes": 1 << 55}


def parse_quota(data) -> quotas.Res:
    if not isinstance(data, dict) or set(data) - set(MAX_QUOTA):
        raise Invalid("quota must be an object with cpu_cores, memory_bytes, disk_bytes.", fields={"quota": "invalid"})
    vals = {}
    for key, limit in MAX_QUOTA.items():
        v = data.get(key, 0)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= limit:
            raise Invalid(f"{key} must be a non-negative integer.", fields={f"quota.{key}": "invalid"})
        vals[key] = v
    return quotas.Res(**vals)


def _user(conn, user_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() if is_uuid(user_id) else None
    if row is None:
        raise NotFound("User not found.")
    return row


def _active_admins(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'").fetchone()[0]


def list_users(conn: sqlite3.Connection) -> list[dict]:
    out = []
    invites = {r["user_id"]: r for r in conn.execute(
        "SELECT id, user_id, expires_at FROM invitations WHERE accepted_at IS NULL AND revoked_at IS NULL")}
    relations: dict[str, list] = {}
    for r in conn.execute("SELECT id, name, owner_id FROM containers WHERE status != 'deleted' AND owner_id IS NOT NULL"):
        relations.setdefault(r["owner_id"], []).append({"id": r["id"], "name": r["name"], "relation": "owner"})
    for r in conn.execute("SELECT c.id, c.name, a.user_id FROM container_access a JOIN containers c ON c.id = a.container_id"
                          " WHERE c.status != 'deleted'"):
        relations.setdefault(r["user_id"], []).append({"id": r["id"], "name": r["name"], "relation": "assigned"})
    for u in conn.execute("SELECT * FROM users ORDER BY status, email"):
        summary = quotas.owner_summary(conn, u["id"])
        inv = invites.get(u["id"])
        out.append({
            "id": u["id"], "email": u["email"], "display_name": u["display_name"], "role": u["role"],
            "status": u["status"], "quota": summary["quota"], "allocated": summary["allocated"],
            "pending": summary["pending"], "remaining": summary["remaining"],
            "invitation": {"id": inv["id"], "expires_at": inv["expires_at"]} if inv else None,
            "containers": relations.get(u["id"], []),
        })
    return out


def invite(conn: sqlite3.Connection, cfg: Config, *, actor_id: str, email: str, role: str, quota: quotas.Res,
           request_id: str | None = None) -> dict:
    email = canonical_email(email)
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise Invalid("Enter a valid email address.", fields={"email": "invalid"})
    if role not in ROLES:
        raise Invalid("Role must be admin or user.", fields={"role": "invalid"})
    secret = new_secret()
    now = utcnow()
    with write_tx(conn):
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is not None and user["status"] == "active":
            raise Conflict("This person already has an active account.", code="USER_EXISTS")
        if user is not None and user["status"] == "revoked":
            raise Conflict("This account was revoked; reinstatement is not supported.", code="USER_REVOKED")
        if user is None:
            user_id = new_id()
            conn.execute(
                "INSERT INTO users(id, email, display_name, role, status, quota_cpu_cores, quota_memory_bytes,"
                " quota_disk_bytes, created_at, updated_at) VALUES (?, ?, '', ?, 'pending', ?, ?, ?, ?, ?)",
                (user_id, email, role, quota.cpu_cores, quota.memory_bytes, quota.disk_bytes, iso(now), iso(now)))
        else:   # re-issue for a pending user: the previous link stops working
            user_id = user["id"]
            quotas.check_quota_change(conn, user_id, quota)
            conn.execute("UPDATE users SET role = ?, quota_cpu_cores = ?, quota_memory_bytes = ?, quota_disk_bytes = ?,"
                         " updated_at = ? WHERE id = ?",
                         (role, quota.cpu_cores, quota.memory_bytes, quota.disk_bytes, iso(now), user_id))
            conn.execute("UPDATE invitations SET revoked_at = ? WHERE email = ? AND accepted_at IS NULL"
                         " AND revoked_at IS NULL", (iso(now), email))
        inv_id = new_id()
        expires = iso(now + timedelta(hours=cfg.invitation_ttl_hours))
        conn.execute("INSERT INTO invitations(id, user_id, email, token_hash, invited_by, created_at, expires_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (inv_id, user_id, email, hash_secret(secret), actor_id, iso(now), expires))
        audit.record(conn, action="user.invite", outcome="succeeded", actor_id=actor_id, target_type="user",
                     target_id=user_id, target_label=email, request_id=request_id,
                     details={"role": role, "quota": quota.as_dict()})
    return {"invitation": {"id": inv_id, "email": email, "expires_at": expires}, "user_id": user_id,
            "link": f"{cfg.public_base_url}/invite/#{secret}"}


def revoke_invitation(conn: sqlite3.Connection, *, actor_id: str, invitation_id: str, request_id=None) -> None:
    with write_tx(conn):
        inv = conn.execute("SELECT * FROM invitations WHERE id = ?", (invitation_id,)).fetchone() \
            if is_uuid(invitation_id) else None
        if inv is None:
            raise NotFound("Invitation not found.")
        if inv["accepted_at"] or inv["revoked_at"]:
            raise Conflict("This invitation is no longer open.", code="INVITATION_CLOSED")
        conn.execute("UPDATE invitations SET revoked_at = ? WHERE id = ?", (utcnow_iso(), invitation_id))
        audit.record(conn, action="user.invitation_revoked", outcome="succeeded", actor_id=actor_id,
                     target_type="user", target_id=inv["user_id"], target_label=inv["email"], request_id=request_id)


def update_user(conn: sqlite3.Connection, *, actor_id: str, user_id: str, role: str | None,
                quota: quotas.Res | None, request_id=None) -> None:
    if role is not None and role not in ROLES:
        raise Invalid("Role must be admin or user.", fields={"role": "invalid"})
    with write_tx(conn):
        user = _user(conn, user_id)
        if user["status"] == "revoked":
            raise Conflict("Revoked accounts cannot be changed.", code="USER_REVOKED")
        before = {"role": user["role"], "quota": quotas.owner_quota(conn, user_id).as_dict()}
        if role is not None and role != user["role"]:
            if user["role"] == "admin" and user["status"] == "active" and _active_admins(conn) <= 1:
                raise Conflict("The last active admin cannot be demoted.", code="LAST_ADMIN")
            conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, utcnow_iso(), user_id))
        if quota is not None:
            quotas.check_quota_change(conn, user_id, quota)
            conn.execute("UPDATE users SET quota_cpu_cores = ?, quota_memory_bytes = ?, quota_disk_bytes = ?,"
                         " updated_at = ? WHERE id = ?",
                         (quota.cpu_cores, quota.memory_bytes, quota.disk_bytes, utcnow_iso(), user_id))
        after = {"role": role or user["role"], "quota": (quota or quotas.owner_quota(conn, user_id)).as_dict()}
        audit.record(conn, action="user.update", outcome="succeeded", actor_id=actor_id, target_type="user",
                     target_id=user_id, target_label=user["email"], request_id=request_id,
                     details={"before": before, "after": after})


def revoke_user(conn: sqlite3.Connection, *, actor_id: str, user_id: str, request_id=None) -> dict:
    """Immediate: sessions, grants and queued work go in the same transaction.
    Owned containers and their allocations stay until an admin transfers or deletes them."""
    with write_tx(conn):
        user = _user(conn, user_id)
        if user["status"] == "revoked":
            raise Conflict("Already revoked.", code="USER_REVOKED")
        if user["role"] == "admin" and user["status"] == "active" and _active_admins(conn) <= 1:
            raise Conflict("The last active admin cannot be revoked.", code="LAST_ADMIN")
        now = utcnow_iso()
        conn.execute("UPDATE users SET status = 'revoked', revoked_at = ?, updated_at = ? WHERE id = ?",
                     (now, now, user_id))
        n_sessions = sessions.revoke_all_for_user(conn, user_id)
        n_grants = conn.execute("DELETE FROM container_access WHERE user_id = ?", (user_id,)).rowcount
        conn.execute("UPDATE invitations SET revoked_at = ? WHERE user_id = ? AND accepted_at IS NULL"
                     " AND revoked_at IS NULL", (now, user_id))
        n_ops = operations.cancel_queued(conn, actor_id=user_id, reason="User was revoked before dispatch.")
        running = conn.execute("SELECT COUNT(*) FROM operations WHERE actor_id = ? AND state = 'running'",
                               (user_id,)).fetchone()[0]
        details = {"sessions_revoked": n_sessions, "grants_removed": n_grants, "queued_cancelled": n_ops,
                   "already_dispatched": running}
        audit.record(conn, action="user.revoke", outcome="succeeded", actor_id=actor_id, target_type="user",
                     target_id=user_id, target_label=user["email"], request_id=request_id, details=details)
    return details


def grant_access(conn: sqlite3.Connection, *, actor_id: str, container_id: str, user_id: str, request_id=None) -> None:
    with write_tx(conn):
        c = conn.execute("SELECT * FROM containers WHERE id = ? AND status != 'deleted'", (container_id,)).fetchone()
        if c is None:
            raise NotFound("Container not found.")
        if not c["managed"]:
            raise Conflict("Only app-managed containers can be assigned.", code="NOT_MANAGED")
        user = _user(conn, user_id)
        if user["status"] == "revoked":
            raise Conflict("Revoked users cannot be granted access.", code="USER_REVOKED")
        cur = conn.execute("INSERT OR IGNORE INTO container_access(container_id, user_id, granted_by, granted_at)"
                           " VALUES (?, ?, ?, ?)", (container_id, user_id, actor_id, utcnow_iso()))
        if cur.rowcount:
            audit.record(conn, action="access.grant", outcome="succeeded", actor_id=actor_id,
                         target_type="container", target_id=container_id, target_label=c["name"],
                         request_id=request_id, details={"user": user["email"]})


def remove_access(conn: sqlite3.Connection, *, actor_id: str, container_id: str, user_id: str, request_id=None) -> None:
    with write_tx(conn):
        c = conn.execute("SELECT * FROM containers WHERE id = ?", (container_id,)).fetchone()
        user = _user(conn, user_id)
        if c is None:
            raise NotFound("Container not found.")
        removed = conn.execute("DELETE FROM container_access WHERE container_id = ? AND user_id = ?",
                               (container_id, user_id)).rowcount
        if not removed:
            raise NotFound("That user has no access to this container.")
        operations.cancel_queued(conn, actor_id=user_id, container_id=container_id,
                                 reason="Access was removed before dispatch.")
        audit.record(conn, action="access.revoke", outcome="succeeded", actor_id=actor_id, target_type="container",
                     target_id=container_id, target_label=c["name"], request_id=request_id,
                     details={"user": user["email"]})


def transfer_owner(conn: sqlite3.Connection, *, actor_id: str, container_id: str, new_owner_id: str,
                   request_id=None) -> None:
    with write_tx(conn):
        c = conn.execute("SELECT * FROM containers WHERE id = ? AND status = 'active'", (container_id,)).fetchone()
        if c is None:
            raise NotFound("Container not found.")
        if not c["managed"]:
            raise Conflict("Only app-managed containers have an owner.", code="NOT_MANAGED")
        if operations.active_for_container(conn, container_id):
            raise Conflict("Wait for the current operation to finish.", code="OPERATION_IN_PROGRESS")
        new_owner = _user(conn, new_owner_id)
        if new_owner["status"] == "revoked":
            raise Conflict("Revoked users cannot own containers.", code="USER_REVOKED")
        if new_owner_id == c["owner_id"]:
            return
        alloc = quotas.Res(c["cpu_cores"] or 0, c["memory_bytes"] or 0, c["disk_bytes"] or 0)
        rem = (quotas.owner_quota(conn, new_owner_id) - quotas.owner_committed(conn, new_owner_id)
               - quotas.owner_pending(conn, new_owner_id))
        fields = {k: "Exceeds the new owner's remaining quota." for k in ("cpu_cores", "memory_bytes", "disk_bytes")
                  if getattr(alloc, k) > getattr(rem, k)}
        if fields:
            raise Conflict("The new owner's quota cannot cover this container.", code="QUOTA_EXCEEDED", fields=fields)
        old = conn.execute("SELECT email FROM users WHERE id = ?", (c["owner_id"],)).fetchone() if c["owner_id"] else None
        conn.execute("UPDATE containers SET owner_id = ?, version = version + 1 WHERE id = ?",
                     (new_owner_id, container_id))
        audit.record(conn, action="container.owner_transfer", outcome="succeeded", actor_id=actor_id,
                     target_type="container", target_id=container_id, target_label=c["name"], request_id=request_id,
                     details={"from": old["email"] if old else None, "to": new_owner["email"]})
