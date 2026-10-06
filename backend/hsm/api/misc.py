"""Identity, health, operations, accounting and audit resources."""
from __future__ import annotations

from ..auth import policy
from ..services import accounting, audit, containers, operations, quotas, state
from ..timeutil import parse, utcnow


class Live:
    policies = {"GET": policy.PUBLIC}

    def on_get(self, req, resp):
        resp.media = {"status": "ok"}


class Me:
    policies = {"GET": policy.AUTHENTICATED}

    def on_get(self, req, resp):
        p = req.context.principal
        resp.media = {"user": {"id": p.user_id, "email": p.email, "display_name": p.display_name, "role": p.role},
                      "csrf_token": p.csrf_token, "quota": quotas.owner_summary(req.context.db, p.user_id)}


class Health:
    policies = {"GET": policy.AUTHENTICATED}

    def on_get(self, req, resp):
        ctx = req.context
        out = {"collector": containers.collector_status(ctx.db, ctx.cfg)}
        worker, _ = state.get_with_time(ctx.db, state.WORKER_HEARTBEAT)
        age = int((utcnow() - parse(worker["at"])).total_seconds()) if worker and worker.get("at") else None
        out["worker"] = {"age_seconds": age, "stale": age is None or age > 30}
        if ctx.principal.is_admin:
            out["collector_detail"] = state.get(ctx.db, state.COLLECTOR_HEARTBEAT)
            out["metrics_storage"] = state.get(ctx.db, state.METRICS_STORAGE)
            out["queue"] = {r["state"]: r["n"] for r in ctx.db.execute(
                "SELECT state, COUNT(*) AS n FROM operations WHERE state IN ('queued','running','reconciling')"
                " GROUP BY state")}
        resp.media = out


class Operation:
    policies = {"GET": policy.OPERATION_VIEW}

    def on_get(self, req, resp, operation_id):
        ctx = req.context
        resp.media = {"operation": operations.serialize(ctx.operation, include_result=ctx.may_see_result)}


class Accounting:
    policies = {"GET": policy.ADMIN}

    def on_get(self, req, resp):
        resp.media = accounting.overview(req.context.db, req.context.cfg)


class AuditEvents:
    policies = {"GET": policy.ADMIN}

    def on_get(self, req, resp):
        limit = req.get_param_as_int("limit", min_value=1, max_value=200) or 50
        before = req.get_param_as_int("before_id", min_value=1)
        events = audit.page(req.context.db, before_id=before, limit=limit)
        resp.media = {"events": events, "next_before_id": events[-1]["id"] if len(events) == limit else None}
