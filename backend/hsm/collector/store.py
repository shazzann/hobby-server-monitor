"""The single TinyFlux owner: raw shards, 5-minute rollups, retention, queries.

Only this module opens TinyFlux files, and only from one thread
(``StoreThread``); everything else talks to it through bounded queues.
Sample ingestion has priority over history requests.

Layout (docs/api-contract.md "TinyFlux layout"):
    <metrics_dir>/raw/YYYYMMDDHH.csv     measurement c_raw_v1, tag cid, hourly UTC
    <metrics_dir>/rollup/YYYYMMDD.csv    measurement c_5m_v1,  tag cid, daily UTC

Verified TinyFlux 1.2.0 behaviour (site-packages/tinyflux):
* Field values must be int/float/None (bool rejected); every value is written
  as ``str(float(v))`` and read back as float, None as ``_none``.
* ``insert`` calls ``time.astimezone(utc)``: a *naive* datetime would be read
  as local time, so we always pass tz-aware UTC datetimes.
* CSVStorage appends with flush + fsync per ``insert_multiple`` batch.
* ``remove()`` rewrites through a NamedTemporaryFile in the system temp dir and
  ``shutil.copy`` - not atomic - so crash-sensitive rewrites here use our own
  temp file in the same directory + fsync + ``os.replace``.
* With ``auto_index=False`` no in-memory index (which holds every field value)
  is built; searches stream the shard. Shards are small (hour / day), so this
  keeps memory flat at the cost of a linear scan of the shards in range.

Rollup durability ordering (why no bucket is ever counted twice):
    1. append the completed buckets to the daily rollup shard (fsync)
    2. then persist service_state['metrics.rollup_checkpoint'] = end of those buckets
  A crash between 1 and 2 leaves rollup points at/after the stored checkpoint;
  ``open()`` deletes them (atomic rewrite) and they are recomputed from raw.
  Raw shards are deleted only once their whole hour is below the checkpoint.
  Reads additionally ignore stored buckets >= checkpoint and dedupe (cid, time).
"""
from __future__ import annotations

import csv
import io
import logging
import math
import os
import queue
import shutil
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from tinyflux import Point, TagQuery, TimeQuery, TinyFlux

from .. import db
from ..services import state
from ..timeutil import utcnow_iso

log = logging.getLogger("hsm.collector.store")

RAW_M = "c_raw_v1"
ROLL_M = "c_5m_v1"
RAW_FIELDS = ("state", "cpu_ns", "cpu_pct", "cores", "mem", "mem_lim", "disk", "disk_lim", "rx", "tx",
              "rx_rate", "tx_rate", "procs", "dt", "cpu_d_ns", "rx_d", "tx_d")
ROLL_FIELDS = ("covered_s", "cpu_s", "cpu_alloc_s", "mem_bs", "mem_max", "disk_bs", "disk_max", "rx_d", "tx_d",
               "procs_max", "samples")
BUCKET = 300
HOUR = 3600
DAY = 86400
GIB = 1024 ** 3
RAW_MAX_SPAN = 2 * HOUR + 60          # ranges up to ~2 h are answered from raw samples
MAX_POINTS = 600
HANDLE_CAP = 4                        # open TinyFlux handles kept (current shards + recent reads)
CORRUPT_CAP_BYTES = 16 * 1024 * 1024  # quarantined shard copies are byte-capped
MAX_ROLLUP_CHUNKS = 24                # hours of raw rolled up per maintenance pass

