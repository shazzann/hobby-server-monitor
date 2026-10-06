"""Reconcile the LXD instance listing into the ``containers`` table.

Identity rules (plan section 7, contract "Internal: collector -> SQLite"):

* A managed row is matched by its marker: ``expanded_config['user.hsm.id']``.
  The name/project follow the instance (identity-preserving rename), so grants
  and history stay attached to the same application UUID.
* An instance without a known marker is matched by (project, name) among
  *unmanaged* rows only; otherwise it becomes a new unmanaged row with a fresh
  UUID. Grants are never transferred by name.
* Duplicate markers (an instance copy): only the instance whose ``volatile.uuid``
  equals the row's ``lxd_volatile_uuid`` keeps the row; if none matches the row
  is quarantined. Other carriers of the marker get no row at all.
* A row missing from the listing is tombstoned only after a complete successful
  listing and only when no operation is active for it. ``creating`` rows (and
  ``failed`` creates) belong to the worker and are never touched.

The collector writes only: name, project, observed_*, image_description, os,
architecture, ephemeral, autostart, safety, safety_reasons, last_seen_at, new
unmanaged rows, status 'quarantined', and 'deleted' tombstones. Never owner_id,
committed limits or reservations.

Everything happens inside one short ``BEGIN IMMEDIATE`` transaction so the
decision is made on a consistent snapshot (a worker cannot insert or change a
row between our read and our write). No LXD call happens inside it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from .. import lxd_safety
from ..db import write_tx
from ..security import new_id
from ..services import audit
from .normalize import cpu_count_limit, parse_bytes, root_device

log = logging.getLogger("hsm.collector.inventory")

MARKER_KEY = "user.hsm.id"
ACTIVE_OP_STATES = ("queued", "running", "reconciling")
# Rows the collector may tombstone when their instance disappears.
TOMBSTONE_STATUSES = ("active", "quarantined")


@dataclass
class ReconcileResult:
    attached: list[tuple[int, str]] = field(default_factory=list)   # (index into listing, container id)
    inserted: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    tombstoned: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def observed_columns(instance: dict, cfg) -> dict:
    """Observed configuration columns for one instance (pure)."""
    ecfg = instance.get("expanded_config") or {}
    root = root_device(instance) or {}
    mem_raw = ecfg.get("limits.memory")
    mem = None if (mem_raw and str(mem_raw).strip().endswith("%")) else parse_bytes(mem_raw)
    reasons = lxd_safety.evaluate(instance, allowed_pools=cfg.allowed_pools, allowed_networks=cfg.allowed_networks)
    return {
        "observed_cpu_cores": cpu_count_limit(ecfg.get("limits.cpu")),
        "observed_memory_bytes": mem,
        "observed_disk_bytes": parse_bytes(root.get("size")),
        "observed_pool": root.get("pool"),
        "image_description": str(ecfg.get("image.description") or ""),
        "os": str(ecfg.get("image.os") or ""),
        "architecture": str(instance.get("architecture") or ""),
        "ephemeral": 1 if instance.get("ephemeral") else 0,
        "autostart": 1 if _truthy(ecfg.get("boot.autostart", "false")) else 0,
        "safety": "unsafe" if reasons else "safe",
        "safety_reasons": json.dumps(reasons),
    }


@dataclass
class _Inst:
    idx: int
    name: str
    project: str
    marker: str | None
    vuuid: str | None
    raw: dict

    @property
    def key(self) -> tuple[str, str]:
        return (self.project, self.name)


def _parse(instances: list[dict]) -> tuple[list[_Inst], bool]:
    out, complete = [], True
    for i, inst in enumerate(instances):
        if not isinstance(inst, dict) or not inst.get("name"):
            complete = False   # a malformed entry could be a row we must not tombstone
            continue
        ecfg = inst.get("expanded_config") or {}
        marker = ecfg.get(MARKER_KEY) or None
        out.append(_Inst(i, str(inst["name"]), str(inst.get("project") or "default"),
                         str(marker) if marker else None, ecfg.get("volatile.uuid") or None, inst))
    return out, complete


def reconcile(conn: sqlite3.Connection, cfg, instances: list[dict], *, now_iso: str,
              listing_complete: bool = True, reported: set | None = None) -> ReconcileResult:
    """Apply one listing. ``reported`` is a caller-owned set used to audit a
    duplicate-marker situation once rather than every 10 seconds."""
    parsed, parse_complete = _parse(instances)
    complete = listing_complete and parse_complete
    reported = reported if reported is not None else set()
    res = ReconcileResult()

    with write_tx(conn):
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM containers WHERE status != 'deleted'")}
        by_marker = {r["lxd_marker"]: r for r in rows.values() if r["lxd_marker"]}
        unmanaged_by_key = {(r["project"], r["name"]): r for r in rows.values() if not r["lxd_marker"]}
        active_ops = {r[0] for r in conn.execute(
            "SELECT DISTINCT container_id FROM operations WHERE container_id IS NOT NULL AND state IN (?,?,?)",
            ACTIVE_OP_STATES)}

        present: set[str] = set()          # rows whose instance exists (never tombstone)
        attach: dict[str, _Inst] = {}      # row id -> instance
        quarantine: dict[str, str] = {}    # row id -> reason
        unmarked: list[_Inst] = []

        groups: dict[str, list[_Inst]] = {}
        for inst in parsed:
            if inst.marker:
                groups.setdefault(inst.marker, []).append(inst)
            else:
                unmarked.append(inst)

        for marker, insts in groups.items():
            row = by_marker.get(marker)
            if row is None:
                # Marker of no live row (e.g. restored copy of a deleted container):
                # treat as foreign/unmanaged; never revive the old identity.
                res.notes.append(f"unknown_marker:{insts[0].project}/{insts[0].name}")
                unmarked.extend(insts)
                continue
            present.add(row["id"])
            if row["status"] == "creating":
                continue
            known = row["lxd_volatile_uuid"]
            if len(insts) == 1:
                inst = insts[0]
                if known and inst.vuuid and inst.vuuid != known:
                    if row["id"] not in active_ops:
                        quarantine[row["id"]] = "identity_changed"
                    continue
                attach[row["id"]] = inst
                continue
            # Duplicate marker: a copy carries our marker.
            matches = [i for i in insts if known and i.vuuid == known]
            names = tuple(sorted(f"{i.project}/{i.name}" for i in insts))
            if len(matches) == 1:
                attach[row["id"]] = matches[0]
                others = [i for i in insts if i is not matches[0]]
                res.notes.append(f"duplicate_marker_ignored:{row['id']}")
                if (row["id"], names) not in reported:
                    reported.add((row["id"], names))
                    audit.record(conn, action="container.duplicate_marker", outcome="denied",
                                 target_type="container", target_id=row["id"], target_label=row["name"],
                                 details={"kept": f"{matches[0].project}/{matches[0].name}",
                                          "ignored": [f"{i.project}/{i.name}" for i in others]})
            elif row["id"] not in active_ops:
                quarantine[row["id"]] = "duplicate_marker"
                res.notes.append(f"duplicate_marker_quarantined:{row['id']}")

        # Names occupied after identity-preserving renames are applied.
        final_key = {rid: inst.key for rid, inst in attach.items()}

        # Unmarked instances: match unmanaged rows by (project, name), else new row.
        inserts: list[_Inst] = []
        for inst in unmarked:
            row = unmanaged_by_key.get(inst.key)
            if row is not None and row["id"] not in final_key:
                present.add(row["id"])
                if row["status"] != "creating":
                    attach[row["id"]] = inst
                    final_key[row["id"]] = inst.key
                continue
            # Is the name held by a managed row whose marker vanished from the listing?
            holder = next((r for r in rows.values() if (r["project"], r["name"]) == inst.key
                           and r["lxd_marker"] and r["id"] not in present), None)
            if holder is not None:
                known = holder["lxd_volatile_uuid"]
                if holder["status"] == "creating" or holder["id"] in active_ops:
                    present.add(holder["id"])      # worker is mid-operation; decide later
                    continue
                if known and inst.vuuid and inst.vuuid != known:
                    inserts.append(inst)           # a different instance reused the name
                    continue
                # Same instance with its marker stripped, or undecidable: quarantine.
                present.add(holder["id"])
                quarantine[holder["id"]] = "marker_missing"
                continue
            inserts.append(inst)

        # 1. Tombstones (only after a complete listing; never with an active operation).
        tombstones = []
        if complete:
            for rid, r in rows.items():
                if (rid not in present and rid not in attach and r["status"] in TOMBSTONE_STATUSES
                        and rid not in active_ops):
                    tombstones.append(r)
        for r in tombstones:
            conn.execute("UPDATE containers SET status = 'deleted', deleted_at = ? WHERE id = ?", (now_iso, r["id"]))
            conn.execute("DELETE FROM container_access WHERE container_id = ?", (r["id"],))
            conn.execute("DELETE FROM latest_metrics WHERE container_id = ?", (r["id"],))
            audit.record(conn, action="container.external_delete", outcome="succeeded", target_type="container",
                         target_id=r["id"], target_label=r["name"],
                         details={"project": r["project"], "name": r["name"], "managed": bool(r["managed"])})
            res.tombstoned.append(r["id"])
        dead = {r["id"] for r in tombstones}

        # 2. Quarantines (audited once, on the transition).
        for rid, reason in quarantine.items():
            r = rows[rid]
            attach.pop(rid, None)
            final_key.pop(rid, None)
            if r["status"] != "quarantined":
                conn.execute("UPDATE containers SET status = 'quarantined' WHERE id = ? AND status = ?",
                             (rid, r["status"]))
                audit.record(conn, action="container.quarantined", outcome="succeeded", target_type="container",
                             target_id=rid, target_label=r["name"], details={"reason": reason})
                res.quarantined.append(rid)

        # 3. Renames, two-phase so swaps cannot trip the unique (project, name) index.
        occupied = {(r["project"], r["name"]): rid for rid, r in rows.items() if rid not in dead}
        renames = []
        for rid, inst in attach.items():
            r = rows[rid]
            if (r["project"], r["name"]) == inst.key:
                continue
            holder = occupied.get(inst.key)
            if holder is not None and holder != rid and holder not in attach:
                # Held by a row we could not resolve this cycle; retry next cycle.
                res.notes.append(f"rename_blocked:{rid}")
                continue
            renames.append((rid, inst))
        for rid, _inst in renames:
            conn.execute("UPDATE containers SET name = ? WHERE id = ?", ("\x00renaming:" + rid, rid))
        for rid, inst in renames:
            conn.execute("UPDATE containers SET project = ?, name = ? WHERE id = ?", (inst.project, inst.name, rid))
            res.renamed.append(rid)
        renamed_to = {rid: inst.key for rid, inst in renames}
        taken = {renamed_to.get(rid, (r["project"], r["name"])) for rid, r in rows.items() if rid not in dead}

        # 4. Observed fields for every attached row.
        updates = []
        for rid, inst in list(attach.items()):
            try:
                obs = observed_columns(inst.raw, cfg)
            except Exception as exc:   # malformed config/devices for one instance
                res.notes.append(f"observe_failed:{rid}:{exc.__class__.__name__}")
                conn.execute("UPDATE containers SET last_seen_at = ? WHERE id = ?", (now_iso, rid))
                continue
            updates.append((obs["observed_cpu_cores"], obs["observed_memory_bytes"], obs["observed_disk_bytes"],
                            obs["observed_pool"], obs["image_description"], obs["os"], obs["architecture"],
                            obs["ephemeral"], obs["autostart"], obs["safety"], obs["safety_reasons"], now_iso, rid))
        conn.executemany(
            "UPDATE containers SET observed_cpu_cores = ?, observed_memory_bytes = ?, observed_disk_bytes = ?,"
            " observed_pool = ?, image_description = ?, os = ?, architecture = ?, ephemeral = ?, autostart = ?,"
            " safety = ?, safety_reasons = ?, last_seen_at = ? WHERE id = ?", updates)

        # 5. New unmanaged rows (fresh UUID, no owner, no grants).
        for inst in inserts:
            if inst.key in taken:
                # Name still held by a row we could not resolve (incomplete listing,
                # active operation); try again next cycle.
                res.notes.append(f"insert_blocked:{inst.project}/{inst.name}")
                continue
            try:
                obs = observed_columns(inst.raw, cfg)
            except Exception as exc:
                res.notes.append(f"insert_failed:{inst.project}/{inst.name}:{exc.__class__.__name__}")
                continue
            cid = new_id()
            itype = inst.raw.get("type") if inst.raw.get("type") in ("container", "virtual-machine") else "container"
            conn.execute(
                "INSERT INTO containers(id, project, name, instance_type, lxd_marker, lxd_volatile_uuid, managed,"
                " status, safety, safety_reasons, observed_cpu_cores, observed_memory_bytes, observed_disk_bytes,"
                " observed_pool, image_description, os, architecture, ephemeral, autostart, description,"
                " created_at, last_seen_at) VALUES (?,?,?,?,NULL,?,0,'active',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, inst.project, inst.name, itype, inst.vuuid, obs["safety"], obs["safety_reasons"],
                 obs["observed_cpu_cores"], obs["observed_memory_bytes"], obs["observed_disk_bytes"],
                 obs["observed_pool"], obs["image_description"], obs["os"], obs["architecture"],
                 obs["ephemeral"], obs["autostart"], str(inst.raw.get("description") or "")[:1000],
                 now_iso, now_iso))
            attach[cid] = inst
            taken.add(inst.key)
            res.inserted.append(cid)

    res.attached = sorted(((inst.idx, rid) for rid, inst in attach.items()), key=lambda t: t[0])
    if res.tombstoned or res.quarantined or res.inserted or res.renamed:
        log.info("inventory: +%d new, %d renamed, %d tombstoned, %d quarantined",
                 len(res.inserted), len(res.renamed), len(res.tombstoned), len(res.quarantined))
    return res
