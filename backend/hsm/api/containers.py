"""Container resources. Policy and object checks have already run in
AuthMiddleware; the resolved row is ``req.context.container``."""
from __future__ import annotations

import time

import falcon

from ..auth import policy
from ..errors import Invalid
from ..services import containers as svc
from ..services import users as users_svc
from .common import idempotency_key, read_json, respond_operation

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
HISTORY_METRICS = ("cpu_pct", "memory_bytes", "disk_bytes", "rx_rate", "tx_rate", "processes")


class Collection:
    policies = {"GET": policy.AUTHENTICATED, "POST": policy.ADMIN}

    def on_get(self, req, resp):
        ctx = req.context
        resp.media = {"containers": svc.list_for(ctx.db, ctx.cfg, ctx.principal),
                      "collector": svc.collector_status(ctx.db, ctx.cfg)}

    def on_post(self, req, resp):
        ctx = req.context
        op, created, cid = svc.submit_create(ctx.db, ctx.cfg, ctx.principal, read_json(req), idempotency_key(req),
                                             ctx.request_id)
        respond_operation(req, resp, op, created, container_id=cid)


class Item:
    policies = {"GET": policy.CONTAINER_VIEW, "DELETE": policy.ADMIN}

    def on_get(self, req, resp, container_id):
        ctx = req.context
        resp.media = svc.detail(ctx.db, ctx.cfg, ctx.principal, ctx.container)

    def on_delete(self, req, resp, container_id):
        ctx = req.context
        c = policy.container_for(ctx.db, ctx.principal.user_id, container_id, "manage")
        op, created = svc.submit_delete(ctx.db, ctx.cfg, ctx.principal, c, req.get_param("confirm_name"),
                                        idempotency_key(req), ctx.request_id)
        respond_operation(req, resp, op, created)


class Limits:
    policies = {"PATCH": policy.ADMIN}

    def on_patch(self, req, resp, container_id):
        ctx = req.context
        c = policy.container_for(ctx.db, ctx.principal.user_id, container_id, "manage")
        op, created = svc.submit_limits(ctx.db, ctx.cfg, ctx.principal, c, read_json(req), idempotency_key(req),
                                        ctx.request_id)
        respond_operation(req, resp, op, created)


class Actions:
    policies = {"POST": policy.ADMIN}

    def on_post(self, req, resp, container_id):
        ctx = req.context
        c = policy.container_for(ctx.db, ctx.principal.user_id, container_id, "manage")
        op, created = svc.submit_action(ctx.db, ctx.cfg, ctx.principal, c, read_json(req), idempotency_key(req),
                                        ctx.request_id)
        respond_operation(req, resp, op, created)


class Adopt:
    policies = {"POST": policy.ADMIN}

    def on_post(self, req, resp, container_id):
        ctx = req.context
        c = policy.container_for(ctx.db, ctx.principal.user_id, container_id, "manage")
        op, created = svc.submit_adopt(ctx.db, ctx.cfg, ctx.principal, c, read_json(req), idempotency_key(req),
                                       ctx.request_id)
        respond_operation(req, resp, op, created)


class Owner:
    policies = {"PATCH": policy.ADMIN}

    def on_patch(self, req, resp, container_id):
        ctx = req.context
        body = read_json(req)
        if set(body) != {"owner_id"} or not isinstance(body["owner_id"], str):
            raise Invalid("Body must be {\"owner_id\": \"<uuid>\"}.", fields={"owner_id": "required"})
        users_svc.transfer_owner(ctx.db, actor_id=ctx.principal.user_id, container_id=container_id,
                                 new_owner_id=body["owner_id"], request_id=ctx.request_id)
        resp.media = svc.detail(ctx.db, ctx.cfg, ctx.principal,
                                policy.container_for(ctx.db, ctx.principal.user_id, container_id, "manage"))


class Assignment:
    policies = {"PUT": policy.ADMIN, "DELETE": policy.ADMIN}

    def on_put(self, req, resp, container_id, user_id):
        ctx = req.context
        users_svc.grant_access(ctx.db, actor_id=ctx.principal.user_id, container_id=container_id, user_id=user_id,
                               request_id=ctx.request_id)
        resp.status = falcon.HTTP_204

    def on_delete(self, req, resp, container_id, user_id):
        ctx = req.context
        users_svc.remove_access(ctx.db, actor_id=ctx.principal.user_id, container_id=container_id, user_id=user_id,
                                request_id=ctx.request_id)
        resp.status = falcon.HTTP_204


class Exec:
    policies = {"POST": policy.CONTAINER_EXEC}

    def on_post(self, req, resp, container_id):
        ctx = req.context
        op, created = svc.submit_exec(ctx.db, ctx.cfg, ctx.principal, ctx.container, read_json(req),
                                      idempotency_key(req), ctx.request_id)
        respond_operation(req, resp, op, created)


def _time_range(req, cfg) -> tuple[int, int]:
    now = int(time.time())
    rng = req.get_param("range")
    if rng:
        if rng not in RANGES:
            raise Invalid("range must be one of " + ", ".join(RANGES), fields={"range": "invalid"})
        return now - RANGES[rng], now
    start, end = req.get_param_as_int("start"), req.get_param_as_int("end")
    if start is None or end is None or not 0 < start < end <= now + 60:
        raise Invalid("Provide range, or start < end as epoch seconds.", fields={"start": "invalid"})
    if end - start > cfg.history_max_range_days * 86400:
        raise Invalid(f"Ranges are limited to {cfg.history_max_range_days} days.", fields={"start": "too long"})
    return start, end


class History:
    policies = {"GET": policy.CONTAINER_VIEW}

    def on_get(self, req, resp, container_id):
        ctx = req.context
        start, end = _time_range(req, ctx.cfg)
        # Falcon does not split "a,b" query values by default; split explicitly.
        raw = req.get_param("metrics") or "cpu_pct,memory_bytes"
        metrics = [m.strip() for m in raw.split(",") if m.strip()]
        if not metrics or len(metrics) > len(HISTORY_METRICS) or set(metrics) - set(HISTORY_METRICS):
            raise Invalid("Unknown metric.", fields={"metrics": "allowed: " + ", ".join(HISTORY_METRICS)})
        max_points = req.get_param_as_int("max_points", min_value=10, max_value=ctx.cfg.history_max_points) or 300
        from .. import history_client  # owned by the collector lane; imported lazily
        resp.media = history_client.query(ctx.cfg, {
            "v": 1, "type": "history", "user_id": ctx.principal.user_id, "container_id": container_id,
            "start": start, "end": end, "metrics": sorted(set(metrics)), "max_points": max_points})


class Usage:
    policies = {"GET": policy.CONTAINER_VIEW}

    def on_get(self, req, resp, container_id):
        ctx = req.context
        start, end = _time_range(req, ctx.cfg)
        from .. import history_client
        resp.media = history_client.query(ctx.cfg, {
            "v": 1, "type": "usage", "user_id": ctx.principal.user_id, "container_id": container_id,
            "start": start, "end": end})


class CreationOptions:
    policies = {"GET": policy.ADMIN}

    def on_get(self, req, resp):
        ctx = req.context
        owner_id = req.get_param("owner_id") or ctx.principal.user_id
        resp.media = svc.creation_options(ctx.db, ctx.cfg, owner_id)
