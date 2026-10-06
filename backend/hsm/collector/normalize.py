"""Turn one LXD instance (from ``GET /1.0/instances?recursion=2``) into a sample.

Rules that matter (see plan section 9):

* Unknown is ``None``, never 0. A field LXD did not report stays ``None``.
* CPU % is "% of the container's allocated cores":
      100 * delta(cpu_ns) / (elapsed_monotonic_s * 1e9 * allocated_cores)
  Elapsed time is measured with the collector's monotonic clock, never with
  wall-clock timestamps (NTP steps would corrupt the rate).
* Rate baselines are reset (rates = None) on: first sample, counter decrease,
  restart (``last_used_at`` changed), changed core count (CPU only), changed
  interface set (network only), a gap > 3 x interval, or a non-Running state.
* ``dt`` is the *observation* interval: seconds since the previous successful
  observation of this container when that gap is <= 3 x interval. It is what a
  sample "covers" for gauges (memory/disk time integrals and history coverage)
  and is independent of whether counter deltas were valid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..timeutil import iso, parse as parse_ts

STATES = ("Running", "Stopped", "Frozen", "Error")
# TinyFlux fields are numeric only, so the state is stored as a small code.
STATE_CODES = {"Unknown": 0, "Running": 1, "Stopped": 2, "Frozen": 3, "Error": 4}
CODE_STATES = {v: k for k, v in STATE_CODES.items()}

LXD_ZERO_TIME = "0001-01-01T00:00:00Z"

_UNITS = {
    "": 1, "B": 1,
    "kB": 10 ** 3, "MB": 10 ** 6, "GB": 10 ** 9, "TB": 10 ** 12, "PB": 10 ** 15, "EB": 10 ** 18,
    "KiB": 2 ** 10, "MiB": 2 ** 20, "GiB": 2 ** 30, "TiB": 2 ** 40, "PiB": 2 ** 50, "EiB": 2 ** 60,
}
# LXD itself accepts upper-case K for decimal kilobytes in some places; be lenient.
_UNITS["KB"] = 10 ** 3
_BYTES_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")


def parse_bytes(value) -> int | None:
    """Parse an LXD byte size ('512MiB', '1GB', '1073741824'). None if absent or unparsable.

    Percentages ('50%') are not byte sizes; the caller flags them separately.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    m = _BYTES_RE.match(str(value))
    if not m:
        return None
    number, unit = m.groups()
    if unit not in _UNITS:
        return None
    return int(float(number) * _UNITS[unit])


def _cpuset_count(spec: str) -> int | None:
    """Count CPUs in a cpuset like '0-1,3'. None if malformed."""
    total = 0
    try:
        for part in spec.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = (int(x) for x in part.split("-", 1))
                if hi < lo:
                    return None
                total += hi - lo + 1
            else:
                int(part)
                total += 1
    except ValueError:
        return None
    return total or None


def cpu_count_limit(value) -> int | None:
    """``limits.cpu`` as a plain core count, else None (unset or a cpuset)."""
    if value is None:
        return None
    s = str(value).strip()
    if s.isdigit() and int(s) > 0:
        return int(s)
    return None


def allocated_cores(limits_cpu, host_cpus: int | None) -> tuple[int | None, list[str]]:
    """Denominator for CPU %. Returns (cores, quality flags)."""
    n = cpu_count_limit(limits_cpu)
    if n is not None:
        return n, []
    if limits_cpu not in (None, ""):
        pinned = _cpuset_count(str(limits_cpu))
        if pinned is not None:
            # A pinned set like '0-1' really limits the container to 2 CPUs; using
            # the host count would understate its load.
            return pinned, ["cores_from_cpuset"]
    if host_cpus:
        return host_cpus, ["cores_unlimited"]
    return None, ["cores_unknown"]


def root_device(instance: dict) -> dict | None:
    devices = instance.get("expanded_devices")
    if not isinstance(devices, dict):
        return None
    for _name, dev in sorted(devices.items()):
        if isinstance(dev, dict) and dev.get("type") == "disk" and dev.get("path") == "/":
            return dev
    return None


