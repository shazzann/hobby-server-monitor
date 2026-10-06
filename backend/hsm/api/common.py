"""Request helpers shared by the thin API resources."""
from __future__ import annotations

import json

import falcon

from ..errors import BadRequest
from ..services import operations

MAX_BODY_BYTES = 16 * 1024


def read_json(req) -> dict:
    length = req.content_length or 0
    if length > MAX_BODY_BYTES:
        raise BadRequest("Request body is too large.", code="TOO_LARGE")
    ctype = (req.content_type or "").split(";")[0].strip()
    if ctype != "application/json":
        raise BadRequest("Content-Type must be application/json.", code="UNSUPPORTED_MEDIA_TYPE")
    raw = req.bounded_stream.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise BadRequest("Request body is too large.", code="TOO_LARGE")
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("Request body is not valid JSON.", code="INVALID_JSON")
    if not isinstance(body, dict):
        raise BadRequest("Request body must be a JSON object.", code="INVALID_JSON")
    return body


def idempotency_key(req) -> str:
    return operations.validate_idempotency_key(req.get_header("Idempotency-Key"))


def respond_operation(req, resp, op, created: bool, **extra) -> None:
    resp.status = falcon.HTTP_202 if created else falcon.HTTP_200
    resp.media = {"operation": operations.serialize(op, include_result=op["actor_id"] == req.context.principal.user_id),
                  **extra}


def client_ip(req) -> str:
    # The socket peer only. X-Forwarded-For is client-controlled unless a trusted proxy
    # rewrites it, so it must not key a rate limit. Behind the local proxy every client
    # shares one bucket, which errs on the strict side.
    return req.remote_addr or "unknown"
