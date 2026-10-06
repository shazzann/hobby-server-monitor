"""SQLite access: connections, migrations and short write transactions.

Rules followed everywhere:
- foreign keys on, WAL journal, busy timeout on every connection;
- conflicting writes use BEGIN IMMEDIATE and stay short;
- no transaction is ever held across an LXD or Google call.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

from .timeutil import utcnow_iso

BUSY_TIMEOUT_MS = 5000


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migrations() -> list[tuple[int, str]]:
    files = resources.files("hsm").joinpath("migrations")
    out = []
    for entry in files.iterdir():
        if entry.name.endswith(".sql"):
            out.append((int(entry.name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
    return sorted(out)


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, sql in _migrations():
        if version in done:
            continue
        # executescript commits implicitly, so wrap the script and the version row together.
        conn.executescript(
            "BEGIN IMMEDIATE;\n" + sql +
            f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{utcnow_iso()}');\nCOMMIT;"
        )
        applied.append(version)
    return applied


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return row[0] or 0
