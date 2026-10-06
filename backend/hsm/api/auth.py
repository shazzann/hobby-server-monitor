"""Login, admission context and logout."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import falcon

from ..auth import admission, oidc, policy, sessions
from ..errors import BadRequest, Invalid
from ..security import hash_secret
from ..services import ratelimit
from .common import client_ip, read_json

log = logging.getLogger("hsm.auth")
BINDING_MAX_AGE = int(oidc.TRANSACTION_TTL.total_seconds())
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _set_binding(req, resp, value: str) -> None:
    cfg = req.context.cfg
    resp.set_cookie(oidc.BINDING_COOKIE, value, max_age=BINDING_MAX_AGE, path="/auth/",
                    secure=cfg.secure_cookies, http_only=True, same_site="Lax")


def _login_error(resp, code: str) -> None:
    raise falcon.HTTPFound(f"/login/?error={code}")


class GoogleStart:
    policies = {"GET": policy.PUBLIC}

    def on_get(self, req, resp):
        cfg, conn = req.context.cfg, req.context.db
        ratelimit.check(conn, f"start:{client_ip(req)}", limit=30, window_seconds=600)
        try:
            url, binding = oidc.begin(conn, cfg)
        except oidc.OIDCError as exc:
            _login_error(resp, exc.code)
        _set_binding(req, resp, binding)
        raise falcon.HTTPFound(url)


class AdmissionContext:
    """Binds a one-time invitation/bootstrap secret to a new login transaction.
    The secret arrives in a same-origin POST body (never a query string)."""
    policies = {"POST": policy.PUBLIC}

    def on_post(self, req, resp):
        cfg, conn = req.context.cfg, req.context.db
        # Per client only: the secrets are 256-bit, so a global cap would add no protection
        # and would let one client lock everybody out of accepting invitations.
        ratelimit.check(conn, f"admission:{client_ip(req)}", limit=10, window_seconds=600)
        body = read_json(req)
        kind, secret = body.get("kind"), body.get("secret")
        if kind not in ("invitation", "bootstrap") or set(body) - {"kind", "secret"}:
            raise Invalid("kind must be 'invitation' or 'bootstrap'.", fields={"kind": "invalid"})
        if not isinstance(secret, str) or not 20 <= len(secret) <= 128:
            raise BadRequest("This link is invalid or has expired.", code=f"{kind.upper()}_INVALID")
        if kind == "invitation":
            inv = admission.invitation_for_secret(conn, secret)
            if inv is None:
                raise BadRequest("This invitation link is invalid, used or expired.", code="INVITATION_INVALID")
            ref = inv["id"]
        else:
            if not admission.bootstrap_secret_valid(conn, secret):
                raise BadRequest("This setup link is invalid, used or expired.", code="BOOTSTRAP_INVALID")
            ref = hash_secret(secret)
        try:
            url, binding = oidc.begin(conn, cfg, admission_kind=kind, admission_ref=ref)
        except oidc.OIDCError as exc:
            raise BadRequest("Google sign-in is not configured.", code=exc.code)
        _set_binding(req, resp, binding)
        resp.media = {"authorize_url": url}


class GoogleCallback:
    policies = {"GET": policy.OAUTH}

    def on_get(self, req, resp):
        cfg, conn = req.context.cfg, req.context.db
        binding = req.cookies.get(oidc.BINDING_COOKIE)
        resp.unset_cookie(oidc.BINDING_COOKIE, path="/auth/")
        if req.get_param("error"):
            _login_error(resp, "OAUTH_FAILED")
        try:
            identity, tx = oidc.complete(conn, cfg, state=req.get_param("state"), code=req.get_param("code"),
                                         binding=binding)
            user_id, token = admission.admit(conn, cfg, identity, tx)
        except oidc.OIDCError as exc:
            log.info("login denied code=%s request_id=%s", exc.code, req.context.request_id)
            _login_error(resp, exc.code)
        # A fresh session id on every login prevents session fixation; drop any previous one.
        old = sessions.resolve(conn, cfg, req.cookies.get(sessions.cookie_name(cfg)))
        if old is not None:
            sessions.revoke(conn, old.session_id)
        resp.set_cookie(sessions.cookie_name(cfg), token, max_age=cfg.session_absolute_hours * 3600, path="/",
                        secure=cfg.secure_cookies, http_only=True, same_site="Lax")
        raise falcon.HTTPFound("/")


class Logout:
    policies = {"POST": policy.AUTHENTICATED}

    def on_post(self, req, resp):
        cfg = req.context.cfg
        sessions.revoke(req.context.db, req.context.principal.session_id)
        # unset_cookie() omits Secure, which browsers require to overwrite a __Host- cookie.
        # (Falcon omits max_age=0 as falsy, so expire it with a past date.)
        resp.set_cookie(sessions.cookie_name(cfg), "", expires=EPOCH, path="/", secure=cfg.secure_cookies,
                        http_only=True, same_site="Lax")
        resp.status = falcon.HTTP_204
