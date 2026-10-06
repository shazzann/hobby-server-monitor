# Decision records

Each record: decision, why, alternatives rejected, cost/tradeoff. File references
point at the code that implements the decision.

## D1. Three local processes from one Python codebase

**Decision.** `hsm-api` (Falcon/Gunicorn), `hsm-worker` (LXD mutations and exec),
`hsm-collector` (polling, TinyFlux owner, history socket), plus a static Astro build.
Entry points: `hsm/app.py:wsgi`, `hsm/worker.py`, `hsm/collector/__main__.py`.

**Why.** LXD socket access is root-equivalent (D4), so the internet-facing process
must not hold it. Long LXD operations and 20 s commands must not block HTTP threads
or delay the 10 s collection cadence. The brief requires collection independent of the UI.

**Rejected.** One process with background threads (simplest, but the API would need the
LXD socket and a crash in one part stops everything). Redis/Celery or a message broker
(a second server process and more RAM for a queue that sees a few jobs a minute).

**Cost.** ~2 extra Python interpreters of resident memory (measured in REPORT), and a
small SQLite job table instead of in-process calls.

## D2. SQLite for metadata, sessions, jobs and latest metrics; TinyFlux only for history

**Decision.** `sqlite3` with WAL, foreign keys, busy timeout and short `BEGIN IMMEDIATE`
transactions (`hsm/db.py`). TinyFlux holds only time series, owned by the collector.

**Why.** One file, no server, transactional quota reservations. Latest values are one row
per container, so dashboards read SQLite and never touch TinyFlux or LXD.

**Rejected.** Postgres (another service on a small host); keeping latest metrics in
TinyFlux (every dashboard poll would scan a CSV shard).

## D3. Sessions: opaque, server-side, revocable

**Decision.** 256-bit random cookie, SHA-256 stored (`hsm/auth/sessions.py`). Each request
re-reads the session row *and* the user's current role/status. Idle 30 min, absolute 8 h.
Logout is a CSRF-protected POST that marks the row revoked. CSRF token = HMAC(session id).
Cookies: `HttpOnly`, `SameSite=Lax`, `Secure` + `__Host-` prefix in HTTPS mode.

**Why.** Logout and revocation must actually end access (brief Q1); demotion must apply on the
next request. A database lookup per request is cheap at this scale.

**Rejected.** Self-contained JWT sessions (cannot be revoked without a denylist that recreates
server state); tokens in localStorage (readable by any XSS).

## D4. The LXD privilege boundary

**Decision.** Treat the LXD unix socket as host root. Only the worker and collector users are in
`lxd`; `hsm-api` is not and its unit makes `/var/snap/lxd` inaccessible. The API cannot express
raw LXD configuration: it accepts typed fields, and the worker's adapter builds the config itself
(`hsm/integrations/lxd.py`). New containers live in a restricted project (`scripts/setup-dev-host.sh`:
unprivileged only, no nesting, no low-level keys, managed disks/NICs only, one bridge).
`hsm/lxd_safety.py` re-checks the *expanded* config before every mutation and exec.

**Why.** "A non-root UID" does not reduce what an `lxd` group member can do. The useful
boundary is: the process that parses untrusted HTTP never has the socket.

**Residual risk (stated, not solved).** A compromise of the worker or collector is a host
compromise. The API and worker share SQLite, so a fully compromised API can enqueue jobs
for the worker — but only jobs the worker re-validates (typed kinds, policy re-check against
current users/assignments, safety check, quota). Project-scoped TLS identities for the worker
would narrow this further; deferred because the admin inventory must also see other projects.

**Rejected.** Running the API as root or in `lxd` (one bug = host root); exposing LXD over
HTTPS to the browser (same, plus network exposure).

## D5. Authorization is declared per route and re-checked on objects

**Decision.** Every resource has `policies = {METHOD: policy}`; `assert_all_routes_declared`
fails app start-up (and a test) if any responder lacks one (`hsm/auth/policy.py`,
`hsm/app.py:AuthMiddleware`). Object access goes through `container_for` /
`operation_for`, which the API *and the worker* call. Unassigned existing container → 403;
unknown id → 404; lists are filtered in SQL.

**Why.** Brief Q2: an endpoint added next month must not be able to forget authorization.
The worker re-check makes revocation effective for queued work.

**Rejected.** Decorators on each handler (easy to forget); checking only in the UI.

## D6. First admin: configured email *and* a one-time secret

**Decision.** `BOOTSTRAP_ADMIN_EMAIL` names the account; `hsm bootstrap` (local CLI) prints a
30-minute one-time link whose secret travels in the URL fragment, is POSTed same-origin and is
bound to the OAuth transaction (`hsm/auth/admission.py`). Completion is persisted; the CLI refuses
once any admin exists.

