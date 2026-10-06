# API and internal contracts

Canonical for every component. Shape changes go through the lead and update
this file together with all consumers.

## Conventions

- IDs: UUIDv4 strings (users, containers, operations, invitations).
- Time: UTC. JSON timestamps are ISO-8601 strings `YYYY-MM-DDTHH:MM:SSZ`;
  chart points use integer **epoch seconds**.
- Units: bytes (integers) for memory/disk/network; `cpu_cores` integer;
  `cpu_allowance_pct` integer 10–100 (percent of the selected cores, applied as
  a hard CFS quota `cores × pct` ms per 100 ms); rates in bytes/second;
  `cpu_pct` = % of the container's allocated cores.
- Unknown values are `null`, never `0`.
- Every unsafe method (POST/PUT/PATCH/DELETE) requires header
  `X-CSRF-Token: <csrf_token from /api/me>` and a same-origin `Origin` header.
- Mutating container endpoints and exec require `Idempotency-Key` (8–128 of
  `[A-Za-z0-9_-]`). Same key + same body → same operation (200); same key +
  different body → 409 `IDEMPOTENCY_KEY_REUSED`.
- Errors:
  `{"error": {"code": "QUOTA_EXCEEDED", "message": "...", "fields": {"memory_bytes": "..."}, "request_id": "..."}}`
  400 malformed · 401 unauthenticated · 403 forbidden · 404 unknown · 409 state/quota/version conflict ·
  422 invalid field · 429 rate limited · 503 dependency unavailable.
- Authenticated responses carry `Cache-Control: no-store`.

## Policies

`public` · `oauth` (callback; own state checks) · `authenticated` · `admin` ·
`container-view` (admin, or assigned user) · `container-exec` (as view, plus
app-managed + safe) · `operation-view` (admin, or the actor while still
assigned). An existing but unassigned container returns **403** on every
container route; an unknown id returns 404.

## Auth routes

| Method | Path | Policy | Notes |
|---|---|---|---|
| GET | `/auth/google/start` | public | 302 to Google; sets HttpOnly `hsm_oauth` binding cookie |
| POST | `/auth/admission-context` | public (rate-limited, same-origin) | body `{"kind": "invitation"\|"bootstrap", "secret": "..."}` → `{"authorize_url": "..."}` |
| GET | `/auth/google/callback` | oauth | 302 to `/` or `/login/?error=CODE` |
| POST | `/auth/logout` | authenticated + CSRF | 204, cookie cleared, session revoked |

Login error codes shown by `/login/`: `NOT_INVITED`, `INVITATION_INVALID`,
`EMAIL_MISMATCH`, `EMAIL_UNVERIFIED`, `ACCOUNT_REVOKED`, `OAUTH_FAILED`,
`STATE_INVALID`, `BOOTSTRAP_INVALID`.

## Application routes

| Method | Path | Policy |
|---|---|---|
| GET | `/api/me` | authenticated |
| GET | `/api/health` | authenticated (details for admin) |
| GET | `/health/live` | public |
| GET | `/api/containers` | authenticated (filtered in SQL) |
| POST | `/api/containers` | admin |
| GET | `/api/containers/{id}` | container-view |
| DELETE | `/api/containers/{id}?confirm_name=<name>` | admin |
| PATCH | `/api/containers/{id}/limits` | admin |
| POST | `/api/containers/{id}/actions` | admin |
| PATCH | `/api/containers/{id}/owner` | admin |
| PUT/DELETE | `/api/containers/{id}/assignments/{user_id}` | admin |
| POST | `/api/containers/{id}/exec` | container-exec |
| GET | `/api/containers/{id}/history` | container-view |
| GET | `/api/containers/{id}/usage` | container-view |
| GET | `/api/creation-options?owner_id=` | admin |
| GET | `/api/operations/{id}` | operation-view |
| GET | `/api/users` | admin |
| PATCH | `/api/users/{id}` | admin |
| POST | `/api/users/{id}/revoke` | admin |
| POST | `/api/invitations` | admin |
| DELETE | `/api/invitations/{id}` | admin |
| GET | `/api/accounting` | admin |
| GET | `/api/audit-events?before_id=&limit=` | admin |

### Shapes

`GET /api/me`
```json
{"user": {"id": "...", "email": "a@x.com", "display_name": "A", "role": "admin"},
 "csrf_token": "hex",
 "quota": {"quota": {"cpu_cores": 4, "memory_bytes": 0, "disk_bytes": 0},
           "allocated": {...}, "pending": {...}, "remaining": {...}}}
```