def state_name(instance: dict) -> str:
    st = instance.get("state") if isinstance(instance.get("state"), dict) else {}
    name = st.get("status") or instance.get("status")
    return name if name in STATES else "Unknown"


def _nonneg_int(value) -> int | None:
    """LXD reports -1 for some unavailable values; treat negatives and non-numbers as unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _network(st: dict) -> tuple[int | None, int | None, frozenset, str | None]:
    """Sum RX/TX over all non-loopback interfaces; pick the first global IPv4.

    Returns (rx, tx, interface names, ipv4). rx/tx are None when the state has no
    network section or any counted interface lacks counters (a partial sum would
    look like a counter decrease later).
    """
    nets = st.get("network")
    if not isinstance(nets, dict):
        return None, None, frozenset(), None
    rx = tx = 0
    complete = True
    ipv4 = None
    names = []
    for name in sorted(nets):
        if name == "lo":
            continue
        iface = nets[name] if isinstance(nets[name], dict) else {}
        names.append(name)
        counters = iface.get("counters") or {}
        r, t = _nonneg_int(counters.get("bytes_received")), _nonneg_int(counters.get("bytes_sent"))
        if r is None or t is None:
            complete = False
        else:
            rx += r
            tx += t
        if ipv4 is None:
            for addr in iface.get("addresses") or []:
                if addr.get("family") == "inet" and addr.get("scope") == "global" and addr.get("address"):
                    ipv4 = addr["address"]
                    break
    if not names or not complete:
        return None, None, frozenset(names), ipv4
    return rx, tx, frozenset(names), ipv4


@dataclass
class Sample:
    container_id: str
    sampled_at: datetime          # UTC wall clock, whole seconds
    state: str
    cpu_ns: int | None = None
    cpu_pct: float | None = None
    cores: int | None = None
    mem: int | None = None
    mem_lim: int | None = None
    disk: int | None = None
    disk_lim: int | None = None
    rx: int | None = None
    tx: int | None = None
    rx_rate: float | None = None
    tx_rate: float | None = None
    procs: int | None = None
    ipv4: str | None = None
    started_at: str | None = None
    dt: float | None = None       # valid observation interval, seconds
    cpu_d_ns: int | None = None
    rx_d: int | None = None
    tx_d: int | None = None
    quality: list[str] = field(default_factory=list)

    def latest_row(self) -> tuple:
        """Values for the latest_metrics upsert (column order in cycle.UPSERT_SQL)."""
        import json
        return (self.container_id, self.state, self.cpu_pct, self.mem, self.mem_lim, self.disk,
                self.disk_lim, self.rx, self.tx, self.rx_rate, self.tx_rate, self.procs, self.ipv4,
                self.started_at, iso(self.sampled_at), json.dumps(sorted(set(self.quality))))

    def raw_fields(self) -> dict:
        """Fields of the TinyFlux ``c_raw_v1`` point (unknown = None)."""
        return {
            "state": STATE_CODES[self.state], "cpu_ns": self.cpu_ns, "cpu_pct": self.cpu_pct,
            "cores": self.cores, "mem": self.mem, "mem_lim": self.mem_lim, "disk": self.disk,
            "disk_lim": self.disk_lim, "rx": self.rx, "tx": self.tx, "rx_rate": self.rx_rate,
            "tx_rate": self.tx_rate, "procs": self.procs, "dt": self.dt, "cpu_d_ns": self.cpu_d_ns,
            "rx_d": self.rx_d, "tx_d": self.tx_d,
        }


@dataclass
class _Baseline:
    mono: float
    running: bool
    last_used_at: str | None
    cores: int | None
    cpu_ns: int | None
    rx: int | None
    tx: int | None
    ifaces: frozenset


class Normalizer:
    """Holds per-container rate baselines, keyed by the application container UUID
    (so an identity-preserving rename keeps its baseline)."""

    def __init__(self, interval_seconds: float):
        self.interval = float(interval_seconds)
        self._base: dict[str, _Baseline] = {}

    def retain(self, container_ids) -> None:
        """Forget baselines of containers that were not observed this cycle."""
        keep = set(container_ids)
        for cid in list(self._base):
            if cid not in keep:
                del self._base[cid]

    def sample(self, container_id: str, instance: dict, *, mono: float, wall: datetime,
               host_cpus: int | None) -> Sample:
        st = instance.get("state") if isinstance(instance.get("state"), dict) else {}
        cfg = instance.get("expanded_config") or {}
        state = state_name(instance)
        running = state == "Running"
        q: list[str] = []
        s = Sample(container_id=container_id, sampled_at=wall.astimezone(timezone.utc).replace(microsecond=0),
                   state=state, quality=q)

        cores, flags = allocated_cores(cfg.get("limits.cpu"), host_cpus)
        s.cores = cores
        q.extend(flags)

        s.cpu_ns = _nonneg_int((st.get("cpu") or {}).get("usage"))
        s.mem = _nonneg_int((st.get("memory") or {}).get("usage"))
        mem_lim_raw = cfg.get("limits.memory")
        if mem_lim_raw and str(mem_lim_raw).strip().endswith("%"):
            q.append("memory_limit_percent")
        else:
            s.mem_lim = parse_bytes(mem_lim_raw)
        disk_state = (st.get("disk") or {}).get("root") or {}
        s.disk = _nonneg_int(disk_state.get("usage"))
        root = root_device(instance)
        s.disk_lim = parse_bytes(root.get("size")) if root else None
        s.rx, s.tx, ifaces, s.ipv4 = _network(st)
        s.procs = _nonneg_int(st.get("processes"))

        last_used = instance.get("last_used_at")
        if not last_used or str(last_used).startswith("0001-01-01"):
            last_used = None
        if running and last_used:
            try:
                s.started_at = iso(parse_ts(last_used))
                q.append("uptime_source_last_used_at")
            except ValueError:
                q.append("started_at_unparsable")

        for name in ("cpu_ns", "mem", "procs"):
            if running and getattr(s, name) is None:
                q.append("missing_" + name)

        prev = self._base.get(container_id)
        if prev is None:
            q.append("baseline_first_sample")
            gap_ok = False
        else:
            elapsed = mono - prev.mono
            gap_ok = 0 < elapsed <= 3 * self.interval
            if not gap_ok:
                q.append("baseline_gap")
        observed = state != "Unknown"
        if gap_ok and observed:
            s.dt = round(mono - prev.mono, 3)

        # Counter deltas are only meaningful between two Running observations of
        # the same boot (same last_used_at) inside a short gap.
        same_run = (gap_ok and running and prev is not None and prev.running
                    and prev.last_used_at == last_used)
        if prev is not None and gap_ok and running and prev.running and prev.last_used_at != last_used:
            q.append("baseline_restart")
        if same_run and s.dt:
            if prev.cores == cores and cores and s.cpu_ns is not None and prev.cpu_ns is not None:
                d = s.cpu_ns - prev.cpu_ns
                if d >= 0:
                    s.cpu_d_ns = d
                    s.cpu_pct = round(100.0 * d / (s.dt * 1e9 * cores), 3)
                else:
                    q.append("baseline_cpu_counter_reset")
            elif prev.cores != cores:
                q.append("baseline_cores_changed")
            if (s.rx is not None and s.tx is not None and prev.rx is not None and prev.tx is not None
                    and ifaces == prev.ifaces):
                drx, dtx = s.rx - prev.rx, s.tx - prev.tx
                if drx >= 0 and dtx >= 0:
                    s.rx_d, s.tx_d = drx, dtx
                    s.rx_rate = round(drx / s.dt, 3)
                    s.tx_rate = round(dtx / s.dt, 3)
                else:
                    q.append("baseline_net_counter_reset")
            elif ifaces != prev.ifaces:
                q.append("baseline_interfaces_changed")

        if observed:
            self._base[container_id] = _Baseline(mono=mono, running=running, last_used_at=last_used, cores=cores,
                                                 cpu_ns=s.cpu_ns, rx=s.rx, tx=s.tx, ifaces=ifaces)
        else:
            # An Unknown observation breaks continuity: the next sample starts fresh.
            self._base.pop(container_id, None)
        return s
