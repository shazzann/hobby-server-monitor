#!/usr/bin/env python3
"""Real-LXD spike for the control adapter. Linux only, after setup-dev-host.sh.

    ~/.venvs/hsm/bin/python scripts/lxd_spike.py            # prints a JSON report

Safety: works ONLY in LXD project ``hsm`` and ONLY on an instance it creates
itself, named ``hsm-spike-<random>`` and carrying a fresh identity marker.
Every mutation re-checks both; cleanup deletes only that instance.

What it records: create/start through LxdAdapter, whether ``last_used_at``
changes on restart (uptime source), live CPU/RAM change, disk expansion,
exec as root and as uid 1500 (stdout/stderr/exit code), the 1 MiB output cap,
a ``sleep 60`` deadline test with a follow-up ``ps``, ``cpu.max`` inside the
container (hard CFS allowance), then stop and delete.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hsm import config as config_mod  # noqa: E402
from hsm import lxd_safety  # noqa: E402
from hsm.integrations.lxd import LxdAdapter  # noqa: E402

PROJECT = "hsm"
PREFIX = "hsm-spike-"
IMAGE_ALIAS = "hsm/alpine-3.22"
MIB = 1024 ** 2


def main() -> int:
    cfg = config_mod.load()
    if cfg.lxd_project != PROJECT:
        print(f"refusing: LXD_PROJECT is {cfg.lxd_project!r}, expected {PROJECT!r}", file=sys.stderr)
        return 2
    adapter = LxdAdapter(cfg)
    name = PREFIX + uuid.uuid4().hex[:8]
    marker = str(uuid.uuid4())
    pool, network = cfg.allowed_pools[0], cfg.allowed_networks[0]
    report: dict = {"name": name, "project": PROJECT, "marker": marker, "steps": {}}
    steps = report["steps"]

    def guard() -> dict:
        inst = adapter.find_by_marker(marker)
        assert inst is not None, "spike instance vanished"
        assert inst["project"] == PROJECT and inst["name"] == name and inst["name"].startswith(PREFIX)
        return inst

    def ex(cmd: str, *, as_root: bool, a: LxdAdapter = adapter) -> dict:
        guard()
        t = time.monotonic()
        res = a.exec_command(PROJECT, name, cmd, as_root=as_root)
        res["wall_ms"] = int((time.monotonic() - t) * 1000)
        if len(res["stdout"]) > 400:
            res["stdout"] = f"<{len(res['stdout'])} chars>"
        return res

    if adapter.get_instance(PROJECT, name) is not None:
        print("refusing: name already exists", file=sys.stderr)
        return 2
    alias = adapter._client().api.images.aliases[quote(IMAGE_ALIAS, safe="")].get()   # default project
    fingerprint = alias.json()["metadata"]["target"]
    steps["image"] = {"alias": IMAGE_ALIAS, "fingerprint": fingerprint}
    created = False
    try:
        t = time.monotonic()
        adapter.create(PROJECT, name, image_fingerprint=fingerprint, pool=pool, network=network, cpu_cores=1,
                       cpu_allowance_pct=50, memory_bytes=256 * MIB, disk_bytes=1024 * MIB, ephemeral=False,
                       autostart=False, description="hsm adapter spike", marker=marker)
        created = True
        steps["create_ms"] = int((time.monotonic() - t) * 1000)
        inst = guard()
        steps["created_config"] = {k: v for k, v in inst["expanded_config"].items() if not k.startswith("volatile.")}
        steps["created_devices"] = inst["expanded_devices"]
        steps["safety_reasons"] = lxd_safety.evaluate(inst, allowed_pools=cfg.allowed_pools,
                                                      allowed_networks=cfg.allowed_networks)
        adapter.start(PROJECT, name)
        first = guard()
        steps["state_after_start"] = {"status": first["status"], "last_used_at": first["last_used_at"],
                                      "state": _brief_state(adapter.get_state(PROJECT, name))}
        time.sleep(2)
        adapter.restart(PROJECT, name)
        second = guard()
        steps["restart"] = {"last_used_at_before": first["last_used_at"], "last_used_at_after": second["last_used_at"],
                            "changed": first["last_used_at"] != second["last_used_at"],
                            "state": _brief_state(adapter.get_state(PROJECT, name))}
        adapter.provision_guest_user(PROJECT, name)
        steps["cpu_max_initial"] = ex("cat /sys/fs/cgroup/cpu.max", as_root=True)
        adapter.update_limits(PROJECT, name, cpu_cores=2, cpu_allowance_pct=50, memory_bytes=512 * MIB,
                              disk_bytes=2048 * MIB)
        after = guard()
        steps["after_update"] = {k: after["expanded_config"].get(k) for k in
                                 ("limits.cpu", "limits.cpu.allowance", "limits.memory", "limits.memory.swap",
                                  "limits.processes")}
        steps["after_update"]["root"] = after["expanded_devices"].get("root")
        steps["cpu_max_after"] = ex("cat /sys/fs/cgroup/cpu.max; cat /sys/fs/cgroup/memory.max; nproc", as_root=True)
        steps["df_after"] = ex("df -k /", as_root=True)
        steps["exec_root"] = ex("id; pwd; echo out; echo err >&2; exit 3", as_root=True)
        steps["exec_guest"] = ex("id; pwd; echo $HOME $USER $PATH; echo err >&2; exit 4", as_root=False)
        steps["exec_guest_sudo"] = ex("grep -E '^(sudo|wheel|admin):' /etc/group || true", as_root=False)
        steps["output_cap"] = ex("head -c 1048576 /dev/zero | tr '\\000' a", as_root=False)
        short = LxdAdapter(dataclasses.replace(cfg, exec_deadline_seconds=5))
        steps["deadline_guest"] = ex("sleep 60 & sleep 60", as_root=False, a=short)
        steps["ps_after_guest_deadline"] = ex("ps -o pid,user,args 2>/dev/null || ps", as_root=True)
        steps["background_guest"] = ex("sleep 300 > /dev/null 2>&1 &", as_root=False)
        steps["ps_after_background"] = ex("ps -o pid,user,args 2>/dev/null || ps", as_root=True)
        steps["deadline_root"] = ex("sleep 60", as_root=True, a=short)
        steps["ps_after_root_deadline"] = ex("ps -o pid,user,args 2>/dev/null || ps", as_root=True)
        guard()
        adapter.stop(PROJECT, name, force=True)
        guard()
        adapter.delete(PROJECT, name)
        steps["deleted"] = adapter.find_by_marker(marker) is None
        created = False
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if created:
            try:
                inst = adapter.find_by_marker(marker)
                if inst and inst["project"] == PROJECT and inst["name"] == name:
                    if inst["status"] != "Stopped":
                        adapter.stop(PROJECT, name, force=True)
                    adapter.delete(PROJECT, name)
                    report["cleanup"] = "deleted"
            except Exception as exc:
                report["cleanup"] = f"FAILED, delete {PROJECT}/{name} manually: {exc}"
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if "error" in report else 0


def _brief_state(st: dict) -> dict:
    return {"status": st.get("status"), "pid": st.get("pid"), "processes": st.get("processes"),
            "cpu_usage_ns": (st.get("cpu") or {}).get("usage"),
            "memory_usage": (st.get("memory") or {}).get("usage")}


if __name__ == "__main__":
    raise SystemExit(main())