Container object (list item and detail):
```json
{"id": "uuid", "name": "web-1", "project": "hsm", "instance_type": "container",
 "managed": true, "status": "active", "safety": "safe", "safety_reasons": [],
 "owner": {"id": "uuid", "email": "u@x.com"},
 "limits": {"cpu_cores": 1, "cpu_allowance_pct": 50, "memory_bytes": 536870912,
            "disk_bytes": 2147483648, "pool": "hsm-btrfs"},
 "observed_limits": {"cpu_cores": 1, "memory_bytes": 536870912, "disk_bytes": 2147483648, "pool": "hsm-btrfs"},
 "image_description": "Ubuntu 24.04 LTS", "os": "Ubuntu", "architecture": "x86_64",
 "ephemeral": false, "autostart": true, "description": "", "version": 3,
 "metrics": {"state": "Running", "cpu_pct": 3.2, "memory_bytes": 104857600, "memory_limit_bytes": 536870912,
             "disk_bytes": 734003200, "disk_limit_bytes": 2147483648, "rx_bytes": 1234, "tx_bytes": 99,
             "rx_rate": 12.5, "tx_rate": 0.0, "processes": 14, "ipv4": "10.1.2.3",
             "started_at": "2026-10-06T16:00:00Z", "uptime_seconds": 3600,
             "sampled_at": "2026-10-06T17:00:00Z", "age_seconds": 4, "stale": false, "quality": []},
 "active_operation": {"id": "uuid", "kind": "restart", "state": "running"}}
```
`metrics` is `null` when never observed. `stale` is true when older than
3 collection intervals. Detail adds `"access": [{"user_id": "...", "email": "..."}]`
(admin only), `"limit_bounds"` (admin, managed: same shape as creation `bounds`, computed with the
container's current allocation counted as available) and
`"capabilities": {"can_manage": bool, "can_exec": bool, "exec_as": "root"|"hsm"}`.

`GET /api/containers` → `{"containers": [...], "collector": {"last_success_at": "...", "age_seconds": 4, "stale": false, "lxd_available": true}}`

`POST /api/containers` (Idempotency-Key) body — unknown fields rejected:
```json
{"name": "web-1", "image": "hsm/ubuntu-24.04", "pool": "hsm-btrfs", "network": "hsmbr0",
 "cpu_cores": 1, "cpu_allowance_pct": 50, "memory_bytes": 536870912, "disk_bytes": 2147483648,
 "owner_id": "uuid", "ephemeral": false, "autostart": true, "description": "", "options_version": "..."}
```
→ `202 {"operation": Operation, "container_id": "uuid"}`; 409 `QUOTA_EXCEEDED`/`HOST_CAPACITY`
with `fields` and refreshed `bounds` under `error.bounds`.

`GET /api/creation-options?owner_id=` →
```json
{"options_version": "sha", "images": [{"alias": "hsm/ubuntu-24.04", "fingerprint": "...", "description": "...", "os": "Ubuntu", "release": "noble", "architecture": "x86_64"}],
 "pools": [{"name": "hsm-btrfs", "driver": "btrfs", "quota_capable": true, "total_bytes": 0, "used_bytes": 0}],
 "networks": [{"name": "hsmbr0"}], "owners": [{"id": "...", "email": "..."}],
 "bounds": {"cpu_cores": {"min": 1, "max": 4}, "cpu_allowance_pct": {"min": 10, "max": 100},
            "memory_bytes": {"min": 134217728, "max": 0, "step": 1048576},
            "disk_bytes": {"hsm-btrfs": {"min": 1073741824, "max": 0, "step": 1048576}},
            "blocked_reason": null},
 "name_rule": "^[a-z][a-z0-9-]{0,61}[a-z0-9]$"}
```

`PATCH /api/containers/{id}/limits` (Idempotency-Key):
`{"version": 3, "cpu_cores": 2, "cpu_allowance_pct": 100, "memory_bytes": ..., "disk_bytes": ..., "confirm_reduction": false}`
— disk is expansion-only; memory reduction needs `confirm_reduction: true` and
must stay above observed use + 64 MiB. Stale `version` → 409 `VERSION_CONFLICT`.

`POST /api/containers/{id}/actions` (Idempotency-Key): `{"action": "start"|"stop"|"restart"|"freeze"|"unfreeze", "confirm_ephemeral": false}`

`POST /api/containers/{id}/exec` (Idempotency-Key): `{"command": "uname -a"}` (≤ 4096 bytes UTF-8, no NUL) → 202 Operation.

Operation:
```json
{"id": "uuid", "kind": "exec", "state": "queued|running|succeeded|failed|reconciling|cancelled",
 "container_id": "uuid", "created_at": "...", "started_at": null, "finished_at": null,
 "error": {"code": "...", "message": "..."} , "result": null}
```
Exec `result` (only for the actor, expires after 15 min):
`{"outcome": "completed"|"timed_out"|"unknown", "exit_code": 0, "stdout": "...", "stderr": "...",
  "stdout_truncated": false, "stderr_truncated": false, "duration_ms": 120, "user": "root"|"hsm"}`

`GET /api/containers/{id}/history?range=1h|6h|24h|7d|30d&metrics=cpu_pct,memory_bytes&max_points=300`
(or `start`/`end` epoch seconds) →
```json
{"container_id": "uuid", "start": 1, "end": 2, "resolution_seconds": 10, "source": "raw"|"rollup",
 "series": {"cpu_pct": {"unit": "%", "points": [[1759766400, 2.5], [1759766410, null]]}},
 "coverage": 0.98, "gaps": [[s, e]], "available_from": 1759700000}
```
Metrics: `cpu_pct`, `memory_bytes`, `disk_bytes`, `rx_rate`, `tx_rate`, `processes`.
At most 600 points per series.

`GET /api/containers/{id}/usage?range=24h` →
`{"start", "end", "covered_seconds", "coverage", "cpu_core_hours", "memory_gib_hours", "disk_avg_bytes", "disk_max_bytes", "rx_bytes", "tx_bytes"}` (null when no coverage).

`GET /api/users` → `{"users": [{"id", "email", "display_name", "role", "status", "quota": {...}, "allocated": {...}, "pending": {...}, "invitation": {"id", "expires_at"} | null, "containers": [{"id", "name", "relation": "owner"|"assigned"}]}]}`

`POST /api/invitations` `{"email": "u@x.com", "role": "user", "quota": {"cpu_cores": 2, "memory_bytes": ..., "disk_bytes": ...}}`
→ 201 `{"invitation": {"id", "email", "expires_at"}, "user_id": "...", "link": "http://localhost:8000/invite/#<secret>"}`
(the link is shown once; only its hash is stored).

`PATCH /api/users/{id}` `{"role"?: "admin"|"user", "quota"?: {...}}` — last active admin cannot be demoted (409 `LAST_ADMIN`).

`GET /api/accounting` →
`{"host": {"known": true, "cpu": {"total", "reserve", "budget", "allocated", "pending", "remaining"}, "memory": {...}, "pools": [{"name", "driver", "total", "used", "reserve", "budget", "allocated", "pending", "remaining"}]}, "incomplete": [...], "owners": [{"user_id", "email", "quota", "allocated", "pending", "remaining"}]}`

`GET /api/audit-events` → `{"events": [{"id", "at", "actor_email", "action", "target_type", "target_id", "target_label", "outcome", "details"}], "next_before_id": 123|null}`

### Responses of mutating endpoints

| Endpoint | Response |
|---|---|
| `POST /api/containers` | 202 `{"operation", "container_id"}` (200 on idempotent replay) |
| `PATCH …/limits`, `POST …/actions`, `DELETE /api/containers/{id}`, `POST …/exec` | 202 `{"operation"}` (200 on replay) |
| `PATCH …/owner` | 200 container detail (synchronous; body `{"owner_id": "uuid"}`) |
| `PUT/DELETE …/assignments/{user_id}` | 204 (synchronous) |
| `PATCH /api/users/{id}` | 200 `{"users": [...]}` |
| `POST /api/users/{id}/revoke` | 200 `{"revoked": {sessions_revoked, grants_removed, queued_cancelled, already_dispatched}, "note"}` |
| `DELETE /api/invitations/{id}` | 204 |
| `POST /auth/logout` | 204 |

`Idempotency-Key` is required only where an operation is created; it is ignored elsewhere.
CSRF failures are 403 `CSRF_FAILED`; cross-origin unsafe requests are 403 `ORIGIN_REJECTED`.
`accounting.incomplete[]` items: `{"container_id", "name", "missing": ["cpu"|"memory"|"disk"]}`.
Audit `outcome`: `requested` | `succeeded` | `failed` | `denied`; `details` is a free-form JSON object.
Container `status`: `creating` | `active` | `quarantined` | `failed` | `deleted`; `metrics.state`:
`Running` | `Stopped` | `Frozen` | `Error` | `Unknown`. Description ≤ 255 printable characters.

## Static pages (Astro, served same-origin)

`/login/`, `/invite/` (secret in `#fragment`), `/setup/` (bootstrap, `#fragment`),
`/` overview, `/containers/view/?id=`, `/containers/new/`, `/users/`,
`/accounting/`, `/activity/`.

## Internal: operations payloads (`operations.payload`)

| kind | payload |
|---|---|
| create | `{name, image_alias, image_fingerprint, pool, network, cpu_cores, cpu_allowance_pct, memory_bytes, disk_bytes, owner_id, ephemeral, autostart, description}` |
| start/restart/freeze/unfreeze | `{}` |
| stop | `{"force": false}` |
| update_limits | `{"before": {limits}, "after": {cpu_cores, cpu_allowance_pct, memory_bytes, disk_bytes}}` |
| delete | `{"name": "..."}` |
| exec | `{"command": "...", "as_root": bool}` (cleared when the result expires) |
| adopt | `{"owner_id", "cpu_cores", "cpu_allowance_pct", "memory_bytes", "disk_bytes"}` |

## Internal: collector → SQLite

- `latest_metrics`: one row per live container (schema in `0001_initial.sql`).
- `service_state['collector.heartbeat']`: `{last_cycle_at, last_success_at, cycle_ms, missed_cycles, lxd_available, error, containers}`.
- `service_state['lxd.capabilities']`:
  `{observed_at, host: {cpu_count, memory_bytes}, pools: [{name, driver, total_bytes, used_bytes, quota_capable}], networks: [{name, type, managed}], images: [{alias, fingerprint, description, os, release, architecture, size_bytes}], project: {name, exists, restricted}}`
- Inventory reconciliation writes only: `name`, `project` (for marker matches), `observed_*`, `image_description`, `os`, `architecture`, `safety`, `safety_reasons`, `last_seen_at`, new unmanaged rows, `quarantined`, and `deleted` tombstones (only after a complete successful listing and only when no operation is active on that container). It never writes `owner_id`, committed limits or reservations.

## Internal: history socket (API → collector)

Unix stream socket `HSM_HISTORY_SOCKET`, peer UID checked against
`HSM_HISTORY_ALLOWED_UIDS`. One request per connection: a single JSON line
(≤ 4096 bytes), one JSON line back.

```json
{"v": 1, "type": "history", "user_id": "uuid", "container_id": "uuid", "start": 1, "end": 2, "metrics": ["cpu_pct"], "max_points": 300}
{"v": 1, "type": "usage",   "user_id": "uuid", "container_id": "uuid", "start": 1, "end": 2}
```
Response `{"ok": true, "data": {...}}` or `{"ok": false, "error": {"code": "BUSY"|"FORBIDDEN"|"INVALID"|"UNAVAILABLE", "message": "..."}}`.
The collector re-checks the user's current access in SQLite before answering.

## TinyFlux layout (owned solely by the collector)

| Measurement | Shard | Tags | Fields |
|---|---|---|---|
| `c_raw_v1` | `metrics/raw/YYYYMMDDHH.csv` (hourly, UTC) | `cid` (container UUID) | `state`, `cpu_ns`, `cpu_pct`, `cores`, `mem`, `mem_lim`, `disk`, `disk_lim`, `rx`, `tx`, `rx_rate`, `tx_rate`, `procs`, `dt` (valid interval s), `cpu_d_ns`, `rx_d`, `tx_d` |
| `c_5m_v1` | `metrics/rollup/YYYYMMDD.csv` (daily, UTC) | `cid` | `covered_s`, `cpu_s`, `cpu_alloc_s`, `mem_bs`, `mem_max`, `disk_bs`, `disk_max`, `rx_d`, `tx_d`, `procs_max`, `samples` |

Unknown field values are stored as TinyFlux `None`. Raw retention 6 h, rollups 30 days, whole-shard deletion.

## Operation error codes (worker)

Shown to users as `operation.error.code` with a human `message`.

| Group | Codes |
|---|---|
| Authorization / identity | `ACTOR_NOT_AUTHORIZED`, `CONTAINER_NOT_FOUND`, `CONTAINER_UNSAFE`, `NOT_MANAGED`, `INSTANCE_MISSING`, `IDENTITY_MISMATCH`, `IDENTITY_AMBIGUOUS`, `IDENTITY_CONFLICT`, `INVALID_STATE`, `OWNER_INACTIVE`, `ALREADY_MANAGED`, `INVALID_PAYLOAD`, `INVALID_REQUEST`, `CANCELLED` |
| LXD outcome | `LXD_REJECTED`, `LXD_UNAVAILABLE` (nothing sent), `LXD_UNCERTAIN`, `LXD_NOT_CREATED`, `LXD_NOT_APPLIED`, `OUTCOME_NOT_OBSERVED`, `DELETE_NOT_APPLIED`, `LEASE_EXPIRED` |
| Create / limits | `NAME_TAKEN`, `POOL_FULL`, `DISK_SHRINK` |
| Exec | `NOT_RUNNING`, `EXEC_FAILED`, `EXEC_EXPIRED` (queued > 5 min), `INVALID_COMMAND`, `OUTCOME_UNKNOWN` (never replayed) |

## Internal: `service_state['metrics.storage']`

`{bytes, raw_from, rollup_from, removed_shards, quarantined: [[start, end]], rollup_checkpoint, dropped_ingest}`
(`raw_from`/`rollup_from`/`rollup_checkpoint` are epoch seconds; `removed_shards` is a running count).
The collector refuses to start (exit 2) on an uninitialized database: run `hsm init-db` first.
Keep `HSM_HISTORY_SOCKET` on a Linux filesystem and under the ~108-byte socket path limit.
