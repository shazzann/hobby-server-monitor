"""TinyFlux store: rollups, crash recovery, retention, history and usage queries."""
from __future__ import annotations

import csv
import dataclasses
from collections import Counter
from datetime import datetime, timezone

import pytest
from tinyflux import Point

from hsm.collector import store as store_mod
from hsm.collector.store import (BUCKET, DAY, GIB, HOUR, ROLL_M, MetricsStore, plan_buckets)
from hsm.services import state

T = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())    # day aligned
A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def fields(dt=10.0, cpu_d=5_000_000_000, mem=GIB, disk=2 * GIB, rx_d=1000, tx_d=100, procs=7):
    return {"state": 1, "cpu_ns": None, "cpu_pct": None if cpu_d is None else 100.0 * cpu_d / (dt * 1e9),
            "cores": 1, "mem": mem, "mem_lim": 2 * GIB, "disk": disk, "disk_lim": 4 * GIB, "rx": None, "tx": None,
            "rx_rate": None if rx_d is None else rx_d / dt, "tx_rate": None if tx_d is None else tx_d / dt,
            "procs": procs, "dt": dt, "cpu_d_ns": cpu_d, "rx_d": rx_d, "tx_d": tx_d}


def feed(st, cid, t0, t1, step=10, **kw):
    st.ingest([(cid, t, fields(**kw)) for t in range(t0, t1, step)])


@pytest.fixture
def st(cfg, conn):
    s = MetricsStore(cfg)
    s.open()
    yield s
    s.close()


def rollup_rows(s, day=T):
    with open(s.roll_path(day), newline="") as f:
        return [r for r in csv.reader(f)]


def test_rollup_buckets_are_time_weighted(st):
    feed(st, A, T, T + 900)                         # 90 samples, 10 s apart
    st.run_rollups(now=T + 900 + st.grace)
    recs = st._rollups(A, T, T + 900)
    assert sorted(recs) == [T, T + 300, T + 600]
    r = recs[T]
    assert r["covered_s"] == 300 and r["samples"] == 30
    assert r["cpu_s"] == 150 and r["cpu_alloc_s"] == 300          # 50 % of one core
    assert r["mem_bs"] == GIB * 300 and r["mem_max"] == GIB and r["rx_d"] == 30_000 and r["procs_max"] == 7
    assert st.checkpoint == T + 900


def test_usage_math_with_rolled_and_unrolled_tail(st):
    feed(st, A, T, T + 900)
    st.run_rollups(now=T + 600 + st.grace)          # only two buckets durable; third comes from raw
    assert st.checkpoint == T + 600
    u = st.usage(A, T, T + 900)
    assert u["covered_seconds"] == 900 and u["coverage"] == 1.0
    assert u["cpu_core_hours"] == pytest.approx(450 / 3600)
    assert u["memory_gib_hours"] == pytest.approx(0.25)
    assert u["disk_avg_bytes"] == 2 * GIB and u["disk_max_bytes"] == 2 * GIB
    assert u["rx_bytes"] == 90_000 and u["tx_bytes"] == 9_000
    empty = st.usage(B, T, T + 900)
    assert empty["coverage"] == 0.0 and empty["cpu_core_hours"] is None and empty["rx_bytes"] is None


def test_crash_between_rollup_write_and_checkpoint_never_duplicates(cfg, st, monkeypatch):
    feed(st, A, T, T + 900)
    feed(st, B, T, T + 900)
    st.run_rollups(now=T + st.grace)                # establishes checkpoint T, writes nothing
    assert st.checkpoint == T
    real_put = state.put

    def failing_put(conn, key, value):
        if key == state.ROLLUP_CHECKPOINT:
            raise RuntimeError("simulated crash before checkpoint")
        return real_put(conn, key, value)

    monkeypatch.setattr(store_mod.state, "put", failing_put)
    with pytest.raises(RuntimeError):
        st.run_rollups(now=T + 900 + st.grace)
    assert len(rollup_rows(st)) == 6                 # shard written, checkpoint not
    monkeypatch.setattr(store_mod.state, "put", real_put)
    st.close()

    # "Restart": recovery trims rollups at/after the durable checkpoint, then recomputes.
    s2 = MetricsStore(cfg)
    s2.open()
    assert s2.checkpoint == T and rollup_rows(s2) == []
    s2.run_rollups(now=T + 900 + s2.grace)
    rows = rollup_rows(s2)
    assert len(rows) == 6
    assert max(Counter((r[0], r[3]) for r in rows).values()) == 1
    assert s2.usage(A, T, T + 900)["cpu_core_hours"] == pytest.approx(450 / 3600)
    s2.close()


def test_in_process_checkpoint_failure_is_trimmed_before_retry(st, monkeypatch):
    feed(st, A, T, T + 600)
    st.run_rollups(now=T + st.grace)
    real_put = state.put
    calls = {"n": 0}

    def flaky_put(conn, key, value):
        if key == state.ROLLUP_CHECKPOINT and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("database is locked")
        return real_put(conn, key, value)

    monkeypatch.setattr(store_mod.state, "put", flaky_put)
    with pytest.raises(RuntimeError):
        st.run_rollups(now=T + 600 + st.grace)
    st.run_rollups(now=T + 600 + st.grace)
    assert len(rollup_rows(st)) == 2


