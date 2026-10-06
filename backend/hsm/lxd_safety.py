"""Container safety policy, evaluated on LXD's *expanded* config and devices
(i.e. after profiles are applied), not only on what this app asked for.

Used by the collector (to label inventory) and by the worker (immediately
before any mutation or command execution, because an operator can change a
profile outside the app at any time).
"""
from __future__ import annotations

from typing import Iterable

# Config keys whose presence (with a truthy/any value) makes a container unsafe
# to hand to a dashboard user.
_UNSAFE_TRUE = ("security.privileged", "security.nesting", "security.protection.shift",
                "security.syscalls.intercept.mount", "security.syscalls.intercept.mknod",
                "security.syscalls.intercept.bpf", "security.syscalls.intercept.setxattr")
_UNSAFE_PREFIXES = ("raw.", "linux.kernel_modules", "security.idmap.isolated_override")
_ALLOWED_DEVICE_TYPES = {"disk", "nic"}


def evaluate(instance: dict, *, allowed_pools: Iterable[str], allowed_networks: Iterable[str]) -> list[str]:
    """Return a list of human-readable reasons; empty list means safe."""
    reasons: list[str] = []
    pools, nets = set(allowed_pools), set(allowed_networks)
    if instance.get("type", "container") != "container":
        reasons.append("Virtual machines are out of scope.")
    cfg = instance.get("expanded_config") or {}
    for key in _UNSAFE_TRUE:
        if str(cfg.get(key, "")).lower() in ("true", "1", "yes", "on"):
            reasons.append(f"{key} is enabled.")
    for key in cfg:
        if key.startswith(_UNSAFE_PREFIXES):
            reasons.append(f"Low-level option {key} is set.")
    devices = instance.get("expanded_devices") or {}
    has_root = False
    for name, dev in sorted(devices.items()):
        dtype = dev.get("type")
        if dtype not in _ALLOWED_DEVICE_TYPES:
            reasons.append(f"Device '{name}' has disallowed type '{dtype}'.")
            continue
        if dtype == "disk":
            if dev.get("path") == "/" and not dev.get("source"):
                has_root = True
                if dev.get("pool") not in pools:
                    reasons.append(f"Root disk is on pool '{dev.get('pool')}', which is not allowed.")
            else:
                # Any other disk device is a host path or volume mount.
                reasons.append(f"Disk device '{name}' mounts additional storage.")
        elif dtype == "nic":
            if dev.get("network") not in nets:
                reasons.append(f"NIC '{name}' is not attached to an allowed managed network.")
            for k in dev:
                if k.startswith("raw.") or k in ("parent", "nictype"):
                    reasons.append(f"NIC '{name}' uses host-level option '{k}'.")
                    break
    if not has_root:
        reasons.append("No managed root disk on an allowed pool.")
    return reasons
