"""Who may get a session after Google authenticates them.

Three paths, each decided inside ONE short write transaction that also creates
the session:

- returning user: matched by Google's immutable ``sub`` only; must be active;
- invitation: the browser proved possession of the one-time invitation secret
  (bound to the OAuth transaction), the invitation is open and unexpired, and
  Google's verified email equals the invited email. The pending user row is
  bound to ``sub``. The secret check matters because Google's email
  verification alone does not prove current ownership of a non-Gmail mailbox;
- bootstrap: the configured BOOTSTRAP_ADMIN_EMAIL *and* possession of the
  one-time setup secret printed by ``hsm bootstrap``. Completion is persisted,
  so restarting with the variable set cannot mint another admin.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from ..config import Config
from ..db import write_tx
from ..security import hash_secret, new_id, new_secret, safe_equal
from ..services import audit, state
from ..timeutil import iso, parse, utcnow, utcnow_iso
from . import sessions
from .oidc import Identity, OIDCError

BOOTSTRAP_TTL = timedelta(minutes=30)


def _deny(conn: sqlite3.Connection, identity: Identity, code: str) -> OIDCError:
    audit.record(conn, action="auth.login", outcome="denied", actor_email=identity.email,
                 details={"reason": code})
    return OIDCError(code)


def admit(conn: sqlite3.Connection, cfg: Config, identity: Identity, tx: sqlite3.Row) -> tuple[str, str]:
    """Return (user_id, session_cookie_value) or raise OIDCError."""
    error: OIDCError | None = None
    with write_tx(conn):
        by_sub = conn.execute("SELECT * FROM users WHERE google_sub = ?", (identity.sub,)).fetchone()
        kind = tx["admission_kind"]
        if kind == "bootstrap":
            user_id, error = _bootstrap(conn, cfg, identity, tx, by_sub)
        elif kind == "invitation":
            user_id, error = _invitation(conn, identity, tx, by_sub)
        elif by_sub is None:
            user_id, error = None, _deny(conn, identity, "NOT_INVITED")
        elif by_sub["status"] != "active":
            user_id, error = None, _deny(conn, identity, "ACCOUNT_REVOKED")
        else:
            user_id = by_sub["id"]
        if error is None:
            token = sessions.create(conn, cfg, user_id)
            audit.record(conn, action="auth.login", outcome="succeeded", actor_id=user_id,
                         details={"path": kind or "returning"})
    # Raised after COMMIT so the denial audit record is kept.
    if error is not None:
        raise error
    return user_id, token


def _invitation(conn, identity, tx, by_sub):
    inv = conn.execute("SELECT * FROM invitations WHERE id = ?", (tx["admission_ref"],)).fetchone()
    if inv is None or inv["accepted_at"] or inv["revoked_at"] or parse(inv["expires_at"]) <= utcnow():
        return None, _deny(conn, identity, "INVITATION_INVALID")
    if identity.email != inv["email"]:
        return None, _deny(conn, identity, "EMAIL_MISMATCH")
    user = conn.execute("SELECT * FROM users WHERE id = ?", (inv["user_id"],)).fetchone()
    if user is None or user["status"] not in ("pending", "revoked"):
        return None, _deny(conn, identity, "INVITATION_INVALID")
    if by_sub is not None and by_sub["id"] != user["id"]:
        # This Google account already belongs to another user; never merge identities.
        return None, _deny(conn, identity, "EMAIL_MISMATCH")
    if user["google_sub"] is not None and user["google_sub"] != identity.sub:
        # Reinstating a revoked user: only the Google account bound before may come back, even if
        # someone else now controls the same email address.
        return None, _deny(conn, identity, "EMAIL_MISMATCH")
    now = utcnow_iso()
    conn.execute("UPDATE users SET google_sub = ?, status = 'active', display_name = ?, revoked_at = NULL,"
                 " updated_at = ? WHERE id = ? AND status IN ('pending', 'revoked')",
                 (identity.sub, identity.name, now, user["id"]))
    conn.execute("UPDATE invitations SET accepted_at = ? WHERE id = ?", (now, inv["id"]))
    audit.record(conn, action="user.invitation_accepted", outcome="succeeded", actor_id=user["id"],
                 target_type="user", target_id=user["id"], target_label=user["email"])
    return user["id"], None


def _bootstrap(conn, cfg, identity, tx, by_sub):
    record = state.get(conn, state.BOOTSTRAP)
    if (not record or record.get("completed_at") or not cfg.bootstrap_admin_email
            or not safe_equal(record.get("secret_hash", ""), tx["admission_ref"] or "")
            or parse(record["expires_at"]) <= utcnow()):
        return None, _deny(conn, identity, "BOOTSTRAP_INVALID")
    if identity.email != cfg.bootstrap_admin_email:
        return None, _deny(conn, identity, "EMAIL_MISMATCH")
    if conn.execute("SELECT 1 FROM users WHERE role = 'admin' AND status = 'active'").fetchone():
        return None, _deny(conn, identity, "BOOTSTRAP_INVALID")
    now = utcnow_iso()
    existing = by_sub or conn.execute("SELECT * FROM users WHERE email = ?", (identity.email,)).fetchone()
    if existing is not None:
        if existing["status"] == "revoked":
            return None, _deny(conn, identity, "ACCOUNT_REVOKED")
        user_id = existing["id"]
        conn.execute("UPDATE users SET google_sub = ?, role = 'admin', status = 'active', display_name = ?,"
                     " updated_at = ? WHERE id = ?", (identity.sub, identity.name, now, user_id))
    else:
        user_id = new_id()
        conn.execute(
            "INSERT INTO users(id, google_sub, email, display_name, role, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'admin', 'active', ?, ?)",
            (user_id, identity.sub, identity.email, identity.name, now, now))
    state.put(conn, state.BOOTSTRAP, {**record, "completed_at": now, "user_id": user_id})
    audit.record(conn, action="auth.bootstrap_admin", outcome="succeeded", actor_id=user_id,
                 target_type="user", target_id=user_id, target_label=identity.email)
    return user_id, None


# ------------------------------------------------------------- admission context

def invitation_for_secret(conn: sqlite3.Connection, secret: str) -> sqlite3.Row | None:
    inv = conn.execute("SELECT * FROM invitations WHERE token_hash = ?", (hash_secret(secret),)).fetchone()
    if inv is None or inv["accepted_at"] or inv["revoked_at"] or parse(inv["expires_at"]) <= utcnow():
        return None
    return inv


def bootstrap_secret_valid(conn: sqlite3.Connection, secret: str) -> bool:
    record = state.get(conn, state.BOOTSTRAP)
    return bool(record and not record.get("completed_at")
                and safe_equal(record.get("secret_hash", ""), hash_secret(secret))
                and parse(record["expires_at"]) > utcnow())


def issue_bootstrap_secret(conn: sqlite3.Connection, cfg: Config) -> str:
    """Operator CLI only. Refuses once any admin exists or bootstrap completed."""
    if not cfg.bootstrap_admin_email:
        raise RuntimeError("BOOTSTRAP_ADMIN_EMAIL is not set")
    with write_tx(conn):
        record = state.get(conn, state.BOOTSTRAP) or {}
        if record.get("completed_at"):
            raise RuntimeError("bootstrap already completed; use `hsm recover-admin` for recovery")
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin' AND status = 'active'").fetchone():
            raise RuntimeError("an active admin already exists")
        secret = new_secret()
        state.put(conn, state.BOOTSTRAP, {"secret_hash": hash_secret(secret),
                                          "expires_at": iso(utcnow() + BOOTSTRAP_TTL),
                                          "email": cfg.bootstrap_admin_email})
        audit.record(conn, action="auth.bootstrap_secret_issued", outcome="succeeded",
                     details={"email": cfg.bootstrap_admin_email})
    return secret
