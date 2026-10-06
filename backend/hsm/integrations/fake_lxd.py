"""In-memory stand-in for ``LxdAdapter`` (tests and LXD-less development).

It mirrors the adapter's public methods and error semantics, and builds
instances with the *real* ``build_create_body`` / ``limit_config`` so tests
exercise the same config the real adapter would send.

Failure injection: ``fail(method, exc, apply=False)`` makes the next call to
``method`` raise ``exc``; with ``apply=True`` the effect is applied first
(an "LXD did it but we never heard back" uncertainty).
"""
from __future__ import annotations

import threading
import uuid
from typing import Any, Callable

from ..config import Config
from . import lxd
from .lxd_errors import AdapterRefused, AmbiguousIdentity, LxdNotFound, LxdRejected, LxdUnavailable

_EXPECTED = {"start": "Running", "stop": "Stopped", "restart": "Running", "freeze": "Frozen", "unfreeze": "Running"}


class FakeAdapter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.instances: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple] = []
        self.available = True
        self.pool_free = 50 * 1024 ** 3
        self.exec_handler: Callable[..., dict] | None = None
        self._faults: dict[str, list[tuple[Exception, bool]]] = {}
        self._lock = threading.Lock()

    # ---- test helpers
    def fail(self, method: str, exc: Exception, *, apply: bool = False) -> None:
        self._faults.setdefault(method, []).append((exc, apply))

    def add_instance(self, project: str, name: str, *, marker: str | None = None, status: str = "Running",
                     cpu_cores: int = 1, cpu_allowance_pct: int = 100, memory_bytes: int = 1024 ** 3,
                     disk_bytes: int = 2 * 1024 ** 3, pool: str = "hsm-btrfs", network: str = "hsmbr0",
                     ephemeral: bool = False, extra_config: dict | None = None,
                     extra_devices: dict | None = None) -> dict:
        body = lxd.build_create_body(self.cfg, name if lxd.CREATE_NAME_RE.match(name) else "x-placeholder",
                                     image_fingerprint="0" * 64, pool=pool, network=network, cpu_cores=cpu_cores,
                                     cpu_allowance_pct=cpu_allowance_pct, memory_bytes=memory_bytes,
                                     disk_bytes=disk_bytes, ephemeral=ephemeral, autostart=False,
                                     description="", marker=marker or str(uuid.uuid4()))
        body["name"] = name
        if marker is None:
            body["config"].pop(lxd.MARKER_KEY)
        body["config"].update(extra_config or {})
        body["devices"].update(extra_devices or {})
        return self._store(project, body, status)

    def _store(self, project: str, body: dict, status: str) -> dict:
        config = dict(body["config"])
        config["volatile.uuid"] = str(uuid.uuid4())
        raw = {"project": project, "name": body["name"], "status": status, "type": "container",
               "ephemeral": body.get("ephemeral", False), "description": body.get("description", ""),
               "config": config, "devices": dict(body["devices"]),
               "last_used_at": "2026-10-06T00:00:00Z"}
        self._expand(raw)
        self.instances[(project, body["name"])] = raw
        return raw

    @staticmethod
    def _expand(raw: dict) -> None:
        raw["expanded_config"] = dict(raw["config"])
        raw["expanded_devices"] = {k: dict(v) for k, v in raw["devices"].items()}

    def _enter(self, method: str, *args) -> tuple[Exception, bool] | None:
        self.calls.append((method,) + args)
        if not self.available:
            raise LxdUnavailable(f"{method}: LXD is not reachable")
        queue = self._faults.get(method)
        return queue.pop(0) if queue else None

    def _run(self, method: str, args: tuple, effect: Callable[[], Any]) -> Any:
        fault = self._enter(method, *args)
        if fault is None:
            return effect()
        exc, apply = fault
        if apply:
            effect()
        raise exc

    def _get(self, project: str, name: str) -> dict:
        raw = self.instances.get((project, name))
        if raw is None:
            raise LxdNotFound(f"{project}/{name}: not found")
        return raw

    # ---- reads
    def list_instances(self) -> list[dict]:
        return self._run("list_instances", (), lambda: [lxd.normalize_instance(r) for r in self.instances.values()])

    def find_by_marker(self, marker: str) -> dict | None:
        def effect():
            hits = [lxd.normalize_instance(r) for r in self.instances.values()
                    if marker and r["config"].get(lxd.MARKER_KEY) == marker]
            if len(hits) > 1:
                raise AmbiguousIdentity(f"{len(hits)} instances carry marker {marker}")
            return hits[0] if hits else None
        return self._run("find_by_marker", (marker,), effect)

    def get_instance(self, project: str, name: str) -> dict | None:
        def effect():
            raw = self.instances.get((project, name))
            return lxd.normalize_instance(raw) if raw else None
        return self._run("get_instance", (project, name), effect)

    def get_state(self, project: str, name: str) -> dict:
        return self._run("get_state", (project, name), lambda: {"status": self._get(project, name)["status"]})

    def pool_free_bytes(self, pool: str) -> int:
        return self._run("pool_free_bytes", (pool,), lambda: self.pool_free)

    # ---- mutations
    def create(self, project: str, name: str, **kw) -> None:
        def effect():
            if project != self.cfg.lxd_project:
                raise AdapterRefused("wrong project")
            if (project, name) in self.instances:
                raise LxdRejected("create: instance already exists")
            body = lxd.build_create_body(self.cfg, name, **kw)
            self._store(project, body, "Stopped")
        return self._run("create", (project, name), effect)

    def _state(self, action: str, project: str, name: str, force: bool = False) -> None:
        def effect():
            raw = self._get(project, name)
            if action == "start" and raw["status"] != "Stopped":
                raise LxdRejected("start: instance is not stopped")
            if action in ("stop", "restart", "freeze") and raw["status"] == "Stopped":
                raise LxdRejected(f"{action}: instance is not running")
            if action == "unfreeze" and raw["status"] != "Frozen":
                raise LxdRejected("unfreeze: instance is not frozen")
            if action == "stop" and raw["ephemeral"]:
                del self.instances[(project, name)]
                return
            raw["status"] = _EXPECTED[action]
        return self._run(action, (project, name), effect)

    def start(self, project, name):
        self._state("start", project, name)

    def stop(self, project, name, *, force=False):
        self._state("stop", project, name, force)

    def restart(self, project, name, *, force=False):
        self._state("restart", project, name, force)

    def freeze(self, project, name):
        self._state("freeze", project, name)

    def unfreeze(self, project, name):
        self._state("unfreeze", project, name)

    def update_limits(self, project: str, name: str, *, cpu_cores: int, cpu_allowance_pct: int,
                      memory_bytes: int, disk_bytes: int, marker: str | None = None) -> None:
        def effect():
            raw = self._get(project, name)
            live = lxd.parse_size(raw["devices"].get("root", {}).get("size"))
            if live is not None and (disk_bytes // lxd.MIB) * lxd.MIB < live:
                raise AdapterRefused("Disk can only be expanded, not shrunk.", code="DISK_SHRINK")
            raw["config"].update(lxd.limit_config(self.cfg, cpu_cores=cpu_cores, cpu_allowance_pct=cpu_allowance_pct,
                                                  memory_bytes=memory_bytes))
            if marker is not None:
                raw["config"][lxd.MARKER_KEY] = marker
            raw["devices"].setdefault("root", {"type": "disk", "path": "/", "pool": "hsm-btrfs"})
            raw["devices"]["root"]["size"] = lxd.mib_string(disk_bytes)
            self._expand(raw)
        return self._run("update_limits", (project, name), effect)

    def delete(self, project: str, name: str) -> None:
        def effect():
            raw = self._get(project, name)
            if raw["status"] != "Stopped":
                raise LxdRejected("delete: instance is running")
            del self.instances[(project, name)]
        return self._run("delete", (project, name), effect)

    def provision_guest_user(self, project: str, name: str) -> None:
        def effect():
            raw = self._get(project, name)
            if raw["status"] != "Running":
                raise LxdRejected("provision: instance is not running")
            raw["guest_user"] = True
        return self._run("provision_guest_user", (project, name), effect)

    def exec_command(self, project: str, name: str, command: str, *, as_root: bool) -> dict:
        def effect():
            self._get(project, name)
            if self.exec_handler:
                return self.exec_handler(command, as_root=as_root)
            return {"outcome": "completed", "exit_code": 0, "stdout": command, "stderr": "",
                    "stdout_truncated": False, "stderr_truncated": False, "duration_ms": 5,
                    "user": "root" if as_root else "hsm"}
        return self._run("exec_command", (project, name, command, as_root), effect)