HISTORY_METRICS = {
    # metric name -> (raw field, unit)
    "cpu_pct": ("cpu_pct", "%"),
    "memory_bytes": ("mem", "bytes"),
    "disk_bytes": ("disk", "bytes"),
    "rx_rate": ("rx_rate", "bytes/s"),
    "tx_rate": ("tx_rate", "bytes/s"),
    "processes": ("procs", "count"),
}


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _floor(t: float, step: int) -> int:
    return int(t // step * step)


# --------------------------------------------------------------------------- pure helpers

def compute_rollups(points) -> dict:
    """Aggregate raw points [(cid, t, fields)] into {(cid, bucket_start): rollup fields}.

    Deterministic: a point belongs to the bucket containing its timestamp t, and
    represents the interval (t - dt, t]. Sums are time-weighted so averages are
    derived later as sum / covered seconds (never an average of averages).
    """
    out: dict = {}
    for cid, t, f in points:
        b = _floor(t, BUCKET)
        r = out.get((cid, b))
        if r is None:
            r = out[(cid, b)] = {"covered_s": 0.0, "cpu_s": 0.0, "cpu_alloc_s": 0.0, "mem_bs": 0.0,
                                 "mem_max": None, "disk_bs": 0.0, "disk_max": None, "rx_d": None,
                                 "tx_d": None, "procs_max": None, "samples": 0}
        r["samples"] += 1
        dt = f.get("dt")
        mem, disk = f.get("mem"), f.get("disk")
        if dt is not None and dt > 0:
            r["covered_s"] += dt
            if mem is not None:
                r["mem_bs"] += mem * dt
            if disk is not None:
                r["disk_bs"] += disk * dt
            cpu_d, cores = f.get("cpu_d_ns"), f.get("cores")
            if cpu_d is not None and cores:
                r["cpu_s"] += cpu_d / 1e9
                r["cpu_alloc_s"] += cores * dt
            for k in ("rx_d", "tx_d"):
                if f.get(k) is not None:
                    r[k] = (r[k] or 0) + f[k]
        for k, v in (("mem_max", mem), ("disk_max", disk), ("procs_max", f.get("procs"))):
            if v is not None and (r[k] is None or v > r[k]):
                r[k] = v
    return out


def plan_buckets(start: int, end: int, max_points: int, base: int) -> tuple[int, int, int]:
    """Choose an epoch-aligned bucket size (multiple of ``base``) so that the
    buckets covering [start, end) number at most ``max_points``.
    Returns (resolution_seconds, first_bucket_start, bucket_count)."""
    max_points = max(1, min(int(max_points), MAX_POINTS))
    span = max(1, end - start)
    res = max(base, math.ceil(span / max_points / base) * base)
    while True:
        first = _floor(start, res)
        n = math.ceil((end - first) / res)
        if n <= max_points:
            return res, first, max(n, 1)
        res += base


def merge_intervals(intervals, tolerance: float = 0.0) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1] + tolerance:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def gaps_between(covered, start: int, end: int, tolerance: float) -> list[list[int]]:
    """Uncovered spans of [start, end) longer than ``tolerance`` seconds."""
    gaps, cursor = [], start
    for s, e in merge_intervals(covered, tolerance):
        if s - cursor > tolerance:
            gaps.append([int(cursor), int(math.ceil(s))])
        cursor = max(cursor, e)
    if end - cursor > tolerance:
        gaps.append([int(cursor), int(end)])
    return gaps


def _covered_len(intervals) -> float:
    return sum(e - s for s, e in merge_intervals(intervals))


def _round(v, nd=3):
    return None if v is None else round(v, nd)


def _valid_row(row: list[str]) -> bool:
    """Structural check of one CSV row as this module writes it."""
    if len(row) < 4:
        return False
    fields = RAW_FIELDS if row[1] == RAW_M else ROLL_FIELDS if row[1] == ROLL_M else None
    if fields is None or len(row) != 4 + 2 * len(fields) or row[2] not in ("_tag_cid", "t_cid") or not row[3]:
        return False
    try:
        datetime.fromisoformat(row[0])
        for i, name in enumerate(fields):
            k, v = row[4 + 2 * i], row[5 + 2 * i]
            if k not in ("f_" + name, "_field_" + name):
                return False
            if v != "_none":
                float(v)
    except ValueError:
        return False
    return True


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- store core

