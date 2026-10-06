"""Google OpenID Connect (authorization-code flow with PKCE).

Libraries do the security-sensitive work:
- Authlib's OAuth2Session builds the authorization URL (state, nonce, S256
  PKCE challenge) and performs the code exchange;
- google-auth's ``verify_oauth2_token`` checks the ID token's signature against
  Google's published keys, issuer, audience and expiry.
This module adds what those calls cannot know: a single-use transaction bound
to the browser by an HttpOnly cookie, the nonce comparison, and the
verified-email requirement.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from authlib.common.security import generate_token
from authlib.integrations.requests_client import OAuth2Session

from ..config import Config
from ..db import write_tx
from ..security import hash_secret, new_id, new_secret, safe_equal
from ..timeutil import iso, parse, utcnow

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
TRANSACTION_TTL = timedelta(minutes=10)
BINDING_COOKIE = "hsm_oauth"


class OIDCError(Exception):
    """Login failed; ``code`` is shown to the user on /login/."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    email_verified: bool
    name: str


def _session(cfg: Config) -> OAuth2Session:
    return OAuth2Session(cfg.google_client_id, cfg.google_client_secret, scope="openid email profile",
                         redirect_uri=cfg.google_redirect_uri, code_challenge_method="S256")


def begin(conn: sqlite3.Connection, cfg: Config, *, admission_kind: str | None = None,
          admission_ref: str | None = None) -> tuple[str, str]:
    """Create a login transaction. Returns (authorize_url, binding_cookie_value)."""
    if not cfg.google_client_id or not cfg.google_client_secret:
        raise OIDCError("OAUTH_NOT_CONFIGURED", "Google OAuth client is not configured")
    binding, state, nonce = new_secret(), new_secret(), new_secret()
    verifier = generate_token(64)
    url, _ = _session(cfg).create_authorization_url(
        AUTHORIZE_URL, state=state, nonce=nonce, code_verifier=verifier, prompt="select_account")
    now = utcnow()
    with write_tx(conn):
        conn.execute(
            "INSERT INTO oauth_transactions(id, state_hash, browser_hash, nonce, code_verifier, admission_kind,"
            " admission_ref, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id(), hash_secret(state), hash_secret(binding), nonce, verifier, admission_kind, admission_ref,
             iso(now), iso(now + TRANSACTION_TTL)))
    return url, binding


def consume_transaction(conn: sqlite3.Connection, state: str | None, binding: str | None) -> sqlite3.Row:
    """Single-use: the transaction is marked consumed before the code exchange."""
    if not state or not binding or len(state) > 128 or len(binding) > 128:
        raise OIDCError("STATE_INVALID", "missing state or browser binding")
    with write_tx(conn):
        tx = conn.execute("SELECT * FROM oauth_transactions WHERE state_hash = ?", (hash_secret(state),)).fetchone()
        if tx is None or tx["consumed_at"] is not None:
            raise OIDCError("STATE_INVALID", "unknown or replayed state")
        conn.execute("UPDATE oauth_transactions SET consumed_at = ? WHERE id = ?", (iso(utcnow()), tx["id"]))
    if parse(tx["expires_at"]) <= utcnow():
        raise OIDCError("STATE_INVALID", "login transaction expired")
    if not safe_equal(tx["browser_hash"], hash_secret(binding)):
        raise OIDCError("STATE_INVALID", "login started in a different browser")
    return tx


def exchange_code(cfg: Config, code: str, verifier: str) -> str:
    """Code -> ID token (server-to-server). Google tokens are not retained."""
    token = _session(cfg).fetch_token(TOKEN_URL, grant_type="authorization_code", code=code,
                                      code_verifier=verifier, timeout=10)
    id_token = token.get("id_token")
    if not id_token:
        raise OIDCError("OAUTH_FAILED", "no id_token in token response")
    return id_token


def verify_id_token(cfg: Config, id_token: str) -> dict:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(id_token, google_requests.Request(), audience=cfg.google_client_id,
                                               clock_skew_in_seconds=30)


def complete(conn: sqlite3.Connection, cfg: Config, *, state: str | None, code: str | None, binding: str | None,
             exchange: Callable[[Config, str, str], str] | None = None,
             verify: Callable[[Config, str], dict] | None = None) -> tuple[Identity, sqlite3.Row]:
    tx = consume_transaction(conn, state, binding)
    if not code or len(code) > 2048:
        raise OIDCError("OAUTH_FAILED", "missing authorization code")
    try:
        id_token = (exchange or exchange_code)(cfg, code, tx["code_verifier"])
        claims = (verify or verify_id_token)(cfg, id_token)
    except OIDCError:
        raise
    except Exception as exc:  # network failure, invalid signature, wrong audience, expired token
        raise OIDCError("OAUTH_FAILED", type(exc).__name__) from exc
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise OIDCError("OAUTH_FAILED", "unexpected issuer")
    if claims.get("aud") != cfg.google_client_id:
        raise OIDCError("OAUTH_FAILED", "unexpected audience")
    if not claims.get("nonce") or not safe_equal(str(claims["nonce"]), tx["nonce"]):
        raise OIDCError("OAUTH_FAILED", "nonce mismatch")
    if not claims.get("sub"):
        raise OIDCError("OAUTH_FAILED", "missing subject")
    if claims.get("email_verified") is not True:
        raise OIDCError("EMAIL_UNVERIFIED", "Google has not verified this email")
    return Identity(sub=str(claims["sub"]), email=str(claims.get("email", "")).strip().lower(),
                    email_verified=True, name=str(claims.get("name", ""))[:200]), tx
