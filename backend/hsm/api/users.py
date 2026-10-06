"""User management resources (admin only)."""
from __future__ import annotations

import falcon

from ..auth import policy
from ..errors import Invalid
from ..services import users as svc
from .common import read_json


class Collection:
    policies = {"GET": policy.ADMIN}

    def on_get(self, req, resp):
        resp.media = {"users": svc.list_users(req.context.db)}


class Item:
    policies = {"PATCH": policy.ADMIN}

    def on_patch(self, req, resp, user_id):
        ctx = req.context
        body = read_json(req)
        if not body or set(body) - {"role", "quota"}:
            raise Invalid("Body may contain only role and quota.", fields={k: "not allowed" for k in set(body) - {"role", "quota"}})
        quota = svc.parse_quota(body["quota"]) if "quota" in body else None
        svc.update_user(ctx.db, actor_id=ctx.principal.user_id, user_id=user_id, role=body.get("role"), quota=quota,
                        request_id=ctx.request_id)
        resp.media = {"users": svc.list_users(ctx.db)}


class Revoke:
    policies = {"POST": policy.ADMIN}

    def on_post(self, req, resp, user_id):
        ctx = req.context
        resp.media = {"revoked": svc.revoke_user(ctx.db, actor_id=ctx.principal.user_id, user_id=user_id,
                                                 request_id=ctx.request_id),
                      "note": "Work already dispatched to LXD before revocation may still complete."}


class Invitations:
    policies = {"POST": policy.ADMIN}

    def on_post(self, req, resp):
        ctx = req.context
        body = read_json(req)
        if set(body) - {"email", "role", "quota"} or "email" not in body:
            raise Invalid("Body must contain email, role and quota.", fields={"email": "required"})
        quota = svc.parse_quota(body.get("quota") or {})
        resp.status = falcon.HTTP_201
        resp.media = svc.invite(ctx.db, ctx.cfg, actor_id=ctx.principal.user_id, email=str(body["email"]),
                                role=body.get("role", "user"), quota=quota, request_id=ctx.request_id)


class Invitation:
    policies = {"DELETE": policy.ADMIN}

    def on_delete(self, req, resp, invitation_id):
        ctx = req.context
        svc.revoke_invitation(ctx.db, actor_id=ctx.principal.user_id, invitation_id=invitation_id,
                              request_id=ctx.request_id)
        resp.status = falcon.HTTP_204