def test_retention_over_35_days_with_churn(cfg, conn):
    s = MetricsStore(cfg)
    s.open()
    now = T + 35 * DAY
    cids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(4)]
    # One rollup point per day for 35 days; the container identity changes every 10 days.
    for d in range(35):
        day = T + d * DAY
        p = Point(time=datetime.fromtimestamp(day, tz=timezone.utc), measurement=ROLL_M,
                  tags={"cid": cids[d // 10]}, fields={k: 1.0 for k in store_mod.ROLL_FIELDS})
        s._handle(s.roll_path(day)).insert_multiple([p], compact_key_prefixes=True)
    # Raw samples every 10 minutes for the last 10 hours.
    feed(s, cids[3], now - 10 * HOUR, now, step=600, dt=600.0)
    old_raw = [h for h, _ in s.shards("raw")]
    assert len(old_raw) == 10

    # Not rolled up yet -> no raw shard may go, whatever its age.
    s._save_checkpoint(now - 10 * HOUR)
    s.apply_retention(now)
    assert [h for h, _ in s.shards("raw")] == old_raw

    s.run_rollups(now)
    s.apply_retention(now)
    raws = [h for h, _ in s.shards("raw")]
    rolls = [d for d, _ in s.shards("rollup")]
    assert raws and all(h + HOUR > now - cfg.raw_retention_hours * HOUR for h in raws) and len(raws) <= 7
    assert all(d + DAY > now - cfg.rollup_retention_days * DAY for d in rolls) and len(rolls) <= 31
    assert min(rolls) == now - 30 * DAY
    s.publish_status()
    status = state.get(conn, state.METRICS_STORAGE)
    assert status["removed_shards"] == 4 + 5 and status["rollup_from"] == min(rolls) and status["bytes"] > 0
    # History is still answerable and bounded after churn.
    h = s.history(cids[3], now - 30 * DAY, now, ["cpu_pct", "memory_bytes"], 600)
    assert h["source"] == "rollup" and len(h["series"]["cpu_pct"]["points"]) <= 600
    s.close()


def test_byte_budget_deletes_oldest_completed_first(cfg, conn):
    s = MetricsStore(cfg)
    s.open()
    for d in range(5):
        feed(s, A, T + d * DAY, T + d * DAY + 1800, step=60, dt=60.0)
    s.run_rollups(now=T + 5 * DAY)
    total = s.total_bytes()
    s.cfg = dataclasses.replace(cfg, metrics_max_bytes=total // 2, raw_retention_hours=10_000,
                                rollup_retention_days=10_000)
    removed = s.apply_retention(now=T + 5 * DAY)
    assert removed and removed[0] == "rollup/20261001.csv"     # oldest completed shard first
    assert s.total_bytes() <= total // 2
    s.close()


def test_history_raw_bounded_with_gaps_and_coverage(st):
    feed(st, A, T, T + 1200)
    # 5-minute collection outage, then a sample without a valid interval (baseline reset).
    st.ingest([(A, T + 1500, fields(dt=None, cpu_d=None, rx_d=None, tx_d=None))])
    feed(st, A, T + 1510, T + 3600)
    h = st.history(A, T, T + 3600, ["cpu_pct", "memory_bytes", "processes"], 300)
    assert h["source"] == "raw" and h["resolution_seconds"] == 20
    pts = h["series"]["cpu_pct"]["points"]
    assert len(pts) <= 300
    assert any(v is None for t, v in pts if T + 1200 <= t < T + 1500)
    assert all(v == 50.0 for t, v in pts if t < T + 1190)
    assert h["gaps"] == [[T + 1190, T + 1500]]
    assert h["coverage"] == pytest.approx((3600 - 310) / 3600, abs=0.01)
    assert h["series"]["memory_bytes"]["unit"] == "bytes" and h["available_from"] == T


def test_history_rollup_source_and_hard_point_cap(st):
    feed(st, A, T + DAY, T + DAY + 3 * HOUR)
    st.run_rollups(now=T + DAY + 2 * HOUR)            # last hour stays in the unrolled tail
    start, end = T + DAY + 3 * HOUR - 7 * DAY, T + DAY + 3 * HOUR
    h = st.history(A, start, end, ["cpu_pct", "rx_rate"], 600)
    assert h["source"] == "rollup" and h["resolution_seconds"] % BUCKET == 0
    pts = h["series"]["cpu_pct"]["points"]
    assert len(pts) <= 600
    vals = [v for _t, v in pts if v is not None]
    assert vals and all(v == pytest.approx(50.0) for v in vals)
    assert h["coverage"] == pytest.approx(3 * HOUR / (7 * DAY), rel=0.02)
    assert h["gaps"][0] == [start, T + DAY]
    assert plan_buckets(0, 30 * DAY, 5000, 300)[2] <= 600
    for mp in (1, 7, 300, 600):
        assert plan_buckets(start + 13, end, mp, 10)[2] <= mp


def test_unterminated_shard_is_quarantined_and_salvaged(cfg, conn):
    s = MetricsStore(cfg)
    s.open()
    feed(s, A, T, T + 300)
    path = s.raw_path(T)
    s.close()
    with open(path, "a", newline="") as f:
        f.write("2026-10-01T00:05:00,c_raw_v1,t_cid," + A + ",f_state,1.0,f_cpu")   # torn final record
    s2 = MetricsStore(cfg)
    s2.open()
    h = s2.history(A, T, T + 600, ["cpu_pct"], 60)
    assert sum(1 for _t, v in h["series"]["cpu_pct"]["points"] if v is not None) > 0
    assert list(path.parent.glob("*.corrupt"))
    q = s2.quarantined[-1]
    assert q["shard"] == "raw/" + path.name and q["from"] == T and q["to"] == T + HOUR and q["dropped_rows"] == 1
    feed(s2, A, T + 300, T + 600)                    # appends cleanly afterwards
    assert len(s2._raw_points(A, T, T + 600)) == 60
    s2.close()
