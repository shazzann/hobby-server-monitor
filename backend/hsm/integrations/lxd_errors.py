"""Outcome-oriented LXD errors.

The worker does not care *why* pylxd failed; it cares whether the change may
have been applied, because that decides whether a quota reservation can be
released:

- ``LxdRejected``   LXD answered with an error (or the adapter refused before
                    sending anything). Confirmed NOT applied -> release.
- ``LxdUncertain``  timeout / dropped connection after the request may have
                    been sent. Unknown -> keep the reservation and reconcile.
- ``LxdUnavailable`` (an ``LxdUncertain``) the socket could not be reached, so
                    the request was never sent. Callers that only performed
                    reads may treat it as "nothing happened".
"""
from __future__ import annotations


class LxdError(Exception):
    code = "LXD_ERROR"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class LxdRejected(LxdError):
    code = "LXD_REJECTED"


class LxdNotFound(LxdRejected):
    code = "INSTANCE_MISSING"


class AdapterRefused(LxdRejected):
    """Refused by the adapter's own validation; LXD was never called."""
    code = "INVALID_REQUEST"


class LxdUncertain(LxdError):
    code = "LXD_UNCERTAIN"


class LxdUnavailable(LxdUncertain):
    code = "LXD_UNAVAILABLE"


class AmbiguousIdentity(LxdError):
    """More than one instance carries the same user.hsm.id marker (a copy or
    clone). Quarantine-worthy; never pick one by name."""
    code = "IDENTITY_AMBIGUOUS"
