"""Small primitives for secrets. Only hashes of bearer secrets are stored."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid


def new_secret() -> str:
    """256 bits of randomness, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_id() -> str:
    return str(uuid.uuid4())


def is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def hmac_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def canonical_email(email: str) -> str:
    """Trim and lower-case only. No Gmail dot/plus folding: that would invent equivalence."""
    return (email or "").strip().lower()
