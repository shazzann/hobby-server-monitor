#!/usr/bin/env python3
"""Sample the CPU and memory of the running Hobby Server Monitor processes.

Linux only (reads /proc). Usage:

    python scripts/measure.py --seconds 600 [--interval 1] [--json out.json]

Processes are found by command line: `hsm.collector`, `hsm.worker`, and the API
(`gunicorn ... hsm.app:wsgi()` or `hsm serve-dev`), including Gunicorn's
master and worker. Definitions used in the report:

- CPU %: (utime + stime) delta / wall time, where 100 % = one logical CPU.
- RSS: resident set size (/proc/<pid>/status VmRSS). Shared library pages are
  counted in full for every process, so RSS totals overstate real use.
- PSS: proportional set size (/proc/<pid>/smaps_rollup). Shared pages are
  divided among the processes sharing them; PSS totals are the fair sum.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time

CLK = os.sysconf("SC_CLK_TCK")
ROLES = (("collector", "hsm.collector"), ("worker", "hsm.worker"), ("api", "hsm.app:wsgi"),
         ("api", "serve-dev"))


def find_processes() -> dict[int, str]:
    found = {}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "measure.py" in cmd:
            continue
        for role, needle in ROLES:
            if needle in cmd:
                found[int(pid)] = role
                break
    return found


def cpu_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])      # utime + stime (fields 14, 15)
    except (OSError, IndexError):
        return None


def kib(path: str, key: str) -> int | None:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + ":"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--json")
    args = ap.parse_args()

    procs = find_processes()
    if not procs:
        raise SystemExit("no hsm processes found; start them first (make dev or systemd)")
    samples: dict[int, dict] = {pid: {"role": role, "cpu": [], "rss": [], "pss": []} for pid, role in procs.items()}
    last = {pid: (cpu_ticks(pid), time.monotonic()) for pid in procs}
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        time.sleep(args.interval)
        for pid, s in samples.items():
            t = cpu_ticks(pid)
            now = time.monotonic()
            prev_t, prev_now = last[pid]
            if t is not None and prev_t is not None:
                s["cpu"].append(100.0 * (t - prev_t) / CLK / (now - prev_now))
            last[pid] = (t, now)
            rss, pss = kib(f"/proc/{pid}/status", "VmRSS"), kib(f"/proc/{pid}/smaps_rollup", "Pss")
            if rss is not None:
                s["rss"].append(rss)
            if pss is not None:
                s["pss"].append(pss)

    def summary(values, scale=1.0):
        if not values:
            return None
        return {"avg": round(statistics.fmean(values) / scale, 2), "peak": round(max(values) / scale, 2)}

    report = {"duration_s": args.seconds, "interval_s": args.interval, "cpu_unit": "% of one logical CPU",
              "logical_cpus": os.cpu_count(), "processes": []}
    totals = {"cpu_avg": 0.0, "rss_avg_mib": 0.0, "pss_avg_mib": 0.0}
    for pid, s in sorted(samples.items(), key=lambda kv: kv[1]["role"]):
        row = {"pid": pid, "role": s["role"], "cpu_pct": summary(s["cpu"]),
               "rss_mib": summary(s["rss"], 1024), "pss_mib": summary(s["pss"], 1024)}
        report["processes"].append(row)
        totals["cpu_avg"] += row["cpu_pct"]["avg"] if row["cpu_pct"] else 0
        totals["rss_avg_mib"] += row["rss_mib"]["avg"] if row["rss_mib"] else 0
        totals["pss_avg_mib"] += row["pss_mib"]["avg"] if row["pss_mib"] else 0
    report["totals"] = {k: round(v, 2) for k, v in totals.items()}
    out = json.dumps(report, indent=2)
    print(out)
    if args.json:
        with open(args.json, "w") as f:
            f.write(out + "\n")


if __name__ == "__main__":
    main()
