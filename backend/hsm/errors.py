"""Application errors rendered as one JSON envelope.

{"error": {"code": "...", "message": "...", "fields": {...}, "request_id": "..."}}
"""
from __future__ import annotations


class HSMError(Exception):
    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 fields: dict | None = None, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.fields = fields or {}
        self.extra = extra or {}


class BadRequest(HSMError):
    status, code = 400, "BAD_REQUEST"


class Unauthenticated(HSMError):
    status, code = 401, "UNAUTHENTICATED"


class Forbidden(HSMError):
    status, code = 403, "FORBIDDEN"


class NotFound(HSMError):
    status, code = 404, "NOT_FOUND"


class Conflict(HSMError):
    status, code = 409, "CONFLICT"


class Invalid(HSMError):
    status, code = 422, "VALIDATION_FAILED"


class TooMany(HSMError):
    status, code = 429, "RATE_LIMITED"


class Unavailable(HSMError):
    status, code = 503, "UNAVAILABLE"
