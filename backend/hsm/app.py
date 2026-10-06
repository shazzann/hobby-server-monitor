"""Falcon WSGI application factory.

Middleware order: context (request id, DB connection, security headers) ->
authentication/authorization (route policy, session, CSRF/origin, object
checks). The static Astro build is served by a GET-only sink for every path
outside /api, /auth and /health.
"""
from __future__ import annotations

import logging
import mimetypes
import secrets
import threading
from pathlib import Path

import falcon

from . import config as config_mod
from . import db
from .api import auth as auth_api
from .api import containers as containers_api
from .api import misc as misc_api
from .api import users as users_api
from .auth import policy, sessions
from .errors import BadRequest, Forbidden, HSMError, NotFound, Unauthenticated

log = logging.getLogger("hsm.api")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class Database:
    """One SQLite connection per server thread."""

    def __init__(self, path):
        self.path = path
        self.local = threading.local()

    def conn(self):
        c = getattr(self.local, "conn", None)
        if c is None:
            c = self.local.conn = db.connect(self.path)
        return c


class ContextMiddleware:
    def __init__(self, cfg, database):
        self.cfg, self.database = cfg, database

    def process_request(self, req, resp):
        req.context.request_id = secrets.token_hex(8)
        req.context.cfg = self.cfg
        req.context.db = self.database.conn()
        req.context.principal = None

    def process_response(self, req, resp, resource, req_succeeded):
        resp.set_header("X-Request-Id", req.context.get("request_id", ""))
        resp.set_header("Referrer-Policy", "no-referrer")
        resp.set_header("X-Content-Type-Options", "nosniff")
        resp.set_header("X-Frame-Options", "DENY")
        resp.set_header("Content-Security-Policy", CSP)
        resp.set_header("Cross-Origin-Opener-Policy", "same-origin")
        if self.cfg.secure_cookies:
            resp.set_header("Strict-Transport-Security", "max-age=31536000")
        if req.path.startswith(("/api/", "/auth/", "/health/")):
            resp.set_header("Cache-Control", "no-store")


class AuthMiddleware:
    """Enforces the declared route policy before any responder runs."""

    def __init__(self, cfg):
        self.cfg = cfg

    def process_resource(self, req, resp, resource, params):
        if resource is None:          # static sink or 404; the sink is GET-only and public
            return
        declared = policy.declared_policy(resource, req.method)
        if declared is None:
            if req.method in ("HEAD", "OPTIONS"):
                raise falcon.HTTPMethodNotAllowed(sorted(resource.policies))
            raise Forbidden("No authorization policy is declared for this route.", code="POLICY_MISSING")
        conn = req.context.db
        principal = sessions.resolve(conn, self.cfg, req.cookies.get(sessions.cookie_name(self.cfg)))
        req.context.principal = principal

        if req.method in UNSAFE_METHODS:
            origin = req.get_header("Origin")
            if origin != self.cfg.public_origin:
                raise Forbidden("Cross-origin request rejected.", code="ORIGIN_REJECTED")
        if declared in policy.NEEDS_SESSION:
            if principal is None:
                raise Unauthenticated("Sign in to continue.")
            if req.method in UNSAFE_METHODS and not sessions.check_csrf(principal, req.get_header("X-CSRF-Token")):
                raise Forbidden("Missing or invalid CSRF token.", code="CSRF_FAILED")
        if declared == policy.ADMIN and not principal.is_admin:
            raise Forbidden("Administrator access is required.")
        if declared in (policy.CONTAINER_VIEW, policy.CONTAINER_EXEC):
            need = "exec" if declared == policy.CONTAINER_EXEC else "view"
            req.context.container = policy.container_for(conn, principal.user_id, params["container_id"], need)
        if declared == policy.OPERATION_VIEW:
            req.context.operation, req.context.may_see_result = policy.operation_for(
                conn, principal.user_id, params["operation_id"])


def _error_body(req, code, message, fields=None, extra=None):
    err = {"code": code, "message": message, "fields": fields or {},
           "request_id": req.context.get("request_id")}
    err.update(extra or {})
    return {"error": err}


def _handle_hsm_error(req, resp, ex: HSMError, params):
    resp.status = falcon.code_to_http_status(ex.status)
    resp.media = _error_body(req, ex.code, ex.message, ex.fields, ex.extra)


