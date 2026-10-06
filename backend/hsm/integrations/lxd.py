"""The single adapter between the control worker and LXD (via pylxd 2.4.x).

Design rules
------------
* Callers never pass raw LXD config. The adapter builds every instance config
  from typed arguments (``build_create_body`` / ``limit_config``), so a browser
  field can never smuggle ``raw.*``, privileged mode, nesting or extra devices.
* Every failure is mapped to an *outcome* (see ``lxd_errors``): rejected (not
  applied) vs uncertain (may have been applied) vs unavailable (never sent).
* Long LXD operations are awaited with an explicit ``/wait?timeout=N`` plus an
  HTTP read timeout slightly above N. A wait that returns while the operation
  is still running is *uncertain*, never "failed".
* Instances are resolved by the ``user.hsm.id`` marker in their *local* config
  (a value inherited from a profile is not an identity), across all projects.

Verified against pylxd 2.4.2 source (``.venv-win``):
* ``Client(endpoint=...)`` only converts a filesystem path to ``http+unix://``
  if the path exists locally, so we always pass the quoted URL ourselves.
* ``_APINode`` adds ``?project=`` only when the client has a project; with a
  project-less client we pass ``params={'project': ...}`` explicitly.
* ``Instance.execute`` waits on ``/operations/<id>/wait`` with the *client's*
  HTTP timeout and no server-side timeout, so exec gets a dedicated client
  whose timeout exceeds our own deadline handling (``lxd_exec``).
"""
from __future__ import annotations

import posixpath
import re
import threading
from typing import Any, Callable
from urllib.parse import quote, urlparse

from ..config import Config
from . import lxd_exec
from .lxd_errors import (AdapterRefused, AmbiguousIdentity, LxdError, LxdNotFound, LxdRejected,
                         LxdUncertain, LxdUnavailable)

MIB = 1024 ** 2
MARKER_KEY = "user.hsm.id"
CREATE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
EXISTING_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")   # names LXD itself reported
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
MARKER_RE = re.compile(r"^[0-9a-f-]{36}$")

# Seconds to let LXD work before declaring the outcome uncertain.
CREATE_WAIT = 300
STATE_WAIT = 90
UPDATE_WAIT = 60
DELETE_WAIT = 120
STOP_TIMEOUT = 30          # graceful shutdown window passed to LXD
PROVISION_DEADLINE = 60

# Fixed provisioning script: no user input is ever interpolated into it.
PROVISION_SCRIPT = r"""set -eu
if grep -q '^hsm:' /etc/passwd; then
  uid=$(grep '^hsm:' /etc/passwd | cut -d: -f3)
  [ "$uid" = 1500 ] || { echo "user hsm exists with uid $uid" >&2; exit 3; }
else
  if grep -q '^[^:]*:[^:]*:1500:' /etc/passwd; then echo "uid 1500 is taken" >&2; exit 4; fi
  if command -v useradd >/dev/null 2>&1; then
    grep -q '^hsm:' /etc/group || groupadd -g 1500 hsm
    useradd -u 1500 -g 1500 -m -d /home/hsm -s /bin/sh hsm
  else
    grep -q '^hsm:' /etc/group || addgroup -g 1500 hsm
    adduser -D -u 1500 -G hsm -h /home/hsm -s /bin/sh hsm
  fi
fi
mkdir -p /home/hsm
chown 1500:1500 /home/hsm
chmod 0750 /home/hsm
"""

