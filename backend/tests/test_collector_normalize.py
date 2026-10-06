"""Sample normalization: CPU/rate math and every baseline reset rule."""
from __future__ import annotations

from datetime import datetime, timezone

from test_collector_fakes import make_instance

from hsm.collector.normalize import Normalizer, allocated_cores, parse_bytes

T = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
CID = "11111111-1111-4111-8111-111111111111"


def two(n, a, b, dt=10.0, host=8):
    s1 = n.sample(CID, a, mono=100.0, wall=T, host_cpus=host)
    s2 = n.sample(CID, b, mono=100.0 + dt, wall=T, host_cpus=host)
    return s1, s2


def test_cpu_pct_and_network_rates():
    n = Normalizer(10)
    s1, s2 = two(n, make_instance("a", cpu_ns=1_000_000_000, rx=1000, tx=50),
                 make_instance("a", cpu_ns=6_000_000_000, rx=3000, tx=150))
    assert s1.cpu_pct is None and s1.rx_rate is None and s1.dt is None
    assert "baseline_first_sample" in s1.quality
    # 5 s of CPU over 10 s on 1 allocated core = 50 %
    assert s2.cpu_pct == 50.0 and s2.cpu_d_ns == 5_000_000_000 and s2.dt == 10.0
    # loopback excluded from totals and rates
    assert s2.rx == 3000 and s2.rx_rate == 200.0 and s2.tx_rate == 10.0
    assert s2.ipv4 == "10.0.0.2"


def test_cpu_denominator_uses_allocated_or_host_cores():
    n = Normalizer(10)
    _, s = two(n, make_instance("a", cpu_ns=0, limits_cpu="2"), make_instance("a", cpu_ns=5_000_000_000, limits_cpu="2"))
    assert s.cpu_pct == 25.0 and s.cores == 2
    n = Normalizer(10)
    _, s = two(n, make_instance("a", cpu_ns=0, limits_cpu=None), make_instance("a", cpu_ns=8_000_000_000, limits_cpu=None))
    assert s.cores == 8 and s.cpu_pct == 10.0 and "cores_unlimited" in s.quality
    assert allocated_cores("0-1,3", 8) == (3, ["cores_from_cpuset"])


def test_counter_decrease_resets():
    n = Normalizer(10)
    _, s = two(n, make_instance("a", cpu_ns=9_000_000_000, rx=500), make_instance("a", cpu_ns=1_000, rx=10))
    assert s.cpu_pct is None and s.rx_rate is None
    assert "baseline_cpu_counter_reset" in s.quality and "baseline_net_counter_reset" in s.quality
    assert s.dt == 10.0          # observation continuity is unaffected


def test_restart_resets():
    n = Normalizer(10)
    _, s = two(n, make_instance("a", cpu_ns=1, last_used="2026-10-06T10:00:00Z"),
               make_instance("a", cpu_ns=2_000_000_000, last_used="2026-10-06T11:59:58Z"))
    assert s.cpu_pct is None and "baseline_restart" in s.quality
    assert s.started_at == "2026-10-06T11:59:58Z" and "uptime_source_last_used_at" in s.quality


def test_gap_resets_rates_and_coverage():
    n = Normalizer(10)
    _, s = two(n, make_instance("a", cpu_ns=1), make_instance("a", cpu_ns=2_000_000_000), dt=31.0)
    assert s.cpu_pct is None and s.dt is None and "baseline_gap" in s.quality


def test_core_change_resets_cpu_only():
    n = Normalizer(10)
    _, s = two(n, make_instance("a", cpu_ns=0, limits_cpu="1", rx=0),
               make_instance("a", cpu_ns=1_000_000_000, limits_cpu="2", rx=100))
    assert s.cpu_pct is None and "baseline_cores_changed" in s.quality
    assert s.rx_rate == 10.0


def test_stopped_container_has_no_rates_and_no_fabricated_values():
    n = Normalizer(10)
    _, s = two(n, make_instance("a", cpu_ns=5), make_instance("a", status="Stopped"))
    assert s.state == "Stopped"
    assert s.cpu_pct is None and s.rx_rate is None and s.rx is None and s.ipv4 is None
    assert s.mem == 0                     # LXD reported 0 for the stopped container
    assert s.started_at is None
    assert s.dt == 10.0                   # a confirmed Stopped observation still covers the interval


def test_missing_fields_stay_none():
    inst = make_instance("a")
    del inst["state"]["memory"]
    del inst["state"]["disk"]
    inst["state"]["processes"] = -1       # LXD's "unknown"
    inst["expanded_devices"]["root"].pop("size")
    s = Normalizer(10).sample(CID, inst, mono=1.0, wall=T, host_cpus=4)
    assert s.mem is None and s.disk is None and s.procs is None and s.disk_lim is None
    assert "missing_mem" in s.quality


def test_unknown_state_and_zero_time():
    inst = make_instance("a")
    inst["status"] = inst["state"]["status"] = "Weird"
    s = Normalizer(10).sample(CID, inst, mono=1.0, wall=T, host_cpus=4)
    assert s.state == "Unknown" and s.started_at is None


def test_memory_limits():
    assert parse_bytes("512MiB") == 512 * 2 ** 20
    assert parse_bytes("1GB") == 10 ** 9
    assert parse_bytes("1073741824") == 2 ** 30
    assert parse_bytes("50%") is None and parse_bytes("lots") is None and parse_bytes(None) is None
    s = Normalizer(10).sample(CID, make_instance("a", limits_mem="50%"), mono=1.0, wall=T, host_cpus=4)
    assert s.mem_lim is None and "memory_limit_percent" in s.quality
    assert Normalizer(10).sample(CID, make_instance("a", ipv4=None), mono=1.0, wall=T, host_cpus=4).ipv4 is None
