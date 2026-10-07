"""Container inventory views and validated mutation requests.

The API never talks to LXD: creation options come from the capability snapshot
the collector publishes, and every mutation becomes a durable operation that
the worker re-validates against live LXD state before dispatch.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from ..auth.sessions import Principal
from ..config import Config
from ..errors import Conflict, Invalid
from ..security import new_id
from ..timeutil import parse, utcnow, utcnow_iso
from . import operations, quotas, state

# LXD instance names: 1-63 chars, letters/digits/hyphens, no leading digit/hyphen,
# no trailing hyphen. The brief further restricts to lowercase.
NAME_RULE = r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$"
NAME_RE = re.compile(NAME_RULE)
MIB = quotas.MIB
LIFECYCLE = ("start", "stop", "restart", "freeze", "unfreeze")
VALID_FROM = {"start": {"Stopped"}, "stop": {"Running", "Frozen"}, "restart": {"Running"},
              "freeze": {"Running"}, "unfreeze": {"Frozen"}}
MEMORY_HEADROOM = 64 * MIB


# ------------------------------------------------------------------ reading

def _metrics_dict(m: sqlite3.Row | None, cfg: Config) -> dict | None:
    if m is None:
        return None
    sampled = parse(m["sampled_at"])
    age = int((utcnow() - sampled).total_seconds())
    started = parse(m["started_at"]) if m["started_at"] else None
    return {
        "state": m["state"], "cpu_pct": m["cpu_pct"], "memory_bytes": m["memory_bytes"],
        "memory_limit_bytes": m["memory_limit_bytes"], "disk_bytes": m["disk_bytes"],
        "disk_limit_bytes": m["disk_limit_bytes"], "rx_bytes": m["rx_bytes"], "tx_bytes": m["tx_bytes"],
        "rx_rate": m["rx_rate"], "tx_rate": m["tx_rate"], "processes": m["processes"], "ipv4": m["ipv4"],
        "started_at": m["started_at"],
        "uptime_seconds": int((utcnow() - started).total_seconds()) if started and m["state"] in ("Running", "Frozen") else None,
        "sampled_at": m["sampled_at"], "age_seconds": age, "stale": age > 3 * cfg.collect_interval_seconds,
        "quality": json.loads(m["quality"] or "[]"),
    }


def serialize(conn: sqlite3.Connection, cfg: Config, c: sqlite3.Row, *, owner_email: str | None = None,
              metrics: sqlite3.Row | None = None, active_op: sqlite3.Row | None = None) -> dict:
    return {
        "id": c["id"], "name": c["name"], "project": c["project"], "instance_type": c["instance_type"],
        "managed": bool(c["managed"]), "status": c["status"], "safety": c["safety"],
        "safety_reasons": json.loads(c["safety_reasons"] or "[]"),
        "owner": {"id": c["owner_id"], "email": owner_email} if c["owner_id"] else None,
        "limits": {"cpu_cores": c["cpu_cores"], "cpu_allowance_pct": c["cpu_allowance_pct"],
                   "memory_bytes": c["memory_bytes"], "disk_bytes": c["disk_bytes"], "pool": c["pool"]},
        "observed_limits": {"cpu_cores": c["observed_cpu_cores"], "memory_bytes": c["observed_memory_bytes"],
                            "disk_bytes": c["observed_disk_bytes"], "pool": c["observed_pool"]},
        "image_description": c["image_description"], "os": c["os"], "architecture": c["architecture"],
        "ephemeral": bool(c["ephemeral"]), "autostart": bool(c["autostart"]), "description": c["description"],
        "version": c["version"], "created_at": c["created_at"], "last_seen_at": c["last_seen_at"],
        "metrics": _metrics_dict(metrics, cfg),
        "active_operation": {"id": active_op["id"], "kind": active_op["kind"], "state": active_op["state"]}
        if active_op else None,
    }


_LIST_SQL = """
SELECT c.*, u.email AS owner_email, m.container_id AS m_cid, m.state AS m_state, m.cpu_pct, m.memory_bytes AS m_memory_bytes,
       m.memory_limit_bytes, m.disk_bytes AS m_disk_bytes, m.disk_limit_bytes, m.rx_bytes, m.tx_bytes, m.rx_rate,
       m.tx_rate, m.processes, m.ipv4, m.started_at, m.sampled_at, m.quality,
       o.id AS op_id, o.kind AS op_kind, o.state AS op_state
