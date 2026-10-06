#!/usr/bin/env python3
"""Payload size and latency of the dashboard's real requests.

    python scripts/bench_http.py --cookie "hsm_session=..." [--base http://localhost:8000]
                                 [--container <uuid>] [--repeat 20]

Get a cookie for a local test account with `hsm test-session --email <admin email>`
(local-http mode only). Reports bytes on the wire (uncompressed and gzip -6 as a proxy
would send them), point counts per series, and p50/p95 latency.
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import time
import urllib.request


def fetch(base: str, path: str, cookie: str) -> tuple[float, bytes]:
    req = urllib.request.Request(base + path, headers={"Cookie": cookie})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
    return (time.perf_counter() - t0) * 1000, body


def pct(values, p):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))], 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--cookie", required=True)
    ap.add_argument("--container")
    ap.add_argument("--repeat", type=int, default=20)
    args = ap.parse_args()

    paths = ["/api/containers"]
    if args.container:
        for rng in ("1h", "24h", "7d", "30d"):
            paths.append(f"/api/containers/{args.container}/history?range={rng}"
                         "&metrics=cpu_pct,memory_bytes,rx_rate,tx_rate&max_points=300")
        paths.append(f"/api/containers/{args.container}/usage?range=24h")
    rows = []
    for path in paths:
        times, body = [], b""
        for _ in range(args.repeat):
            ms, body = fetch(args.base, path, args.cookie)
            times.append(ms)
        data = json.loads(body)
        points = {k: len(v["points"]) for k, v in data.get("series", {}).items()} if "series" in data else None
        rows.append({"path": path.split("?")[0] + ("?" + path.split("?")[1].split("&")[0] if "?" in path else ""),
                     "bytes": len(body), "gzip_bytes": len(gzip.compress(body, 6)),
                     "points_per_series": points, "resolution_s": data.get("resolution_seconds"),
                     "p50_ms": pct(times, 50), "p95_ms": pct(times, 95),
                     "mean_ms": round(statistics.fmean(times), 1)})
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