**Why.** "First user to sign in wins" is a race on a freshly exposed server. Email alone is not
proof of mailbox ownership for non-Gmail Google accounts. Persisting completion means restarting
with the variable set cannot mint another admin.

## D7. Invitation-only admission bound to Google `sub`

**Decision.** Admin invites by email → pending user + one-time link (only the hash stored).
Admission requires the secret, an open invitation, Google `email_verified`, and an exact email match;
the account is then bound to Google's immutable `sub`. Returning users are matched by `sub` only.

**Rejected.** Matching by email on every login (email changes / third-party addresses); automatic
email delivery (P2; the admin shares the link).

## D8. Quota = configured allocation charged to one owner

**Decision.** A container has exactly one resource owner; access grants are free. Running,
stopped and frozen containers count; pending operations hold reservations
(`hsm/services/quotas.py`). Checking and reserving happen in one `BEGIN IMMEDIATE` transaction,
so two racing requests are serialized by SQLite's write lock (`tests/test_quotas.py::test_two_racing_creates…`).
At the limit the request is rejected with 409 and refreshed bounds; nothing running is killed.
An uncertain LXD outcome keeps its reservation until reconciliation proves the state.

**Rejected.** Measuring quota by instantaneous usage (a stopped container would free quota it can
reclaim on start); charging every assigned user (double counting).

## D9. CPU allowance is a hard CFS quota

**Decision.** UI "N cores at P %" → `limits.cpu=N`, `limits.cpu.allowance = N×P ms / 100 ms`.

**Why.** LXD's percentage form (`50%`) is a soft scheduler weight that only matters under
contention; calling it a cap would be untrue. Verified on the real host in docs/verification.md
(`cpu.max` inside the container) once LXD is available.

## D10. Durable operations with idempotency keys

**Decision.** Mutations and exec return `202 {operation}`; the worker claims with a lease, re-checks
policy/safety/capacity, calls LXD outside any transaction, then finalizes accounting and the
reservation in one transaction (`hsm/services/operations.py`, `hsm/worker.py`). Idempotency key +
hash of the request body: same → original operation, different → 409. Exec is never replayed.

**Rejected.** Synchronous LXD calls inside HTTP requests (thread starvation, ambiguous timeouts).

## D11. Metrics: fixed 10 s collector, SQLite latest cache, sharded TinyFlux history

**Decision.** One collector process polls LXD with one bulk request per cycle, writes `latest_metrics`
and raw points (hourly shards, 6 h), builds 5-minute rollups (daily shards, 30 days), deletes whole
expired shards and enforces a byte budget. Browsers poll `/api/containers` every 10 s while visible;
tabs never trigger LXD calls. Charts are ≤ 600 points per series.

**Why.** Cost is independent of open tabs (brief Q5); bounded storage after a month (Q6);
a 24 h chart is ~288 five-minute points per series (Q9).

**Rejected.** Server-sent events/WebSockets (a long-lived connection per tab for a 10 s cadence);
keeping raw 10 s data for 30 days (~260k points per container).

## D12. Terminal: one command at a time, really executed in the container

**Decision.** pylxd `execute` with fixed argv `timeout -s KILL <deadline> /bin/sh -c <command>`,
non-root guest uid 1500 for container users (root for admins), fixed environment, 4 KiB command,
64 KiB output cap, 20 s deadline; for uid 1500 a follow-up `kill -KILL -1` as that uid removes any
background process. Output is rendered with `textContent`.

**Rejected.** A browser PTY over WebSockets (P2; larger attack surface and a long-lived privileged
stream); running `lxc exec` through a host shell (command injection on the host).

## D13. Same-origin static frontend with a strict CSP

**Decision.** Astro builds static files that Falcon serves from the same origin, so cookies stay
`SameSite=Lax`, no CORS. CSP `script-src 'self'; style-src 'self'` — the build is checked for
inline scripts/styles. In HTTPS deployments a reverse proxy terminates TLS in front of the API.

**Rejected.** A Node SSR server (another process and runtime in production).

## D14. Development environment (recorded constraint)

The development host is Windows 11 + WSL2 Ubuntu 22.04 (kernel 6.6, systemd enabled, cgroup v2,
btrfs as a module). A btrfs loop pool is used because the `dir` driver cannot enforce root-disk
quotas. Unit tests also run on Windows Python 3.13 against fakes; Linux-only pieces (fcntl lock,
AF_UNIX peer credentials) are exercised in WSL.
