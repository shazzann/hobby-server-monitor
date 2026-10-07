"""UTC timestamp helpers. Stored timestamps are 'YYYY-MM-DDTHH:MM:SSZ' text."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_FRACTION = re.compile(r"(?<=:\d\d)(\.\d+)")

FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def utcnow_iso() -> str:
    return iso(utcnow())


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # LXD reports nanoseconds; Python 3.10's fromisoformat accepts exactly 3 or 6 fraction
    # digits, so normalise any fraction to 6 (truncate or zero-pad).
    v = _FRACTION.sub(lambda m: "." + m.group(1)[1:7].ljust(6, "0"), v)
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def in_future(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))
