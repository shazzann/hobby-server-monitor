"""``python -m hsm.collector`` - the independent metrics collector process.

Schedule: monotonic, fixed period (COLLECTOR_POLL_INTERVAL_SECONDS). Cycles
never overlap (single thread) and there is no burst catch-up: if a cycle
overruns, the missed slots are counted and skipped, and the next cycle runs on
the next aligned slot.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from .. import config, db
from .cycle import Collector
from .source import LXDSource
from .store import MetricsStore, StoreThread

log = logging.getLogger("hsm.collector")


def acquire_singleton(cfg):
    """Hold an exclusive flock on <data_dir>/collector.lock for the process lifetime."""
    try:
        import fcntl
    except ImportError:  # Windows dev environment: systemd provides the guarantee in production
        log.warning("fcntl unavailable; singleton lock not enforced")
        return None
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    fh = open(cfg.data_dir / "collector.lock", "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit("another collector instance holds collector.lock")
    return fh


def next_slot(next_at: float, now: float, interval: float) -> tuple[float, int]:
    """Advance the schedule. Returns (next deadline, slots missed)."""
    next_at += interval
    if now <= next_at:
        return next_at, 0
    missed = int((now - next_at) // interval) + 1
    return next_at + missed * interval, missed


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("HSM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    lock = acquire_singleton(cfg)
    conn = db.connect(cfg.sqlite_path)
    if db.schema_version(conn) == 0:
        log.error("database is not migrated; run the migration step first")
        return 2

    store = MetricsStore(cfg)
    store.open()
    store_thread = StoreThread(store)
    store_thread.start()

    server = None
    try:
        from .history_server import HistoryServer
        server = HistoryServer(cfg, store_thread)
        server.start()
    except (RuntimeError, OSError) as exc:
        log.error("history socket not started: %s", exc)
        server = None

    stop = threading.Event()

    def _on_signal(signum, _frame):
        log.info("signal %s received; stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    collector = Collector(cfg, conn, LXDSource(cfg), store_thread)
    interval = float(cfg.collect_interval_seconds)
    next_at = time.monotonic()
    log.info("collector started (interval %ss)", interval)
    while not stop.is_set():
        try:
            collector.run_cycle()
        except Exception:
            # e.g. SQLite busy beyond its timeout: skip this cycle, keep running.
            log.exception("collection cycle failed")
        next_at, missed = next_slot(next_at, time.monotonic(), interval)
        if missed:
            collector.missed_cycles += missed
            log.warning("cycle overran; skipped %d slot(s)", missed)
        stop.wait(max(0.0, next_at - time.monotonic()))

    # Graceful shutdown: stop taking history requests, flush samples, close shards.
    if server is not None:
        server.stop()
    store_thread.stop()
    conn.close()
    if lock is not None:
        lock.close()
    log.info("collector stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
