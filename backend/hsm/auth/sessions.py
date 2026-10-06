"""Opaque server-side sessions.

The cookie carries 256 random bits; SQLite stores only its SHA-256. Every
request re-reads the session *and* the user's current role/status, so
revocation and demotion take effect on the next request.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from ..config import Config
from ..security import hash_secret, hmac_hex, new_id, new_secret, safe_equal
from ..timeutil import iso, parse, utcnow

LAST_SEEN_WRITE_INTERVAL = timedelta(seconds=60)   # batch idle-tracking writes


def cookie_name(cfg: Config) -> str:
    # __Host- prefix forces Secure, Path=/ and no Domain in browsers (HTTPS only).
    return "__Host-hsm_session" if cfg.secure_cookies else "hsm_session"


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    display_name: str
    role: str
    session_id: str
    csrf_token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def csrf_for(cfg: Config, session_id: str) -> str:
    return hmac_hex(cfg.session_secret, "csrf:" + session_id)


def create(conn: sqlite3.Connection, cfg: Config, user_id: str) -> str:
    """Insert a session row (caller owns the transaction). Returns the cookie value."""
    token = new_secret()
    now = utcnow()
    conn.execute(
        "INSERT INTO sessions(id, token_hash, user_id, created_at, expires_at, last_seen_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (new_id(), hash_secret(token), user_id, iso(now),
         iso(now + timedelta(hours=cfg.session_absolute_hours)), iso(now)),
    )
    return token


def resolve(conn: sqlite3.Connection, cfg: Config, token: str | None) -> Principal | None:
    if not token or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT s.id AS sid, s.expires_at, s.last_seen_at, s.revoked_at,"
        " u.id AS uid, u.email, u.display_name, u.role, u.status"
        " FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (hash_secret(token),),
    ).fetchone()
    if row is None or row["revoked_at"] is not None or row["status"] != "active":
        return None
    now = utcnow()
    last_seen = parse(row["last_seen_at"])
    if now >= parse(row["expires_at"]) or now - last_seen > timedelta(minutes=cfg.session_idle_minutes):
        return None
    if now - last_seen > LAST_SEEN_WRITE_INTERVAL:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ? AND revoked_at IS NULL",
                     (iso(now), row["sid"]))
    return Principal(row["uid"], row["email"], row["display_name"], row["role"], row["sid"],
                     csrf_for(cfg, row["sid"]))


def revoke(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (iso(utcnow()), session_id))


def revoke_all_for_user(conn: sqlite3.Connection, user_id: str) -> int:
    cur = conn.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                       (iso(utcnow()), user_id))
    return cur.rowcount


def check_csrf(principal: Principal, header_value: str | None) -> bool:
    return bool(header_value) and safe_equal(principal.csrf_token, header_value)


def purge_expired(conn: sqlite3.Connection) -> None:
    cutoff = iso(utcnow() - timedelta(days=1))
    conn.execute("DELETE FROM sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
                 (cutoff, cutoff))
    conn.execute("DELETE FROM oauth_transactions WHERE expires_at < ?", (cutoff,))
