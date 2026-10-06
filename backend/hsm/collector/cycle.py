"""One collection cycle, independent of scheduling so tests can drive it.

    bulk LXD read -> inventory reconcile -> normalize -> latest_metrics upsert
    -> enqueue raw points to the TinyFlux store -> heartbeat

Transactions: inventory reconciliation is one short BEGIN IMMEDIATE; the
latest_metrics upsert and heartbeat are a second one. No transaction is ever
open while LXD is being called.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from ..db import write_tx
from ..services import state
from ..timeutil import iso
from . import inventory
from .normalize import Normalizer
from .source import Source, SourceError

log = logging.getLogger("hsm.collector")

CAPABILITIES_EVERY = 60.0

# Insert only while the container row is still live: a worker may tombstone it
# between our reconcile and this write.
UPSERT_SQL = (
    "INSERT INTO latest_metrics(container_id, state, cpu_pct, memory_bytes, memory_limit_bytes, disk_bytes,"
    " disk_limit_bytes, rx_bytes, tx_bytes, rx_rate, tx_rate, processes, ipv4, started_at, sampled_at, quality)"
    " SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS"
    " (SELECT 1 FROM containers c WHERE c.id = ?1 AND c.status != 'deleted')"
    " ON CONFLICT(container_id) DO UPDATE SET state = excluded.state, cpu_pct = excluded.cpu_pct,"
    " memory_bytes = excluded.memory_bytes, memory_limit_bytes = excluded.memory_limit_bytes,"
    " disk_bytes = excluded.disk_bytes, disk_limit_bytes = excluded.disk_limit_bytes,"
    " rx_bytes = excluded.rx_bytes, tx_bytes = excluded.tx_bytes, rx_rate = excluded.rx_rate,"
    " tx_rate = excluded.tx_rate, processes = excluded.processes, ipv4 = excluded.ipv4,"
    " started_at = excluded.started_at, sampled_at = excluded.sampled_at, quality = excluded.quality"
)


class RateLimitedLog:
    """Log an identical message at most once per ``every`` seconds (LXD outages
    would otherwise log every 10 s)."""

    def __init__(self, logger, every: float = 60.0):
        self.logger, self.every, self._last = logger, every, {}

    def warning(self, msg: str, *args) -> None:
        key = msg % args if args else msg
        now = time.monotonic()
        last = self._last.get(key)
        if last is None or now - last >= self.every:
            self._last[key] = now
            self.logger.warning(msg, *args)


class Collector:
    def __init__(self, cfg, conn, source: Source, store=None):
        self.cfg = cfg
        self.conn = conn
        self.source = source
        self.store = store                    # StoreThread or None
        self.normalizer = Normalizer(cfg.collect_interval_seconds)
        self.missed_cycles = 0
        self.last_success_at: str | None = None
        self.host_cpus: int | None = None
        self._caps_at: float | None = None
        self._reported: set = set()
        self._rlog = RateLimitedLog(log)

    def _refresh_capabilities(self, mono: float) -> None:
        if self._caps_at is not None and mono - self._caps_at < CAPABILITIES_EVERY:
            return
        self._caps_at = mono
        try:
            caps = self.source.capabilities(self.cfg)
        except SourceError as exc:
            self._rlog.warning("capability refresh failed: %s", exc)
            return
        self.host_cpus = (caps.get("host") or {}).get("cpu_count") or self.host_cpus
        with write_tx(self.conn):
            state.put(self.conn, state.LXD_CAPABILITIES, caps)

    def _heartbeat(self, *, cycle_at: str, started: float, lxd_available: bool, error: str | None,
                   containers: int) -> dict:
        hb = {"last_cycle_at": cycle_at, "last_success_at": self.last_success_at,
              "cycle_ms": int((time.monotonic() - started) * 1000), "missed_cycles": self.missed_cycles,
              "lxd_available": lxd_available, "error": error, "containers": containers}
        return hb

    def run_cycle(self, *, wall: datetime | None = None, mono: float | None = None) -> dict:
        started = time.monotonic()
        try:
            instances = self.source.list_instances()
        except SourceError as exc:
            # LXD down: keep the process and the history, tombstone nothing,
            # leave latest_metrics to age into "stale".
            self._rlog.warning("LXD unavailable: %s", exc)
            now = wall or datetime.now(timezone.utc)
            hb = self._heartbeat(cycle_at=iso(now), started=started, lxd_available=False,
                                 error="LXD unavailable", containers=0)
            with write_tx(self.conn):
                state.put(self.conn, state.COLLECTOR_HEARTBEAT, hb)
            return hb

        mono = time.monotonic() if mono is None else mono
        wall = (wall or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        now_iso = iso(wall)
        self._refresh_capabilities(mono)
        host_cpus = self.host_cpus or os.cpu_count()

        result = inventory.reconcile(self.conn, self.cfg, instances, now_iso=now_iso, reported=self._reported)

        samples, errors = [], 0
        for idx, cid in result.attached:
            try:
                samples.append(self.normalizer.sample(cid, instances[idx], mono=mono, wall=wall,
                                                      host_cpus=host_cpus))
            except Exception as exc:
                # One malformed instance must not break the others.
                errors += 1
                self._rlog.warning("could not normalize %s: %s: %s", cid, exc.__class__.__name__, exc)
        self.normalizer.retain(cid for _i, cid in result.attached)

        self.last_success_at = now_iso
        hb = self._heartbeat(cycle_at=now_iso, started=started, lxd_available=True,
                             error=f"{errors} container(s) unreadable" if errors else None,
                             containers=len(samples))
        with write_tx(self.conn):
            self.conn.executemany(UPSERT_SQL, [s.latest_row() for s in samples])
            state.put(self.conn, state.COLLECTOR_HEARTBEAT, hb)

        if self.store is not None and samples:
            t = int(wall.timestamp())
            self.store.submit_ingest([(s.container_id, t, s.raw_fields()) for s in samples])
        return hb
