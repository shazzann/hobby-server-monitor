# Interview walkthrough

A guide to the code as it actually is, written so the candidate can explain and defend it.
It was produced by the AI agent that wrote the code (see docs/ai-usage.md); the
candidate should verify each claim against the files before relying on it.

## 1. The request path in one minute

1. Browser loads static Astro pages from the API's own origin (`hsm/app.py:StaticSink`).
2. Page script calls `/api/me` (`hsm/api/misc.py:Me`) → gets role + CSRF token (`dashboard/src/lib/api.ts`).
3. Every request passes `AuthMiddleware.process_resource` (`hsm/app.py`):
   policy lookup → session resolve (`hsm/auth/sessions.py:resolve`) → Origin + CSRF on unsafe
   methods → admin check → object check (`hsm/auth/policy.py:container_for` / `operation_for`).
4. Reads come from SQLite only (`hsm/services/containers.py:list_for`), history via the
   collector's Unix socket (`hsm/history_client.py` → `hsm/collector/history_server.py`).
5. Writes become operations (`hsm/services/operations.py:submit`) inside one `BEGIN IMMEDIATE`
   transaction that also reserves quota (`hsm/services/quotas.py:check_and_reserve`).
6. The worker (`hsm/worker.py`) claims, re-authorizes, re-checks live safety, calls LXD through
   `hsm/integrations/lxd.py`, then finalizes in one transaction.
7. The collector (`hsm/collector/__main__.py`, `cycle.py`) polls every 10 s regardless of browsers.

## 2. The twelve open questions

**1. Staying signed in / logout.** Opaque 256-bit cookie, only its SHA-256 stored
(`sessions.create`). `resolve` re-reads session *and* user each request; idle 30 min, absolute 8 h,
`last_seen` written at most once a minute. Logout (`api/auth.py:Logout`) marks the row revoked and
expires the cookie. Rejected: JWTs (no real revocation).

**2. Where authorization happens.** Two layers: declared route policies checked by middleware, and
object checks in `policy.py`. `assert_all_routes_declared` runs at start-up and in
`tests/test_api_security.py::test_every_route_declares_a_policy`, so a new endpoint without a policy
cannot start. The worker calls the same `container_for` before dispatch.

**3. What a quota measures.** Configured allocation (cores, memory bytes, disk bytes) charged to one
owner; stopped/frozen still count; pending operations reserve. At the limit: 409 with field messages
and refreshed bounds; nothing running is touched. Race safety: `BEGIN IMMEDIATE` serializes writers
(`tests/test_quotas.py::test_two_racing_creates_cannot_jointly_exceed_quota`).

**4. What pylxd access grants.** Root on the host (LXD socket = can create privileged containers,
mount `/`). Response: only worker/collector users are in `lxd` (`deploy/install.sh`), the API never
imports pylxd (`tests/test_boundaries.py`), the adapter builds every config itself
(`lxd.build_create_body`, `limit_config`), restricted project, live safety re-check
(`hsm/lxd_safety.py`). Residual: worker/collector compromise = host compromise.

**5. How the dashboard learns about changes.** Visible-tab polling of `/api/containers` every 10 s
(`dashboard/src/lib/poll.ts`), no overlap, backoff, pause when hidden. Pending operations polled at
750 ms until terminal. Cost while nobody looks: zero API requests; the collector's cost is fixed.

**6. Metric store after a month.** Raw 10 s points for ≤ 7 h (hourly shards), 5-minute rollups for
≤ 31 days (daily shards), whole-shard deletion, byte budget and free-space floor
(`hsm/collector/store.py:apply_retention`). Per continuously running container ≈ 2,160 raw +
8,640 rollup points. Measured bytes: see REPORT.

**7. How real the terminal is.** Real `pylxd execute` inside the container
(`hsm/integrations/lxd_exec.py`). Users run as uid 1500 with a fixed env; 4 KiB command, 64 KiB
output, 20 s deadline, `kill -KILL -1` as uid 1500 after each command. What a user can still do:
anything uid 1500 can do inside that container for 20 s (e.g. use network, fill its own disk quota,
fork up to `limits.processes`). Root commands (admins) can leave background processes.

**8. Rename / delete while assigned.** App UUID stored in `user.hsm.id`; renames keep UUID, grants
and history (`collector/inventory.py`). Deletion tombstones the row and drops grants and latest
metrics; a new container with the same name gets a new UUID and no grants. Copies with the same
marker are quarantined.

**9. 24-hour chart payload.** ≤ 288 five-minute points per series (cap 600), only requested metrics
of one container (`store.history`). Measured bytes: see REPORT/verification.

**10. LXD down / slow / odd.** Collector keeps running, marks `lxd_available=false`, keeps history,
never tombstones without a complete listing; UI shows last values with a stale banner. Worker
distinguishes "rejected" from "uncertain" (`integrations/lxd_errors.py`); uncertain keeps the
reservation and goes to `reconciling`. Malformed data for one container doesn't affect others.

**11. First admin.** `BOOTSTRAP_ADMIN_EMAIL` + a CLI-printed one-time secret, bound to the OAuth
transaction; persisted completion (`auth/admission.py:_bootstrap`, `issue_bootstrap_secret`).

**12. Running and rebooting.** Three systemd units (`deploy/systemd/`), enabled at boot, migrations
run once by the installer; the worker reconciles expired leases on start; the collector resumes.

## 3. Things an interviewer is likely to probe — and the honest answer

- *"Show me an unassigned container returning 403."* `curl -b cookie localhost:8000/api/containers/<id>` → 403;
  test `test_unassigned_container_routes_return_403`.
- *"Can a user escape via exec?"* Not to the host through our code path: the text is one argv element of a
  container-side `/bin/sh`; no host shell is involved. Kernel/LXD escapes are out of our control; we rely on
  unprivileged containers + LXD's AppArmor/seccomp and keep users non-root.
- *"Why SQLite as a job queue?"* A few jobs per minute; the reservation and the job must commit atomically.
- *"What did the AI get wrong?"* docs/ai-usage.md lists the actual corrections, and the review fixes are in
  commit `Fix defects from the security cross-review` (open redirect, rate-limit design, orphan creating rows…).

## 4. What would change at 100 or 1,000 containers
Measure first. Likely: per-cycle LXD listing becomes the bottleneck → stagger state fetches; TinyFlux CSV
scans → a server TSDB; SQLite writer contention from latest-metrics upserts → batch less often or move
latest values to memory in the collector. The authorization and accounting invariants stay the same.

## 5. Known limitations (keep in sync with REPORT.md)
- See REPORT.md "Known limitations".

## 6. Review before the interview (candidate's checklist)
1. Read `hsm/auth/policy.py`, `hsm/app.py:AuthMiddleware`, `hsm/services/quotas.py`, `hsm/services/operations.py`,
   `hsm/integrations/lxd_exec.py`, `hsm/collector/normalize.py` end to end.
2. Run `make test` and one real flow yourself; break something on purpose (remove a policy) and watch start-up fail.
3. Be able to derive the CPU % formula and the 288-point figure without notes.
4. Rehearse the LXD privilege answer including the residual risk.