def _handle_http_error(req, resp, ex: falcon.HTTPError, params):
    resp.status = ex.status
    code = {400: "BAD_REQUEST", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 413: "TOO_LARGE",
            415: "UNSUPPORTED_MEDIA_TYPE"}.get(ex.status_code, "HTTP_ERROR")
    resp.media = _error_body(req, code, ex.title or "Request failed.")
    for k, v in (ex.headers or {}).items():
        resp.set_header(k, v)


def _handle_unexpected(req, resp, ex: Exception, params):
    log.exception("unhandled error request_id=%s", req.context.get("request_id"))
    resp.status = falcon.HTTP_500
    resp.media = _error_body(req, "INTERNAL", "An unexpected error occurred.")


class StaticSink:
    """Serves the Astro build. Paths are resolved inside dist; no listings."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def __call__(self, req, resp, **kwargs):
        if req.method not in ("GET", "HEAD"):
            raise falcon.HTTPMethodNotAllowed(["GET", "HEAD"])
        rel = req.path.lstrip("/")
        target = (self.root / rel).resolve()
        if self.root != target and self.root not in target.parents:
            raise NotFound("Not found.")
        if target.is_dir():
            if not req.path.endswith("/"):
                raise falcon.HTTPMovedPermanently(req.path + "/")
            target = target / "index.html"
        if not target.is_file():
            page = self.root / "404.html"
            if not page.is_file():
                raise NotFound("Not found.")
            resp.status, target = falcon.HTTP_404, page
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        resp.content_type = ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else "")
        # Hashed Astro assets are immutable; HTML must revalidate.
        resp.cache_control = ["public", "max-age=31536000", "immutable"] if "/_astro/" in req.path else ["no-cache"]
        resp.data = target.read_bytes()


def routes():
    """(path, resource) list; also used by the policy-coverage test."""
    return [
        ("/health/live", misc_api.Live()),
        ("/auth/google/start", auth_api.GoogleStart()),
        ("/auth/admission-context", auth_api.AdmissionContext()),
        ("/auth/google/callback", auth_api.GoogleCallback()),
        ("/auth/logout", auth_api.Logout()),
        ("/api/me", misc_api.Me()),
        ("/api/health", misc_api.Health()),
        ("/api/containers", containers_api.Collection()),
        ("/api/containers/{container_id}", containers_api.Item()),
        ("/api/containers/{container_id}/limits", containers_api.Limits()),
        ("/api/containers/{container_id}/actions", containers_api.Actions()),
        ("/api/containers/{container_id}/owner", containers_api.Owner()),
        ("/api/containers/{container_id}/assignments/{user_id}", containers_api.Assignment()),
        ("/api/containers/{container_id}/exec", containers_api.Exec()),
        ("/api/containers/{container_id}/history", containers_api.History()),
        ("/api/containers/{container_id}/usage", containers_api.Usage()),
        ("/api/creation-options", containers_api.CreationOptions()),
        ("/api/operations/{operation_id}", misc_api.Operation()),
        ("/api/users", users_api.Collection()),
        ("/api/users/{user_id}", users_api.Item()),
        ("/api/users/{user_id}/revoke", users_api.Revoke()),
        ("/api/invitations", users_api.Invitations()),
        ("/api/invitations/{invitation_id}", users_api.Invitation()),
        ("/api/accounting", misc_api.Accounting()),
        ("/api/audit-events", misc_api.AuditEvents()),
    ]


def create_app(cfg=None) -> falcon.App:
    cfg = cfg or config_mod.load()
    if not cfg.session_secret or (cfg.deployment_mode == "https" and len(cfg.session_secret) < 32):
        raise RuntimeError("SESSION_SECRET must be set (32+ characters in https mode)")
    database = Database(cfg.sqlite_path)
    if db.schema_version(database.conn()) == 0:
        raise RuntimeError("database is not initialized; run `hsm init-db` first")
    app = falcon.App(middleware=[ContextMiddleware(cfg, database), AuthMiddleware(cfg)])
    app.resp_options.secure_cookies_by_default = cfg.secure_cookies
    table = routes()
    policy.assert_all_routes_declared(table)
    for path, resource in table:
        app.add_route(path, resource)
    app.add_sink(StaticSink(cfg.dashboard_dist), prefix=r"/(?!api/|auth/|health/)")
    app.add_error_handler(Exception, _handle_unexpected)
    app.add_error_handler(falcon.HTTPError, _handle_http_error)
    app.add_error_handler(HSMError, _handle_hsm_error)
    return app


def wsgi():
    """Gunicorn entry point: `gunicorn 'hsm.app:wsgi()'`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return create_app()


__all__ = ["create_app", "wsgi", "routes", "BadRequest"]