FROM containers c
LEFT JOIN users u ON u.id = c.owner_id
LEFT JOIN latest_metrics m ON m.container_id = c.id
LEFT JOIN operations o ON o.id = (SELECT id FROM operations WHERE container_id = c.id
                                  AND state IN ('queued','running','reconciling') ORDER BY created_at LIMIT 1)
WHERE c.status != 'deleted' {filter}
ORDER BY c.name
"""


class _MetricsView:
    """Adapts a joined row to the latest_metrics column names."""

    def __init__(self, row):
        self.r = row

    def __getitem__(self, key):
        alias = {"state": "m_state", "memory_bytes": "m_memory_bytes", "disk_bytes": "m_disk_bytes"}
        return self.r[alias.get(key, key)]


def list_for(conn: sqlite3.Connection, cfg: Config, principal: Principal) -> list[dict]:
    if principal.is_admin:
        rows = conn.execute(_LIST_SQL.format(filter=""))
    else:   # filtering happens in SQL, not after loading everything
        rows = conn.execute(_LIST_SQL.format(
            filter="AND c.id IN (SELECT container_id FROM container_access WHERE user_id = ?)"),
            (principal.user_id,))
    out = []
    for r in rows:
        op = {"id": r["op_id"], "kind": r["op_kind"], "state": r["op_state"]} if r["op_id"] else None
        out.append(serialize(conn, cfg, r, owner_email=r["owner_email"],
                             metrics=_MetricsView(r) if r["m_cid"] else None, active_op=op))
    return out


def detail(conn: sqlite3.Connection, cfg: Config, principal: Principal, c: sqlite3.Row) -> dict:
    owner = conn.execute("SELECT email FROM users WHERE id = ?", (c["owner_id"],)).fetchone() if c["owner_id"] else None
    metrics = conn.execute("SELECT * FROM latest_metrics WHERE container_id = ?", (c["id"],)).fetchone()
    out = serialize(conn, cfg, c, owner_email=owner["email"] if owner else None, metrics=metrics,
                    active_op=operations.active_for_container(conn, c["id"]))
    can_exec = bool(c["managed"]) and c["status"] == "active" and c["safety"] == "safe"
    out["capabilities"] = {"can_manage": principal.is_admin and bool(c["managed"]) and c["status"] == "active",
                           "can_exec": can_exec, "exec_as": "root" if principal.is_admin else "hsm"}
    if principal.is_admin and c["managed"] and c["status"] == "active" and c["owner_id"]:
        # Live ranges for the limits form; the server re-validates on submit anyway.
        out["limit_bounds"] = quotas.bounds(
            conn, cfg, c["owner_id"], current=quotas.Res(c["cpu_cores"], c["memory_bytes"], c["disk_bytes"]),
            pool=c["pool"])
    if principal.is_admin:
        out["access"] = [dict(r) for r in conn.execute(
            "SELECT a.user_id, u.email FROM container_access a JOIN users u ON u.id = a.user_id"
            " WHERE a.container_id = ? ORDER BY u.email", (c["id"],))]
    return out


def collector_status(conn: sqlite3.Connection, cfg: Config) -> dict:
    hb, _ = state.get_with_time(conn, state.COLLECTOR_HEARTBEAT)
    if not hb or not hb.get("last_success_at"):
        return {"last_success_at": None, "age_seconds": None, "stale": True,
                "lxd_available": bool(hb and hb.get("lxd_available"))}
    age = int((utcnow() - parse(hb["last_success_at"])).total_seconds())
    return {"last_success_at": hb["last_success_at"], "age_seconds": age,
            "stale": age > 3 * cfg.collect_interval_seconds, "lxd_available": bool(hb.get("lxd_available"))}


# ------------------------------------------------------------- create options

def creation_options(conn: sqlite3.Connection, cfg: Config, owner_id: str | None) -> dict:
    caps = state.get(conn, state.LXD_CAPABILITIES) or {}
    images = [i for i in caps.get("images") or [] if str(i.get("alias", "")).startswith(cfg.image_alias_prefix)]
    pools = [p for p in caps.get("pools") or [] if p["name"] in cfg.allowed_pools and p.get("quota_capable")]
    networks = [{"name": n["name"]} for n in caps.get("networks") or [] if n["name"] in cfg.allowed_networks]
    owners = [dict(r) for r in conn.execute(
        "SELECT id, email, role, status FROM users WHERE status IN ('active', 'pending') ORDER BY email")]
    bounds = quotas.bounds(conn, cfg, owner_id) if owner_id else None
    version_src = json.dumps([caps.get("observed_at"), bounds], sort_keys=True)
    return {"options_version": hashlib.sha256(version_src.encode()).hexdigest()[:16],
            "images": images, "pools": pools, "networks": networks, "owners": owners,
            "bounds": bounds, "name_rule": NAME_RULE,
            # Lets the form say *which* limit applies (owner quota vs host capacity).
            "owner_quota": quotas.owner_summary(conn, owner_id) if owner_id else None,
            "project": cfg.lxd_project, "capabilities_observed_at": caps.get("observed_at")}


def _strict(body, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(body, dict):
        raise Invalid("Request body must be a JSON object.")
    unknown = set(body) - allowed
    if unknown:
        raise Invalid("Unknown fields: " + ", ".join(sorted(unknown)),
                      fields={k: "not allowed" for k in sorted(unknown)})
    missing = required - set(body)
    if missing:
        raise Invalid("Missing fields: " + ", ".join(sorted(missing)),
                      fields={k: "required" for k in sorted(missing)})
    return body


def _int(body: dict, key: str, lo: int, hi: int, step: int = 1) -> int:
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise Invalid(f"{key} must be an integer.", fields={key: "must be an integer"})
    if hi < lo:
        raise Conflict(f"No {key.replace('_', ' ')} remain within the owner's quota and host capacity.",
                       code="OUT_OF_BOUNDS", fields={key: "nothing available"})
    if v < lo or v > hi:
        raise Conflict(f"{key} must be between {lo} and {hi}.", code="OUT_OF_BOUNDS",
                       fields={key: f"allowed range {lo}..{hi}"})
    if v % step:
        raise Invalid(f"{key} must be a multiple of {step}.", fields={key: f"multiple of {step}"})
    return v


def _bool(body: dict, key: str, default=False) -> bool:
    v = body.get(key, default)
    if not isinstance(v, bool):
        raise Invalid(f"{key} must be true or false.", fields={key: "boolean"})
    return v


def submit_create(conn: sqlite3.Connection, cfg: Config, principal: Principal, body, idem_key: str,
                  request_id: str) -> tuple[sqlite3.Row, bool, str]:
    fields = {"name", "image", "pool", "network", "cpu_cores", "cpu_allowance_pct", "memory_bytes", "disk_bytes",
              "owner_id", "ephemeral", "autostart", "description", "options_version"}
    replay = operations.find_replay(conn, principal.user_id, idem_key, "create", None, body)
    if replay is not None:
        return replay, False, replay["container_id"]
    body = _strict(body, fields, fields - {"ephemeral", "autostart", "description", "options_version"})
    name = body["name"]
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise Invalid("Name must be 2-63 lowercase letters, digits or hyphens, start with a letter and not end "
                      "with a hyphen.", fields={"name": "invalid"})
    desc = body.get("description", "")
    if not isinstance(desc, str) or len(desc) > 255 or any(ord(ch) < 32 for ch in desc):
        raise Invalid("Description must be at most 255 printable characters.", fields={"description": "invalid"})
    for key in ("image", "pool", "network", "owner_id"):
        if not isinstance(body[key], str):
            raise Invalid(f"{key} must be a string.", fields={key: "invalid"})
    owner_id = body["owner_id"]
    owner = conn.execute("SELECT * FROM users WHERE id = ? AND status IN ('active','pending')",
                         (owner_id,)).fetchone()
    if owner is None:
        raise Invalid("Owner must be an active or invited user.", fields={"owner_id": "invalid"})
    opts = creation_options(conn, cfg, owner_id)
    image = next((i for i in opts["images"] if i["alias"] == body["image"]), None)
    if image is None:
        raise Invalid("Choose one of the offered images.", fields={"image": "not offered"})
    if body["pool"] not in {p["name"] for p in opts["pools"]}:
        raise Invalid("Choose one of the offered storage pools.", fields={"pool": "not offered"})
    if body["network"] not in {n["name"] for n in opts["networks"]}:
        raise Invalid("Choose one of the offered networks.", fields={"network": "not offered"})
    b = opts["bounds"]
    if b["blocked_reason"]:
        raise Conflict(b["blocked_reason"], code="ACCOUNTING_INCOMPLETE")
    disk_b = b["disk_bytes"].get(body["pool"]) or {"min": 0, "max": 0, "step": MIB}
    try:
        cpu = _int(body, "cpu_cores", b["cpu_cores"]["min"], b["cpu_cores"]["max"])
        pct = _int(body, "cpu_allowance_pct", b["cpu_allowance_pct"]["min"], 100)
        mem = _int(body, "memory_bytes", b["memory_bytes"]["min"], b["memory_bytes"]["max"], MIB)
        disk = _int(body, "disk_bytes", disk_b["min"], disk_b["max"], MIB)
    except Conflict as exc:
        exc.extra["bounds"] = b
        raise
    ephemeral, autostart = _bool(body, "ephemeral"), _bool(body, "autostart")
    if conn.execute("SELECT 1 FROM containers WHERE project = ? AND name = ? AND status != 'deleted'",
                    (cfg.lxd_project, name)).fetchone():
        raise Conflict("A container with this name already exists.", code="NAME_TAKEN", fields={"name": "taken"})

    container_id = new_id()
    payload = {"name": name, "image_alias": image["alias"], "image_fingerprint": image["fingerprint"],
               "pool": body["pool"], "network": body["network"], "cpu_cores": cpu, "cpu_allowance_pct": pct,
               "memory_bytes": mem, "disk_bytes": disk, "owner_id": owner_id, "ephemeral": ephemeral,
               "autostart": autostart, "description": desc}

    def insert_row(c: sqlite3.Connection) -> None:
        # Re-check the name inside the write transaction (the check above was a fast path).
        if c.execute("SELECT 1 FROM containers WHERE project = ? AND name = ? AND status != 'deleted'",
                     (cfg.lxd_project, name)).fetchone():
            raise Conflict("A container with this name already exists.", code="NAME_TAKEN", fields={"name": "taken"})
        c.execute(
            "INSERT INTO containers(id, project, name, lxd_marker, managed, status, safety, owner_id, pool,"
            " image_description, ephemeral, autostart, description, created_at)"
            " VALUES (?, ?, ?, ?, 1, 'creating', 'unknown', ?, ?, ?, ?, ?, ?, ?)",
            (container_id, cfg.lxd_project, name, container_id, owner_id, body["pool"],
             image.get("description", ""), int(ephemeral), int(autostart), desc, utcnow_iso()))
        if owner["role"] != "admin":
            # The owner pays for the container, so by default they can also see and use it.
            # This is an ordinary grant: the admin can remove it like any other.
            c.execute("INSERT INTO container_access(container_id, user_id, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                      (container_id, owner_id, principal.user_id, utcnow_iso()))

    try:
        op, created = operations.submit(
            conn, cfg, actor_id=principal.user_id, kind="create", container_id=container_id, payload=payload,
            idempotency_key=idem_key, request_body=body, request_id=request_id, target_label=name,
            audit_details={"owner": owner["email"], "limits": {k: payload[k] for k in
                           ("cpu_cores", "cpu_allowance_pct", "memory_bytes", "disk_bytes", "pool")}},
            before_insert=insert_row,
            reserve=(owner_id, body["pool"], quotas.Res(cpu, mem, disk)))
    except Conflict as exc:
        if exc.code in ("QUOTA_EXCEEDED", "HOST_CAPACITY"):
            exc.extra["bounds"] = quotas.bounds(conn, cfg, owner_id)
        raise
    return op, created, op["container_id"]


def _unchanged(c: sqlite3.Row):
    """before_insert hook: the row the request was validated against must still be
    current *inside* the write transaction (version, owner and status), otherwise the
    reservation could be computed from stale limits or charged to the wrong owner."""
    def check(conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT version, owner_id, status, managed FROM containers WHERE id = ?",
                           (c["id"],)).fetchone()
        if row is None or (row["version"], row["owner_id"], row["status"], row["managed"]) !=                 (c["version"], c["owner_id"], c["status"], c["managed"]):
            raise Conflict("The container changed while this request was processed; reload and retry.",
                           code="VERSION_CONFLICT")
    return check


def _require_managed(c: sqlite3.Row) -> None:
    if not c["managed"] or c["status"] != "active":
        raise Conflict("Only active, app-managed containers can be changed here.", code="NOT_MANAGED")


def _observed_state(conn, container_id: str) -> str | None:
    r = conn.execute("SELECT state FROM latest_metrics WHERE container_id = ?", (container_id,)).fetchone()
    return r["state"] if r else None


def submit_limits(conn: sqlite3.Connection, cfg: Config, principal: Principal, c: sqlite3.Row, body,
                  idem_key: str, request_id: str) -> tuple[sqlite3.Row, bool]:
    replay = operations.find_replay(conn, principal.user_id, idem_key, "update_limits", c["id"], body)
    if replay is not None:
        return replay, False
    _require_managed(c)
    keys = {"cpu_cores", "cpu_allowance_pct", "memory_bytes", "disk_bytes"}
    body = _strict(body, keys | {"version", "confirm_reduction"}, {"version"})
    if body["version"] != c["version"]:
        raise Conflict("The container changed since you loaded it; reload and retry.", code="VERSION_CONFLICT")
    before = {"cpu_cores": c["cpu_cores"], "cpu_allowance_pct": c["cpu_allowance_pct"],
              "memory_bytes": c["memory_bytes"], "disk_bytes": c["disk_bytes"]}
    current = quotas.Res(c["cpu_cores"], c["memory_bytes"], c["disk_bytes"])
    b = quotas.bounds(conn, cfg, c["owner_id"], current=current, pool=c["pool"])
    after = dict(before)
    try:
        if "cpu_cores" in body:
            after["cpu_cores"] = _int(body, "cpu_cores", 1, b["cpu_cores"]["max"])
        if "cpu_allowance_pct" in body:
            after["cpu_allowance_pct"] = _int(body, "cpu_allowance_pct", quotas.MIN_ALLOWANCE_PCT, 100)
        if "memory_bytes" in body:
            after["memory_bytes"] = _int(body, "memory_bytes", quotas.MIN_MEMORY_BYTES, b["memory_bytes"]["max"], MIB)
        if "disk_bytes" in body:
            disk_b = b["disk_bytes"].get(c["pool"]) or {"max": c["disk_bytes"]}
            after["disk_bytes"] = _int(body, "disk_bytes", c["disk_bytes"], disk_b["max"], MIB)
    except Conflict as exc:
        exc.extra["bounds"] = b
        raise
    if after == before:
        raise Invalid("No limits changed.")
    if after["memory_bytes"] < before["memory_bytes"]:
        if not _bool(body, "confirm_reduction"):
            raise Conflict("Reducing memory on a running container can disrupt it; confirm to proceed.",
                           code="CONFIRMATION_REQUIRED", fields={"memory_bytes": "confirm_reduction required"})
        m = conn.execute("SELECT memory_bytes, sampled_at FROM latest_metrics WHERE container_id = ?",
                         (c["id"],)).fetchone()
        if m is None or m["memory_bytes"] is None:
            raise Conflict("Current memory use is unknown, so a reduction cannot be checked.",
                           code="USAGE_UNKNOWN", fields={"memory_bytes": "usage unknown"})
        if after["memory_bytes"] < m["memory_bytes"] + MEMORY_HEADROOM:
            raise Conflict("The new limit is below current use plus 64 MiB headroom.", code="BELOW_USAGE",
                           fields={"memory_bytes": f"currently using {m['memory_bytes'] // MIB} MiB"})
    delta = quotas.Res(after["cpu_cores"], after["memory_bytes"], after["disk_bytes"]) - current
    op, created = operations.submit(
        conn, cfg, actor_id=principal.user_id, kind="update_limits", container_id=c["id"],
        payload={"before": before, "after": after}, idempotency_key=idem_key, request_body=body,
        request_id=request_id,
        target_label=c["name"], audit_details={"before": before, "after": after}, before_insert=_unchanged(c),
        reserve=(c["owner_id"], c["pool"], delta.positive()))
    return op, created


def submit_action(conn: sqlite3.Connection, cfg: Config, principal: Principal, c: sqlite3.Row, body,
                  idem_key: str, request_id: str) -> tuple[sqlite3.Row, bool]:
    replay = operations.find_replay(conn, principal.user_id, idem_key, "action", c["id"], body)
    if replay is not None:
        return replay, False
    _require_managed(c)
    body = _strict(body, {"action", "confirm_ephemeral"}, {"action"})
    action = body["action"]
    if action not in LIFECYCLE:
        raise Invalid("Unknown action.", fields={"action": "one of " + ", ".join(LIFECYCLE)})
    observed = _observed_state(conn, c["id"])
    if observed not in VALID_FROM[action]:
        raise Conflict(f"Cannot {action} a container that is {observed or 'in an unknown state'}.",
                       code="INVALID_STATE")
    if action == "stop" and c["ephemeral"] and not _bool(body, "confirm_ephemeral"):
        raise Conflict("Stopping an ephemeral container deletes it; confirm to proceed.",
                       code="CONFIRMATION_REQUIRED", fields={"confirm_ephemeral": "required"})
    payload = {"force": False} if action == "stop" else {}
    return operations.submit(conn, cfg, actor_id=principal.user_id, kind=action, container_id=c["id"],
                             payload=payload, idempotency_key=idem_key, request_body=body, hash_scope="action",
                             request_id=request_id, target_label=c["name"], audit_details={"from_state": observed},
                             before_insert=_unchanged(c))


def submit_delete(conn: sqlite3.Connection, cfg: Config, principal: Principal, c: sqlite3.Row, confirm_name: str | None,
                  idem_key: str, request_id: str) -> tuple[sqlite3.Row, bool]:
    request_body = {"confirm_name": confirm_name}
    replay = operations.find_replay(conn, principal.user_id, idem_key, "delete", c["id"], request_body)
    if replay is not None:
        return replay, False
    _require_managed(c)
    if confirm_name != c["name"]:
        raise Conflict("Type the container's name to confirm deletion.", code="CONFIRMATION_REQUIRED",
                       fields={"confirm_name": "must equal the container name"})
    return operations.submit(conn, cfg, actor_id=principal.user_id, kind="delete", container_id=c["id"],
                             payload={"name": c["name"]}, idempotency_key=idem_key, request_body=request_body,
                             request_id=request_id, before_insert=_unchanged(c),
                             target_label=c["name"], audit_details={
                                 "limits": {"cpu_cores": c["cpu_cores"], "memory_bytes": c["memory_bytes"],
                                            "disk_bytes": c["disk_bytes"]}})


def submit_exec(conn: sqlite3.Connection, cfg: Config, principal: Principal, c: sqlite3.Row, body,
                idem_key: str, request_id: str) -> tuple[sqlite3.Row, bool]:
    # An exec replay returns the original operation; a command is never re-run.
    replay = operations.find_replay(conn, principal.user_id, idem_key, "exec", c["id"], body)
    if replay is not None:
        return replay, False
    body = _strict(body, {"command"}, {"command"})
    command = body["command"]
    if not isinstance(command, str) or not command.strip():
        raise Invalid("Enter a command.", fields={"command": "required"})
    if "\x00" in command:
        raise Invalid("Commands cannot contain NUL bytes.", fields={"command": "invalid"})
    if len(command.encode("utf-8")) > cfg.exec_max_command_bytes:
        raise Invalid(f"Commands are limited to {cfg.exec_max_command_bytes} bytes.", fields={"command": "too long"})
    if _observed_state(conn, c["id"]) != "Running":
        raise Conflict("The container must be running to execute commands.", code="NOT_RUNNING")
    return operations.submit(conn, cfg, actor_id=principal.user_id, kind="exec", container_id=c["id"],
                             payload={"command": command, "as_root": principal.is_admin},
                             idempotency_key=idem_key, request_body=body, request_id=request_id,
                             target_label=c["name"])


def submit_adopt(conn: sqlite3.Connection, cfg: Config, principal: Principal, c: sqlite3.Row, body,
                 idem_key: str, request_id: str) -> tuple[sqlite3.Row, bool]:
    """Bring an existing, *safe* unmanaged container under management with explicit
    limits and an owner. Unsafe containers need manual remediation first; the app
    never "fixes" someone else's workload. The worker re-checks everything live."""
    replay = operations.find_replay(conn, principal.user_id, idem_key, "adopt", c["id"], body)
    if replay is not None:
        return replay, False
    if c["managed"] or c["status"] != "active":
        raise Conflict("Only active, unmanaged containers can be adopted.", code="ALREADY_MANAGED")
    if c["instance_type"] != "container":
        raise Conflict("Virtual machines are out of scope.", code="NOT_SUPPORTED")
    if c["safety"] != "safe":
        raise Conflict("This container needs manual remediation before adoption: "
                       + "; ".join(json.loads(c["safety_reasons"] or "[]")), code="CONTAINER_UNSAFE")
    keys = {"owner_id", "cpu_cores", "cpu_allowance_pct", "memory_bytes", "disk_bytes"}
    body = _strict(body, keys, keys)
    owner = conn.execute("SELECT * FROM users WHERE id = ? AND status IN ('active','pending')",
                         (body["owner_id"],)).fetchone() if isinstance(body["owner_id"], str) else None
    if owner is None:
        raise Invalid("Owner must be an active or invited user.", fields={"owner_id": "invalid"})
    pool = c["observed_pool"]
    if pool not in cfg.allowed_pools:
        raise Conflict("The root disk is not on an allowed storage pool.", code="CONTAINER_UNSAFE")
    cpu = _int(body, "cpu_cores", 1, 1024)
    pct = _int(body, "cpu_allowance_pct", quotas.MIN_ALLOWANCE_PCT, 100)
    mem = _int(body, "memory_bytes", quotas.MIN_MEMORY_BYTES, 1 << 50, MIB)
    disk = _int(body, "disk_bytes", max(quotas.MIN_DISK_BYTES, c["observed_disk_bytes"] or 0), 1 << 55, MIB)
    payload = {"owner_id": owner["id"], "cpu_cores": cpu, "cpu_allowance_pct": pct, "memory_bytes": mem,
               "disk_bytes": disk}
    # The full allocation is reserved against the owner and the host. While the job is pending the
    # container's observed limits are also counted for the host, which errs on the safe side.
    return operations.submit(conn, cfg, actor_id=principal.user_id, kind="adopt", container_id=c["id"],
                             payload=payload, idempotency_key=idem_key, request_body=body, request_id=request_id,
                             target_label=c["name"], audit_details={"owner": owner["email"], "limits": payload},
                             before_insert=_unchanged(c), reserve=(owner["id"], pool, quotas.Res(cpu, mem, disk)))