_SIZE_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]*)\s*$")
_UNITS = {"": 1, "B": 1, "bytes": 1,
          "kB": 1000, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4, "PB": 1000 ** 5,
          "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4, "PiB": 1024 ** 5}


# ------------------------------------------------------------ pure helpers

def parse_size(value: Any) -> int | None:
    """LXD size string -> bytes. None for missing, relative ('50%') or unparsable."""
    if value is None:
        return None
    m = _SIZE_RE.match(str(value))
    if not m or m.group(2) not in _UNITS:
        return None
    return int(m.group(1)) * _UNITS[m.group(2)]


def allowance(cpu_cores: int, allowance_pct: int) -> str:
    """Hard CFS quota: N cores at P% -> N*P ms per 100 ms period.
    Never a bare percentage, which LXD treats as a soft scheduler share."""
    return f"{cpu_cores * allowance_pct}ms/100ms"


def mib_string(n_bytes: int) -> str:
    return f"{n_bytes // MIB}MiB"


def _require_int(name: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise AdapterRefused(f"{name} must be an integer between {lo} and {hi}.")
    return value


def _check_limits(cpu_cores: int, cpu_allowance_pct: int, memory_bytes: int, disk_bytes: int) -> None:
    _require_int("cpu_cores", cpu_cores, 1, 1024)
    _require_int("cpu_allowance_pct", cpu_allowance_pct, 1, 100)
    _require_int("memory_bytes", memory_bytes, MIB, 1 << 50)
    _require_int("disk_bytes", disk_bytes, MIB, 1 << 56)


def limit_config(cfg: Config, *, cpu_cores: int, cpu_allowance_pct: int, memory_bytes: int) -> dict[str, str]:
    _check_limits(cpu_cores, cpu_allowance_pct, memory_bytes, MIB)
    return {
        "limits.cpu": str(cpu_cores),
        "limits.cpu.allowance": allowance(cpu_cores, cpu_allowance_pct),
        "limits.memory": mib_string(memory_bytes),
        "limits.memory.swap": "false",
        "limits.processes": str(cfg.container_max_processes),
    }


def build_create_body(cfg: Config, name: str, *, image_fingerprint: str, pool: str, network: str,
                      cpu_cores: int, cpu_allowance_pct: int, memory_bytes: int, disk_bytes: int,
                      ephemeral: bool, autostart: bool, description: str, marker: str) -> dict:
    """The fixed, reviewed creation template. Every value is validated here."""
    if not CREATE_NAME_RE.match(name or ""):
        raise AdapterRefused("Invalid container name.")
    if not FINGERPRINT_RE.match(image_fingerprint or ""):
        raise AdapterRefused("Image fingerprint must be 64 lowercase hex characters.")
    if pool not in cfg.allowed_pools:
        raise AdapterRefused(f"Pool '{pool}' is not allowed.")
    if network not in cfg.allowed_networks:
        raise AdapterRefused(f"Network '{network}' is not allowed.")
    if not MARKER_RE.match(marker or ""):
        raise AdapterRefused("Invalid identity marker.")
    if not isinstance(ephemeral, bool) or not isinstance(autostart, bool):
        raise AdapterRefused("ephemeral/autostart must be booleans.")
    if not isinstance(description, str) or len(description) > 1024:
        raise AdapterRefused("Description is too long.")
    _check_limits(cpu_cores, cpu_allowance_pct, memory_bytes, disk_bytes)
    config = limit_config(cfg, cpu_cores=cpu_cores, cpu_allowance_pct=cpu_allowance_pct,
                          memory_bytes=memory_bytes)
    config.update({
        "boot.autostart": "true" if autostart else "false",
        "security.privileged": "false",
        "security.nesting": "false",
        MARKER_KEY: marker,
    })
    return {
        "name": name,
        "type": "container",
        "description": description,
        "ephemeral": ephemeral,
        "profiles": ["default"],
        # Images live in the default project (the hsm project has features.images=false).
        "source": {"type": "image", "fingerprint": image_fingerprint},
        "config": config,
        "devices": {
            "root": {"type": "disk", "path": "/", "pool": pool, "size": mib_string(disk_bytes)},
            "eth0": {"type": "nic", "network": network, "name": "eth0"},
        },
    }


def normalize_instance(raw: dict) -> dict:
    cfg = raw.get("config") or {}
    return {
        "project": raw.get("project") or "default",
        "name": raw.get("name"),
        "status": raw.get("status"),
        "type": raw.get("type") or "container",
        "ephemeral": bool(raw.get("ephemeral")),
        "description": raw.get("description") or "",
        "last_used_at": raw.get("last_used_at"),
        "config": cfg,
        "devices": raw.get("devices") or {},
        "expanded_config": raw.get("expanded_config") or {},
        "expanded_devices": raw.get("expanded_devices") or {},
        "volatile_uuid": cfg.get("volatile.uuid"),
        "marker": cfg.get(MARKER_KEY),          # local config only: a profile cannot forge identity
    }


def root_disk_bytes(instance: dict) -> int | None:
    root = (instance.get("expanded_devices") or {}).get("root") or {}
    return parse_size(root.get("size"))


def limits_match(instance: dict, limits: dict) -> bool:
    """True if the live config equals ``limits`` (only keys present are compared)."""
    if not limits:
        return False
    ec = instance.get("expanded_config") or {}
    cores, pct = limits.get("cpu_cores"), limits.get("cpu_allowance_pct")
    if cores is not None and ec.get("limits.cpu") != str(cores):
        return False
    if cores is not None and pct is not None and ec.get("limits.cpu.allowance") != allowance(cores, pct):
        return False
    mem = limits.get("memory_bytes")
    if mem is not None and parse_size(ec.get("limits.memory")) != (mem // MIB) * MIB:
        return False
    disk = limits.get("disk_bytes")
    if disk is not None and root_disk_bytes(instance) != (disk // MIB) * MIB:
        return False
    return True


# ------------------------------------------------------------ error mapping

def _never_sent(exc: BaseException) -> bool:
    """True if the failure happened while connecting, i.e. before any request
    bytes could reach LXD (missing socket, refused, no permission)."""
    try:
        from urllib3.exceptions import NewConnectionError
        from requests.exceptions import ConnectTimeout
        connect_errors: tuple = (FileNotFoundError, ConnectionRefusedError, PermissionError,
                                 NewConnectionError, ConnectTimeout)
    except ImportError:          # pragma: no cover
        connect_errors = (FileNotFoundError, ConnectionRefusedError, PermissionError)
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen or len(seen) > 20:
            continue
        seen.add(id(e))
        if isinstance(e, connect_errors):
            return True
        stack.extend([getattr(e, "__cause__", None), getattr(e, "__context__", None), getattr(e, "reason", None)])
        stack.extend(a for a in getattr(e, "args", ()) if isinstance(a, BaseException))
    return False


def translate(exc: BaseException, what: str) -> LxdError:
    if isinstance(exc, LxdError):
        return exc
    from pylxd import exceptions as px
    if isinstance(exc, px.NotFound):
        return LxdNotFound(f"{what}: not found")
    if isinstance(exc, px.LXDAPIException):
        try:
            detail = str(exc)
        except Exception:        # pragma: no cover - defensive; str() parses the response
            detail = "error response"
        return LxdRejected(f"{what}: {detail[:300]}")
    if isinstance(exc, px.ClientConnectionFailed) or _never_sent(exc):
        return LxdUnavailable(f"{what}: LXD is not reachable ({type(exc).__name__})")
    return LxdUncertain(f"{what}: {type(exc).__name__}: {str(exc)[:200]}")


# ------------------------------------------------------------ adapter

class LxdAdapter:
    """Typed operations on LXD. Thread-safe: pylxd clients are per thread."""

    def __init__(self, cfg: Config, *, client_factory: Callable[..., Any] | None = None):
        self.cfg = cfg
        self.endpoint = "http+unix://" + quote(cfg.lxd_socket, safe="")
        self._client_factory = client_factory
        self._local = threading.local()
        # Exec needs an HTTP read timeout longer than our own deadline handling.
        self._exec_http_timeout = (cfg.exec_deadline_seconds + lxd_exec.OVERRUN_SECONDS
                                   + 2 * lxd_exec.KILL_GRACE_SECONDS + 15)

    # ---- clients (lazy, so the worker starts and heartbeats while LXD is down)
    def _client(self, project: str | None = None, timeout: float | None = None):
        key = (project, timeout or self.cfg.lxd_timeout_seconds)
        cache = getattr(self._local, "clients", None)
        if cache is None:
            cache = self._local.clients = {}
        if key not in cache:
            try:
                if self._client_factory is not None:
                    cache[key] = self._client_factory(endpoint=self.endpoint, project=project, timeout=key[1])
                else:
                    from pylxd import Client
                    cache[key] = Client(endpoint=self.endpoint, project=project, timeout=key[1])
            except Exception as exc:
                raise translate(exc, "connect") from None
        return cache[key]

    @property
    def api(self):
        return self._client().api

    @staticmethod
    def _p(project: str) -> dict:
        if not PROJECT_RE.match(project or ""):
            raise AdapterRefused("Invalid project name.")
        return {"project": project}

    @staticmethod
    def _n(name: str) -> str:
        if not EXISTING_NAME_RE.match(name or ""):
            raise AdapterRefused("Invalid instance name.")
        return name

    def _read(self, what: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            raise translate(exc, what) from None

    def _mutate(self, what: str, send: Callable[[], Any], wait_seconds: int) -> dict:
        """Send a mutating request; await its background operation."""
        try:
            resp = send()
        except Exception as exc:
            raise translate(exc, what) from None
        try:
            body = resp.json()
        except ValueError:
            raise LxdUncertain(f"{what}: unreadable response") from None
        if body.get("type") == "async" and body.get("operation"):
            return self._wait(what, body["operation"], wait_seconds)
        return body.get("metadata") or {}

    def _wait(self, what: str, operation_url: str, seconds: int) -> dict:
        op_id = posixpath.basename(urlparse(operation_url).path)
        try:
            resp = self.api.operations[op_id].wait.get(params={"timeout": seconds}, timeout=seconds + 10)
            meta = resp.json().get("metadata") or {}
        except Exception as exc:
            # The change was already submitted: any failure to observe it is uncertain.
            raise LxdUncertain(f"{what}: could not await operation ({translate(exc, what).message})") from None
        code = meta.get("status_code")
        if code == 200:
            return meta
        if isinstance(code, int) and code >= 400:
            raise LxdRejected(f"{what}: {(meta.get('err') or meta.get('status') or 'failed')[:300]}")
        raise LxdUncertain(f"{what}: still {meta.get('status') or 'running'} after {seconds}s")

    # ---- reads
    def list_instances(self) -> list[dict]:
        resp = self._read("list instances", lambda: self.api.instances.get(
            params={"recursion": 1, "all-projects": "true"}))
        return [normalize_instance(i) for i in resp.json().get("metadata") or [] if isinstance(i, dict)]

    def find_by_marker(self, marker: str) -> dict | None:
        """The one instance whose local config carries ``user.hsm.id=marker``.
        A complete listing is required, so absence here is real evidence."""
        hits = [i for i in self.list_instances() if marker and i["marker"] == marker]
        if len(hits) > 1:
            raise AmbiguousIdentity(f"{len(hits)} instances carry marker {marker}: "
                                    + ", ".join(f"{i['project']}/{i['name']}" for i in hits))
        return hits[0] if hits else None

    def get_instance(self, project: str, name: str) -> dict | None:
        params, name = self._p(project), self._n(name)
        try:
            resp = self._read("get instance", lambda: self.api.instances[name].get(params=params))
        except LxdNotFound:
            return None
        meta = resp.json().get("metadata") or {}
        meta.setdefault("project", project)
        return normalize_instance(meta)

    def get_state(self, project: str, name: str) -> dict:
        params, name = self._p(project), self._n(name)
        resp = self._read("get state", lambda: self.api.instances[name].state.get(params=params))
        return resp.json().get("metadata") or {}

    def pool_free_bytes(self, pool: str) -> int:
        if pool not in self.cfg.allowed_pools:
            raise AdapterRefused(f"Pool '{pool}' is not allowed.")
        resp = self._read("pool resources", lambda: self.api.storage_pools[pool].resources.get())
        space = (resp.json().get("metadata") or {}).get("space") or {}
        return max(0, int(space.get("total") or 0) - int(space.get("used") or 0))

    # ---- mutations
    def create(self, project: str, name: str, *, image_fingerprint: str, pool: str, network: str,
               cpu_cores: int, cpu_allowance_pct: int, memory_bytes: int, disk_bytes: int,
               ephemeral: bool, autostart: bool, description: str, marker: str) -> None:
        if project != self.cfg.lxd_project:
            raise AdapterRefused("New containers are only created in the configured project.")
        body = build_create_body(self.cfg, name, image_fingerprint=image_fingerprint, pool=pool, network=network,
                                 cpu_cores=cpu_cores, cpu_allowance_pct=cpu_allowance_pct,
                                 memory_bytes=memory_bytes, disk_bytes=disk_bytes, ephemeral=ephemeral,
                                 autostart=autostart, description=description, marker=marker)
        params = self._p(project)
        self._mutate("create", lambda: self.api.instances.post(json=body, params=params), CREATE_WAIT)

    def _state(self, project: str, name: str, action: str, *, force: bool = False) -> None:
        params, name = self._p(project), self._n(name)
        body = {"action": action, "timeout": STOP_TIMEOUT, "force": bool(force)}
        self._mutate(action, lambda: self.api.instances[name].state.put(json=body, params=params),
                     STATE_WAIT + (STOP_TIMEOUT if action in ("stop", "restart") else 0))

    def start(self, project: str, name: str) -> None:
        self._state(project, name, "start")

    def stop(self, project: str, name: str, *, force: bool = False) -> None:
        self._state(project, name, "stop", force=force)

    def restart(self, project: str, name: str, *, force: bool = False) -> None:
        self._state(project, name, "restart", force=force)

    def freeze(self, project: str, name: str) -> None:
        self._state(project, name, "freeze")

    def unfreeze(self, project: str, name: str) -> None:
        self._state(project, name, "unfreeze")

    def update_limits(self, project: str, name: str, *, cpu_cores: int, cpu_allowance_pct: int,
                      memory_bytes: int, disk_bytes: int, marker: str | None = None) -> None:
        """Apply CPU/RAM live and expand (never shrink) the root disk. With
        ``marker`` it also writes the identity marker (adoption) in the same PATCH."""
        _check_limits(cpu_cores, cpu_allowance_pct, memory_bytes, disk_bytes)
        current = self.get_instance(project, name)
        if current is None:
            raise LxdNotFound("update limits: instance not found")
        root = dict((current["expanded_devices"] or {}).get("root") or {})
        if root.get("type") != "disk" or root.get("path") != "/" or root.get("pool") not in self.cfg.allowed_pools:
            raise AdapterRefused("The root disk is not on an allowed pool.")
        live = parse_size(root.get("size"))
        if live is not None and (disk_bytes // MIB) * MIB < live:
            raise AdapterRefused("Disk can only be expanded, not shrunk.", code="DISK_SHRINK")
        config = limit_config(self.cfg, cpu_cores=cpu_cores, cpu_allowance_pct=cpu_allowance_pct,
                              memory_bytes=memory_bytes)
        if marker is not None:
            if not MARKER_RE.match(marker):
                raise AdapterRefused("Invalid identity marker.")
            config[MARKER_KEY] = marker
        root["size"] = mib_string(disk_bytes)
        # Send every local device (plus the root override) so the result is the
        # same whether LXD merges or replaces the devices map on PATCH.
        devices = dict(current["devices"] or {})
        devices["root"] = root
        params, name = self._p(project), self._n(name)
        self._mutate("update limits", lambda: self.api.instances[name].patch(
            json={"config": config, "devices": devices}, params=params), UPDATE_WAIT)

    def delete(self, project: str, name: str) -> None:
        params, name = self._p(project), self._n(name)
        self._mutate("delete", lambda: self.api.instances[name].delete(params=params), DELETE_WAIT)

    # ---- exec
    def _executor(self, project: str, name: str) -> lxd_exec.ExecuteFn:
        params, name = self._p(project), self._n(name)
        client = self._client(params["project"], timeout=self._exec_http_timeout)
        from pylxd import exceptions as px
        from pylxd.models import Instance
        instance = Instance(client, name=name)

        def execute(argv, ident, on_stdout, on_stderr) -> int:
            try:
                res = instance.execute(argv, environment=ident.environment(), decode=False,
                                       stdout_handler=on_stdout, stderr_handler=on_stderr,
                                       user=ident.uid, group=ident.gid, cwd=ident.cwd)
            except px.LXDAPIException as exc:
                # The exec POST was refused, or the exec operation failed to
                # start the process: in both cases the command did not run.
                raise translate(exc, "exec") from None
            return res.exit_code
        return execute

    def exec_command(self, project: str, name: str, command: str, *, as_root: bool) -> dict:
        if not isinstance(command, str) or "\x00" in command or not command.strip():
            raise AdapterRefused("Invalid command.")
        if len(command.encode("utf-8")) > self.cfg.exec_max_command_bytes:
            raise AdapterRefused("Command is too long.")
        execute = self._executor(project, name)
        return lxd_exec.run_command(execute, command, as_root=bool(as_root),
                                    deadline_seconds=self.cfg.exec_deadline_seconds,
                                    max_output_bytes=self.cfg.exec_max_output_bytes)

    def provision_guest_user(self, project: str, name: str) -> None:
        """Create the unprivileged ``hsm`` account (uid/gid 1500, no sudo)."""
        execute = self._executor(project, name)
        res = lxd_exec.run_command(execute, PROVISION_SCRIPT, as_root=True, deadline_seconds=PROVISION_DEADLINE,
                                   max_output_bytes=4096)
        if res["outcome"] == "unknown":
            raise LxdUncertain("provision guest user: outcome unknown")
        if res["outcome"] != "completed" or res["exit_code"] != 0:
            raise LxdRejected(f"provision guest user failed (exit {res['exit_code']}): {res['stderr'][:200]}")
