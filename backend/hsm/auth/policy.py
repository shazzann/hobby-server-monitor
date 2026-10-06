"""Default-deny authorization.

Two layers, both mandatory:

1. Route policy. Every Falcon responder must be declared in its resource's
   ``policies`` mapping (method -> policy). ``assert_all_routes_declared`` runs
   at app start-up and in the test-suite, so an endpoint added next month
   without a policy fails before it can serve a request.
2. Object policy. ``container_for`` / ``operation_for`` are the only ways the
   API, the services and the worker resolve a container or operation for a
   user. They re-read the user's *current* role/status and assignments.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import Conflict, Forbidden, NotFound
from ..security import is_uuid

PUBLIC = "public"                 # no session needed
OAUTH = "oauth"                   # OAuth callback: protected by its own state/binding checks
AUTHENTICATED = "authenticated"
ADMIN = "admin"
CONTAINER_VIEW = "container-view"
CONTAINER_EXEC = "container-exec"
OPERATION_VIEW = "operation-view"

ALL_POLICIES = {PUBLIC, OAUTH, AUTHENTICATED, ADMIN, CONTAINER_VIEW, CONTAINER_EXEC, OPERATION_VIEW}
NEEDS_SESSION = ALL_POLICIES - {PUBLIC, OAUTH}

HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def declared_policy(resource: Any, method: str) -> str | None:
    return getattr(resource, "policies", {}).get(method)


def assert_all_routes_declared(routes: list[tuple[str, Any]]) -> None:
    """Fail start-up if any responder lacks a known policy."""
    problems = []
    for path, resource in routes:
        policies = getattr(resource, "policies", None)
        if not isinstance(policies, dict):
            problems.append(f"{path}: resource has no policies mapping")
            continue
        for method in HTTP_METHODS:
            if hasattr(resource, "on_" + method.lower()):
                p = policies.get(method)
                if p not in ALL_POLICIES:
                    problems.append(f"{method} {path}: missing or unknown policy {p!r}")
        for method in policies:
            if not hasattr(resource, "on_" + method.lower()):
                problems.append(f"{method} {path}: policy declared without responder")
    if problems:
        raise RuntimeError("route policy check failed:\n  " + "\n  ".join(problems))


def active_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or row["status"] != "active":
        raise Forbidden("Your account is not active.", code="ACCOUNT_INACTIVE")
    return row


def has_assignment(conn: sqlite3.Connection, user_id: str, container_id: str) -> bool:
    return conn.execute("SELECT 1 FROM container_access WHERE container_id = ? AND user_id = ?",
                        (container_id, user_id)).fetchone() is not None


def container_for(conn: sqlite3.Connection, user_id: str, container_id: str, need: str) -> sqlite3.Row:
    """Resolve a live container for ``user_id`` or raise.

    need: 'view' | 'exec' | 'manage'
    Unknown id -> 404. Existing but not assigned -> 403 (as the brief requires).
    """
    user = active_user(conn, user_id)
    if not is_uuid(container_id):
        raise NotFound("Container not found.")
    c = conn.execute("SELECT * FROM containers WHERE id = ? AND status != 'deleted'",
                     (container_id,)).fetchone()
    if c is None:
        raise NotFound("Container not found.")
    if user["role"] != "admin":
        if need == "manage" or not has_assignment(conn, user_id, container_id):
            raise Forbidden("You do not have access to this container.")
    if need == "exec":
        if not c["managed"] or c["status"] != "active":
            raise Conflict("Commands can only run on active, app-managed containers.",
                           code="CONTAINER_NOT_EXECUTABLE")
        if c["safety"] != "safe":
            raise Conflict("This container failed the safety check; commands are disabled.",
                           code="CONTAINER_UNSAFE")
    return c


def operation_for(conn: sqlite3.Connection, user_id: str, operation_id: str) -> tuple[sqlite3.Row, bool]:
    """Return (operation, may_see_result). Admins see every operation's status;
    only the actor sees an exec result, and only while they still have access."""
    user = active_user(conn, user_id)
    if not is_uuid(operation_id):
        raise NotFound("Operation not found.")
    op = conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if op is None:
        raise NotFound("Operation not found.")
    is_actor = op["actor_id"] == user_id
    if user["role"] == "admin":
        return op, is_actor
    if not is_actor:
        raise Forbidden("You do not have access to this operation.")
    if op["container_id"] and not has_assignment(conn, user_id, op["container_id"]):
        raise Forbidden("You no longer have access to this container.")
    return op, True