class MetricsStore:
    """Synchronous core. Not thread-safe: use it from one thread only (StoreThread)."""

    def __init__(self, cfg, *, sqlite_path=None):
        self.cfg = cfg
        self.root = Path(cfg.metrics_dir)
        self.raw_dir = self.root / "raw"
        self.roll_dir = self.root / "rollup"
        self.interval = int(cfg.collect_interval_seconds)
        self.grace = max(30, 3 * self.interval)   # let the last cycle of a bucket land before rolling it up
        self._sqlite_path = sqlite_path or cfg.sqlite_path
        self._conn = None
        self._handles: "OrderedDict[Path, TinyFlux]" = OrderedDict()
        self._tail_checked: set[Path] = set()
        self.checkpoint: int | None = None
        self._needs_trim = False
        self.generation = 0
        self.removed_shards = 0
        self.quarantined: list[dict] = []
        self.dropped_ingest = 0

    # ---- lifecycle
    def _db(self):
        if self._conn is None:
            self._conn = db.connect(self._sqlite_path)
        return self._conn

    def open(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.roll_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.raw_dir, self.roll_dir):
            for tmp in d.glob("*.tmp"):
                tmp.unlink()        # an interrupted atomic rewrite; the original is intact
        cp = state.get(self._db(), state.ROLLUP_CHECKPOINT)
        if isinstance(cp, dict) and isinstance(cp.get("until"), int):
            self.checkpoint = cp["until"]
            removed = self._trim_rollups_from(self.checkpoint)
            if removed:
                log.warning("recovery: removed %d rollup points at/after checkpoint %d", removed, self.checkpoint)

    def close(self) -> None:
        for h in self._handles.values():
            try:
                h.close()
            except Exception:  # pragma: no cover - closing on shutdown
                pass
        self._handles.clear()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- shard files
    def raw_path(self, hour: int) -> Path:
        return self.raw_dir / (_dt(hour).strftime("%Y%m%d%H") + ".csv")

    def roll_path(self, day: int) -> Path:
        return self.roll_dir / (_dt(day).strftime("%Y%m%d") + ".csv")

    def shards(self, kind: str) -> list[tuple[int, Path]]:
        d, fmt = (self.raw_dir, "%Y%m%d%H") if kind == "raw" else (self.roll_dir, "%Y%m%d")
        out = []
        if d.is_dir():
            for p in d.glob("*.csv"):
                try:
                    out.append((_epoch(datetime.strptime(p.stem, fmt).replace(tzinfo=timezone.utc)), p))
                except ValueError:
                    continue
        return sorted(out)

    def _corrupt_files(self) -> list[Path]:
        files = [p for d in (self.raw_dir, self.roll_dir) if d.is_dir() for p in d.glob("*.corrupt")]
        return sorted(files, key=lambda p: p.stat().st_mtime)

    def _handle(self, path: Path) -> TinyFlux:
        h = self._handles.pop(path, None)
        if h is None:
            if path not in self._tail_checked:
                self._tail_checked.add(path)
                self._check_tail(path)
            h = TinyFlux(str(path), auto_index=False, create_dirs=True)
        self._handles[path] = h
        while len(self._handles) > HANDLE_CAP:
            _, old = self._handles.popitem(last=False)   # evict least recently used shard
            old.close()
        return h

    def _evict(self, path: Path) -> None:
        h = self._handles.pop(path, None)
        if h is not None:
            h.close()

    def _delete(self, path: Path) -> None:
        self._evict(path)
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        self._tail_checked.discard(path)
        self.removed_shards += 1
        self.generation += 1

    def _atomic_write(self, path: Path, rows: list[list[str]]) -> None:
        self._evict(path)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    def _check_tail(self, path: Path) -> None:
        """A crash mid-append leaves an unterminated last row; the next append
        would glue onto it. Repair before the shard is opened again."""
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size == 0:
            return
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
        if last != b"\n":
            self._salvage(path, "unterminated final record")

    def _salvage(self, path: Path, reason: str) -> None:
        """Quarantine a damaged shard: keep a byte-capped copy as *.corrupt,
        rewrite the shard with only its structurally valid rows."""
        self._evict(path)
        data = path.read_bytes()
        good, dropped = [], 0
        for line in data.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = next(csv.reader(io.StringIO(line)))
            except (csv.Error, StopIteration):
                dropped += 1
                continue
            if _valid_row(row):
                good.append(row)
            else:
                dropped += 1
        corrupt = path.with_name(f"{path.name}.{int(time.time())}.corrupt")
        existing = self._corrupt_files()
        total = sum(p.stat().st_size for p in existing)
        while existing and total + len(data) > CORRUPT_CAP_BYTES:
            old = existing.pop(0)
            total -= old.stat().st_size
            old.unlink()
        if len(data) <= CORRUPT_CAP_BYTES:
            corrupt.write_bytes(data)
        self._atomic_write(path, good)
        kind = "raw" if path.parent == self.raw_dir else "rollup"
        start = next((s for s, p in self.shards(kind) if p == path), None)
        span = HOUR if kind == "raw" else DAY
        entry = {"shard": f"{kind}/{path.name}", "from": start, "to": None if start is None else start + span,
                 "reason": reason, "dropped_rows": dropped, "at": utcnow_iso()}
        self.quarantined = (self.quarantined + [entry])[-20:]
        self.generation += 1
        log.warning("quarantined damaged shard %s (%s): kept %d rows, dropped %d", entry["shard"], reason,
                    len(good), dropped)

    def _search(self, path: Path, measurement: str, t0: int, t1: int, cid: str | None = None) -> list:
        """Points of one shard in [t0, t1) as (cid, epoch, fields)."""
        if not path.exists():
            return []
        q = (TimeQuery() >= _dt(t0)) & (TimeQuery() < _dt(t1))
        if cid is not None:
            q = q & (TagQuery().cid == cid)
        for attempt in (0, 1):
            try:
                pts = self._handle(path).search(q, measurement=measurement)
                return [(p.tags.get("cid"), _epoch(p.time), p.fields) for p in pts]
            except (ValueError, IndexError, TypeError, KeyError, csv.Error):
                if attempt:
                    raise
                self._salvage(path, "unparsable record")
        return []  # pragma: no cover

    # ---- ingestion
    def ingest(self, items: list[tuple[str, int, dict]]) -> int:
        """Append raw samples [(cid, epoch_seconds, fields)] to their hourly shards."""
        by_hour: dict[int, list[Point]] = {}
        for cid, t, fields in items:
            clean = {}
            for k in RAW_FIELDS:
                v = fields.get(k)
                clean[k] = None if (v is None or isinstance(v, bool)) else v
            by_hour.setdefault(_floor(t, HOUR), []).append(
                Point(time=_dt(t), measurement=RAW_M, tags={"cid": cid}, fields=clean))
        n = 0
        for hour in sorted(by_hour):
            pts = sorted(by_hour[hour], key=lambda p: p.time)
            n += self._handle(self.raw_path(hour)).insert_multiple(pts, compact_key_prefixes=True)
        if n:
            self.generation += 1
        return n

    # ---- rollups
    def _save_checkpoint(self, until: int) -> None:
        try:
            with db.write_tx(self._db()) as conn:
                state.put(conn, state.ROLLUP_CHECKPOINT, {"until": int(until)})
        except Exception:
            # Rollup points past the old checkpoint may now exist; remove them before
            # the next attempt so they are not appended twice.
            self._needs_trim = True
            raise
        self.checkpoint = int(until)

    def _trim_rollups_from(self, until: int) -> int:
        removed = 0
        for day, path in self.shards("rollup"):
            if day + DAY <= until:
                continue
            self._evict(path)
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                text = f.read()
            keep, dropped = [], 0
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    row = next(csv.reader(io.StringIO(line)))
                except (csv.Error, StopIteration):
                    dropped += 1
                    continue
                if _valid_row(row) and _epoch(datetime.fromisoformat(row[0])) < until:
                    keep.append(row)
                else:
                    dropped += 1
            if dropped:
                self._atomic_write(path, keep)
                removed += dropped
                self.generation += 1
        return removed

    def run_rollups(self, now: float) -> int:
        """Roll up every completed 5-minute bucket after the checkpoint. Returns buckets written."""
        limit = _floor(now - self.grace, BUCKET)
        if self._needs_trim and self.checkpoint is not None:
            self._trim_rollups_from(self.checkpoint)
            self._needs_trim = False
        if self.checkpoint is None:
            raws = self.shards("raw")
            self._save_checkpoint(min(_floor(raws[0][0], BUCKET), limit) if raws else limit)
        written, chunks = 0, 0
        c = self.checkpoint
        while c < limit and chunks < MAX_ROLLUP_CHUNKS:
            hour = _floor(c, HOUR)
            path = self.raw_path(hour)
            if not path.exists():
                later = [h for h, _ in self.shards("raw") if h > hour]
                c = min(limit, later[0]) if later else limit
                self._save_checkpoint(c)     # nothing written: ordering is irrelevant here
                continue
            chunk_end = min(hour + HOUR, limit)
            recs = compute_rollups(self._search(path, RAW_M, c, chunk_end))
            by_day: dict[int, list[Point]] = {}
            for (cid, b), r in sorted(recs.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                by_day.setdefault(_floor(b, DAY), []).append(
                    Point(time=_dt(b), measurement=ROLL_M, tags={"cid": cid}, fields=r))
            for day, pts in sorted(by_day.items()):
                written += self._handle(self.roll_path(day)).insert_multiple(pts, compact_key_prefixes=True)
            # Only now - the rollup shard is fsynced - may the checkpoint move.
            self._save_checkpoint(chunk_end)
            c = chunk_end
            chunks += 1
        if written:
            self.generation += 1
        return written

    # ---- retention
    def apply_retention(self, now: float) -> list[str]:
        removed: list[str] = []
        cp = self.checkpoint if self.checkpoint is not None else -1
        raw_cut = now - self.cfg.raw_retention_hours * HOUR
        for hour, path in self.shards("raw"):
            # Whole-shard deletion: a shard goes once its *end* is past retention and
            # every bucket in it has been rolled up durably.
            if hour + HOUR <= raw_cut and hour + HOUR <= cp:
                self._delete(path)
                removed.append("raw/" + path.name)
        roll_cut = now - self.cfg.rollup_retention_days * DAY
        for day, path in self.shards("rollup"):
            if day + DAY <= roll_cut:
                self._delete(path)
                removed.append("rollup/" + path.name)
        for p in self._corrupt_files():
            if p.stat().st_mtime <= roll_cut:
                p.unlink()
                removed.append(p.parent.name + "/" + p.name)

        # Byte budget and free-space reserve: drop oldest *completed* data first.
        total = self.total_bytes()
        free = shutil.disk_usage(self.root).free
        while total > self.cfg.metrics_max_bytes or free < self.cfg.metrics_min_free_bytes:
            cands = [(p.stat().st_mtime - 1e12, p) for p in self._corrupt_files()]
            cands += [(d, p) for d, p in self.shards("rollup") if d + DAY <= now]
            cands += [(h, p) for h, p in self.shards("raw") if h + HOUR <= min(now, cp)]
            if not cands:
                log.warning("metrics storage over budget (%d bytes, %d free) with nothing completed to delete",
                            total, free)
                break
            _, victim = min(cands, key=lambda c: c[0])
            size = victim.stat().st_size
            if victim.suffix == ".corrupt":
                victim.unlink()
            else:
                self._delete(victim)
            removed.append(victim.parent.name + "/" + victim.name)
            total -= size
            free += size
        if removed:
            log.info("retention removed %d files", len(removed))
        return removed

    def total_bytes(self) -> int:
        return sum(p.stat().st_size for d in (self.raw_dir, self.roll_dir) if d.is_dir()
                   for p in d.iterdir() if p.is_file())

    def storage_status(self) -> dict:
        raws, rolls = self.shards("raw"), self.shards("rollup")
        return {"bytes": self.total_bytes(), "raw_from": raws[0][0] if raws else None,
                "rollup_from": rolls[0][0] if rolls else None, "removed_shards": self.removed_shards,
                "quarantined": self.quarantined[-10:], "rollup_checkpoint": self.checkpoint,
                "dropped_ingest": self.dropped_ingest}

    def publish_status(self) -> None:
        with db.write_tx(self._db()) as conn:
            state.put(conn, state.METRICS_STORAGE, self.storage_status())

    def maintain(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.run_rollups(now)
        self.apply_retention(now)
        self.publish_status()

    # ---- reads
    def _raw_points(self, cid: str, t0: int, t1: int) -> list[tuple[int, dict]]:
        seen: dict[int, dict] = {}
        for hour in range(_floor(t0, HOUR), t1, HOUR):
            for _c, t, f in self._search(self.raw_path(hour), RAW_M, t0, t1, cid):
                seen[t] = f            # dedupe by (cid, time): last write wins
        return sorted(seen.items())

    def _rollups(self, cid: str, t0: int, t1: int) -> dict[int, dict]:
        """Rollup records {bucket_start: fields} for buckets starting in [t0, t1):
        durable rollups below the checkpoint plus buckets computed on the fly
        from raw samples for the not-yet-rolled tail."""
        cp = self.checkpoint if self.checkpoint is not None else t0
        out: dict[int, dict] = {}
        stored_end = min(t1, cp)
        if stored_end > t0:
            for day in range(_floor(t0, DAY), stored_end, DAY):
                for _c, b, f in self._search(self.roll_path(day), ROLL_M, t0, stored_end, cid):
                    out[b] = f
        tail_from = max(t0, cp)
        if tail_from < t1:
            pts = [(cid, t, f) for t, f in self._raw_points(cid, tail_from, t1)]
            for (_c, b), r in compute_rollups(pts).items():
                out[b] = r
        return out

    def available_from(self, source: str) -> int | None:
        raws = self.shards("raw")
        raw_from = raws[0][0] if raws else None
        if source == "raw":
            return raw_from
        rolls = self.shards("rollup")
        cands = [x for x in (rolls[0][0] if rolls else None, raw_from) if x is not None]
        return min(cands) if cands else None

    def history(self, cid: str, start: int, end: int, metrics: list[str], max_points: int) -> dict:
        span = end - start
        raw_from = self.available_from("raw")
        if span <= RAW_MAX_SPAN and raw_from is not None and start >= raw_from:
            return self._history_raw(cid, start, end, metrics, max_points)
        return self._history_rollup(cid, start, end, metrics, max_points)

    def _history_raw(self, cid, start, end, metrics, max_points) -> dict:
        res, first, n = plan_buckets(start, end, max_points, self.interval)
        acc = {m: [[0.0, 0, None] for _ in range(n)] for m in metrics}   # sum, count, max
        covered = []
        for t, f in self._raw_points(cid, start, end):
            i = (t - first) // res
            if not 0 <= i < n:
                continue
            for m in metrics:
                v = f.get(HISTORY_METRICS[m][0])
                if v is None:
                    continue
                a = acc[m][i]
                a[0] += v
                a[1] += 1
                a[2] = v if a[2] is None else max(a[2], v)
            dt = f.get("dt")
            if dt:
                covered.append((max(start, t - dt), min(end, t)))
        series = {}
        for m in metrics:
            pts = []
            for i, (s, c, mx) in enumerate(acc[m]):
                if c == 0:
                    val = None
                elif m == "processes":
                    val = int(mx)
                elif m in ("memory_bytes", "disk_bytes"):
                    val = int(round(s / c))
                else:
                    val = round(s / c, 3)
                pts.append([first + i * res, val])
            series[m] = {"unit": HISTORY_METRICS[m][1], "points": pts}
        return {"container_id": cid, "start": start, "end": end, "resolution_seconds": res, "source": "raw",
                "series": series, "coverage": round(min(1.0, _covered_len(covered) / max(1, end - start)), 4),
                "gaps": gaps_between(covered, start, end, 1.5 * self.interval),
                "available_from": self.available_from("raw")}

    def _history_rollup(self, cid, start, end, metrics, max_points) -> dict:
        res, first, n = plan_buckets(start, end, max_points, BUCKET)
        recs = self._rollups(cid, _floor(start, BUCKET), end)
        keys = ("covered_s", "cpu_s", "cpu_alloc_s", "mem_bs", "disk_bs", "rx_d", "tx_d")
        acc = [dict.fromkeys(keys + ("mem_seen", "disk_seen", "procs_max"), None) for _ in range(n)]
        covered_in_range, covered = 0.0, []
        for b, r in recs.items():
            i = (b - first) // res
            if not 0 <= i < n:
                continue
            a = acc[i]
            for k in keys:
                if r.get(k) is not None:
                    a[k] = (a[k] or 0) + r[k]
            a["mem_seen"] = a["mem_seen"] or r.get("mem_max") is not None
            a["disk_seen"] = a["disk_seen"] or r.get("disk_max") is not None
            if r.get("procs_max") is not None:
                a["procs_max"] = max(a["procs_max"] or 0, r["procs_max"])
            cov = r.get("covered_s") or 0
            if cov > 0:
                overlap = max(0, min(b + BUCKET, end) - max(b, start))
                covered_in_range += cov * overlap / BUCKET
                if overlap:
                    covered.append((max(b, start), min(b + BUCKET, end)))
        series = {}
        for m in metrics:
            pts = []
            for i, a in enumerate(acc):
                cov = a["covered_s"] or 0
                val = None
                if m == "cpu_pct" and a["cpu_alloc_s"]:
                    val = round(100.0 * a["cpu_s"] / a["cpu_alloc_s"], 3)
                elif m == "memory_bytes" and cov and a["mem_seen"]:
                    val = int(round(a["mem_bs"] / cov))
                elif m == "disk_bytes" and cov and a["disk_seen"]:
                    val = int(round(a["disk_bs"] / cov))
                elif m in ("rx_rate", "tx_rate") and cov and a[m[:2] + "_d"] is not None:
                    val = round(a[m[:2] + "_d"] / cov, 3)
                elif m == "processes" and a["procs_max"] is not None:
                    val = int(a["procs_max"])
                pts.append([first + i * res, val])
            series[m] = {"unit": HISTORY_METRICS[m][1], "points": pts}
        return {"container_id": cid, "start": start, "end": end, "resolution_seconds": res, "source": "rollup",
                "series": series, "coverage": round(min(1.0, covered_in_range / max(1, end - start)), 4),
                "gaps": gaps_between(covered, start, end, 0), "available_from": self.available_from("rollup")}

    def usage(self, cid: str, start: int, end: int) -> dict:
        """Consumption over [start, end): durable rollups (5-minute granularity at
        the edges) plus raw samples for the not-yet-rolled tail."""
        recs = self._rollups(cid, start, end)
        cov = sum((r.get("covered_s") or 0) for r in recs.values())
        out = {"start": start, "end": end, "covered_seconds": int(round(min(cov, end - start))),
               "coverage": round(min(1.0, cov / max(1, end - start)), 4), "cpu_core_hours": None,
               "memory_gib_hours": None, "disk_avg_bytes": None, "disk_max_bytes": None,
               "rx_bytes": None, "tx_bytes": None}
        if cov <= 0:
            out["coverage"] = 0.0
            return out
        vals = list(recs.values())
        disk_max = [r["disk_max"] for r in vals if r.get("disk_max") is not None]
        out.update({
            "cpu_core_hours": round(sum(r.get("cpu_s") or 0 for r in vals) / 3600, 6),
            "memory_gib_hours": round(sum(r.get("mem_bs") or 0 for r in vals) / (GIB * 3600), 6),
            "disk_avg_bytes": int(round(sum(r.get("disk_bs") or 0 for r in vals) / cov)) if disk_max else None,
            "disk_max_bytes": int(max(disk_max)) if disk_max else None,
            "rx_bytes": int(sum(r.get("rx_d") or 0 for r in vals)),
            "tx_bytes": int(sum(r.get("tx_d") or 0 for r in vals)),
        })
        return out


# --------------------------------------------------------------------------- thread wrapper

class StoreBusy(Exception):
    """The request queue is full."""


class StoreTimeout(Exception):
    """The request did not finish within its deadline."""


class StoreClosed(Exception):
    """The store is stopping."""


class _Request:
    __slots__ = ("fn", "done", "result", "error", "cancelled")

    def __init__(self, fn):
        self.fn, self.done, self.result, self.error, self.cancelled = fn, threading.Event(), None, None, False


class StoreThread(threading.Thread):
    """Owns a MetricsStore. Two bounded queues; ingestion is always drained first."""

    def __init__(self, store: MetricsStore, *, ingest_max: int = 32, request_max: int = 8,
                 maintain_every: float = 30.0):
        super().__init__(name="hsm-tinyflux", daemon=True)
        self.store = store
        self._ingest: queue.Queue = queue.Queue(maxsize=ingest_max)
        self._requests: queue.Queue = queue.Queue(maxsize=request_max)
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._maintain_every = maintain_every
        self._last_maintain = 0.0

    @property
    def generation(self) -> int:
        return self.store.generation

    def submit_ingest(self, items: list) -> bool:
        """Never blocks the collection loop; a full queue drops the batch (counted)."""
        if self._stopping.is_set():
            return False
        try:
            self._ingest.put_nowait(items)
        except queue.Full:
            self.store.dropped_ingest += 1
            log.warning("ingest queue full; dropped a batch of %d samples", len(items))
            return False
        self._wake.set()
        return True

    def call(self, fn, timeout: float):
        """Run ``fn(store)`` on the store thread and wait for its result."""
        if self._stopping.is_set():
            raise StoreClosed()
        req = _Request(fn)
        try:
            self._requests.put_nowait(req)
        except queue.Full:
            raise StoreBusy() from None
        self._wake.set()
        if not req.done.wait(timeout):
            req.cancelled = True
            raise StoreTimeout()
        if req.error is not None:
            raise req.error
        return req.result

    def run(self) -> None:
        while True:
            self._wake.clear()
            worked = self._drain_ingest()
            try:
                req = self._requests.get_nowait()
            except queue.Empty:
                req = None
            if req is not None:
                worked = True
                if self._stopping.is_set():
                    req.error = StoreClosed()
                    req.done.set()
                elif not req.cancelled:
                    try:
                        req.result = req.fn(self.store)
                    except Exception as exc:
                        log.exception("history request failed")
                        req.error = exc
                    req.done.set()
            if self._stopping.is_set() and self._ingest.empty() and self._requests.empty():
                break
            mono = time.monotonic()
            if mono - self._last_maintain >= self._maintain_every and self._ingest.empty():
                self._last_maintain = mono
                try:
                    self.store.maintain()
                except Exception:
                    log.exception("metrics maintenance failed")
            if not worked:
                self._wake.wait(1.0)
        self._drain_ingest()
        self.store.close()

    def _drain_ingest(self) -> bool:
        worked = False
        while True:
            try:
                items = self._ingest.get_nowait()
            except queue.Empty:
                return worked
            worked = True
            try:
                self.store.ingest(items)
            except Exception:
                log.exception("raw sample write failed (%d samples lost)", len(items))

    def stop(self, timeout: float = 10.0) -> None:
        """Stop accepting work, flush pending samples, close shard handles."""
        self._stopping.set()
        self._wake.set()
        self.join(timeout)
