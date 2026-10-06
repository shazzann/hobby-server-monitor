"""Fixed-window counters in SQLite for the few public endpoints."""
from __future__ import annotations

import sqlite3

from ..db import write_tx
from ..errors import TooMany
from ..timeutil import iso, parse, utcnow


def check(conn: sqlite3.Connection, bucket: str, limit: int, window_seconds: int) -> None:
    now = utcnow()
    with write_tx(conn):
        row = conn.execute("SELECT window_start, count FROM rate_limits WHERE bucket = ?", (bucket,)).fetchone()
        if row is None or (now - parse(row["window_start"])).total_seconds() >= window_seconds:
            conn.execute("INSERT INTO rate_limits(bucket, window_start, count) VALUES (?, ?, 1)"
                         " ON CONFLICT(bucket) DO UPDATE SET window_start = excluded.window_start, count = 1",
                         (bucket, iso(now)))
            return
        if row["count"] >= limit:
            raise TooMany("Too many attempts; wait a few minutes and try again.")
        conn.execute("UPDATE rate_limits SET count = count + 1 WHERE bucket = ?", (bucket,))
