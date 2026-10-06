"""LXD reads for the collector, behind a two-method interface so tests use a fake.

Per cycle there is exactly one LXD request for instances:
    GET /1.0/instances?recursion=2&all-projects=true
(recursion=2 embeds each instance's state, so CPU/memory/disk/network/process
counters arrive in the same response.)

Slow host metadata (resources, pools, networks, images, project) is read by
``capabilities()``, which the cycle calls about once a minute.

Verified against pylxd 2.4.2 source (.venv-win/Lib/site-packages/pylxd/client.py):
* ``Client(endpoint="http+unix://<urlquoted socket path>")`` mounts the Unix
  adapter; a bare path is only accepted when it already exists, so the quoted
  form is passed explicitly.
* ``Client(...)`` performs ``GET /1.0`` in its constructor and raises
  ``ClientConnectionFailed`` when the daemon is unreachable.
* ``client.api.<segment>.get(params=..., timeout=...)`` returns a
  ``requests.Response`` and raises ``LXDAPIException``/``NotFound`` on non-200;
  ``api.storage_pools`` maps to ``/storage-pools``.
"""
from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import quote

from ..timeutil import utcnow_iso

log = logging.getLogger("hsm.collector.source")

# Drivers whose root volumes enforce a size quota.
QUOTA_DRIVERS = {"btrfs", "zfs", "lvm", "ceph"}


class SourceError(Exception):
    """LXD could not be read (daemon down, timeout, malformed response)."""


class Source(Protocol):
    def list_instances(self) -> list[dict]:
        """All instances in all projects with embedded state. Raises SourceError."""

    def capabilities(self, cfg) -> dict:
        """Host capability snapshot in the contract shape. Raises SourceError."""


def build_capabilities(cfg, *, resources: dict, pools: list[dict], pool_resources: dict[str, dict],
                       networks: list[dict], images: list[dict], project: dict | None) -> dict:
    """Assemble ``service_state['lxd.capabilities']`` from raw LXD documents (pure; tested)."""
    cpu = (resources.get("cpu") or {}).get("total")
    mem = (resources.get("memory") or {}).get("total")
    out_pools = []
    for p in sorted(pools, key=lambda p: p.get("name", "")):
        name = p.get("name")
        if not name:
            continue
        space = (pool_resources.get(name) or {}).get("space") or {}
        driver = p.get("driver") or ""
        out_pools.append({
            "name": name, "driver": driver,
            "total_bytes": space.get("total") if isinstance(space.get("total"), int) else None,
            "used_bytes": space.get("used") if isinstance(space.get("used"), int) else None,
            "quota_capable": driver in QUOTA_DRIVERS,
        })
    out_nets = [{"name": n.get("name"), "type": n.get("type") or "", "managed": bool(n.get("managed"))}
                for n in sorted(networks, key=lambda n: n.get("name", "")) if n.get("name") and n.get("managed")]
    out_images = []
    prefix = cfg.image_alias_prefix
    for img in images:
        props = img.get("properties") or {}
        for alias in img.get("aliases") or []:
            name = alias.get("name") or ""
            if prefix and not name.startswith(prefix):
                continue
            out_images.append({
                "alias": name, "fingerprint": img.get("fingerprint") or "",
                "description": props.get("description") or alias.get("description") or "",
                "os": props.get("os") or "", "release": props.get("release") or "",
                "architecture": props.get("architecture") or img.get("architecture") or "",
                "size_bytes": img.get("size") if isinstance(img.get("size"), int) else None,
            })
    out_images.sort(key=lambda i: i["alias"])
    if project is None:
        proj = {"name": cfg.lxd_project, "exists": False, "restricted": False}
    else:
        pcfg = project.get("config") or {}
        proj = {"name": cfg.lxd_project, "exists": True,
                "restricted": str(pcfg.get("restricted", "false")).lower() == "true"}
    return {
        "observed_at": utcnow_iso(),
        "host": {"cpu_count": cpu if isinstance(cpu, int) else None,
                 "memory_bytes": mem if isinstance(mem, int) else None},
        "pools": out_pools, "networks": out_nets, "images": out_images, "project": proj,
    }


class LXDSource:
    """Real source over the local Unix socket. Reconnects lazily after any failure."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None

    def _connect(self):
        if self._client is None:
            try:
                import pylxd  # imported lazily so tests and the API never need the socket
                endpoint = "http+unix://" + quote(self.cfg.lxd_socket, safe="")
                self._client = pylxd.Client(endpoint=endpoint, timeout=self.cfg.lxd_timeout_seconds)
            except Exception as exc:  # ClientConnectionFailed, requests errors, ...
                raise SourceError(f"cannot connect to LXD: {exc.__class__.__name__}: {exc}") from None
        return self._client

    def _get(self, node, params: dict | None = None, *, missing_ok: bool = False):
        try:
            resp = node.get(params=dict(params or {}))
            return resp.json()["metadata"]
        except Exception as exc:
            if missing_ok and exc.__class__.__name__ == "NotFound":
                return None
            # Drop the client so the next cycle reconnects (daemon restart, socket change).
            self._client = None
            raise SourceError(f"LXD request failed: {exc.__class__.__name__}: {exc}") from None

    def list_instances(self) -> list[dict]:
        api = self._connect().api
        data = self._get(api.instances, {"recursion": 2, "all-projects": "true"})
        if not isinstance(data, list):
            raise SourceError("unexpected instance listing shape")
        return data

    def capabilities(self, cfg) -> dict:
        api = self._connect().api
        resources = self._get(api.resources) or {}
        pools = self._get(api.storage_pools, {"recursion": 1}) or []
        pool_res = {}
        for p in pools:
            name = p.get("name")
            if name:
                try:
                    pool_res[name] = self._get(api.storage_pools[name].resources) or {}
                except SourceError as exc:
                    # One pool's usage failing must not hide the others.
                    log.warning("pool %s resources unavailable: %s", name, exc)
                    self._connect()
        networks = self._get(api.networks, {"recursion": 1}) or []
        images = self._get(api.images, {"recursion": 1}) or []
        project = self._get(api.projects[cfg.lxd_project], missing_ok=True)
        return build_capabilities(cfg, resources=resources, pools=pools, pool_resources=pool_res,
                                  networks=networks, images=images, project=project)
