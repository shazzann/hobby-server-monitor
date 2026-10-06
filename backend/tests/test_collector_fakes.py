"""Deterministic fakes for collector tests (imported by the other test_collector_* modules)."""
from __future__ import annotations

import copy

from hsm.collector.source import SourceError

GIB = 1024 ** 3


def make_instance(name, *, project="hsm", marker=None, vuuid=None, status="Running", cpu_ns=0, mem=100 * 2 ** 20,
                  disk=500 * 2 ** 20, rx=0, tx=0, procs=5, last_used="2026-10-06T10:00:00Z", limits_cpu="1",
                  limits_mem="512MiB", root_size="2GiB", pool="hsm-btrfs", network="hsmbr0", ipv4="10.0.0.2",
                  autostart="true", ephemeral=False):
    cfg = {"image.description": "Alpine 3.22", "image.os": "Alpine", "boot.autostart": autostart}
    if marker:
        cfg["user.hsm.id"] = marker
    if vuuid:
        cfg["volatile.uuid"] = vuuid
    if limits_cpu is not None:
        cfg["limits.cpu"] = limits_cpu
    if limits_mem is not None:
        cfg["limits.memory"] = limits_mem
    root = {"type": "disk", "path": "/", "pool": pool}
    if root_size:
        root["size"] = root_size
    running = status == "Running"
    net = None
    if running:
        net = {
            "lo": {"addresses": [{"family": "inet", "address": "127.0.0.1", "scope": "local"}],
                   "counters": {"bytes_received": 999, "bytes_sent": 999}},
            "eth0": {"addresses": ([{"family": "inet", "address": ipv4, "scope": "global"}] if ipv4 else [])
                     + [{"family": "inet6", "address": "fe80::1", "scope": "link"}],
                     "counters": {"bytes_received": rx, "bytes_sent": tx}},
        }
    return {
        "name": name, "project": project, "type": "container", "status": status, "architecture": "x86_64",
        "ephemeral": ephemeral, "description": "", "last_used_at": last_used if running else "0001-01-01T00:00:00Z",
        "expanded_config": cfg,
        "expanded_devices": {"root": root, "eth0": {"type": "nic", "network": network, "name": "eth0"}},
        "state": {"status": status, "cpu": {"usage": cpu_ns if running else 0},
                  "memory": {"usage": mem if running else 0},
                  "disk": {"root": {"usage": disk}}, "network": net, "processes": procs if running else 0},
    }


class FakeSource:
    def __init__(self, instances=None, cpu_count=8):
        self.instances = instances or []
        self.fail = False
        self.cpu_count = cpu_count
        self.calls = 0

    def list_instances(self):
        self.calls += 1
        if self.fail:
            raise SourceError("connection refused")
        return copy.deepcopy(self.instances)

    def capabilities(self, cfg):
        if self.fail:
            raise SourceError("connection refused")
        return {"observed_at": "2026-10-06T10:00:00Z", "host": {"cpu_count": self.cpu_count, "memory_bytes": 16 * GIB},
                "pools": [], "networks": [], "images": [],
                "project": {"name": cfg.lxd_project, "exists": True, "restricted": True}}


def test_fake_instance_shape():
    inst = make_instance("a", marker="m")
    assert inst["expanded_config"]["user.hsm.id"] == "m"
    assert inst["state"]["network"]["eth0"]["counters"]["bytes_received"] == 0
