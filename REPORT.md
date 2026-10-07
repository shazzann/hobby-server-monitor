# Final Report — Hobby Server Monitor

Every number below was measured on the machine described in "Resource Measurements"; anything not
verified is listed under "Known Limitations". Evidence log with commands and timestamps:
[docs/verification.md](docs/verification.md).

## Time Spent

Sprint start 2026-10-06 21:16 (Asia/Colombo). The code was written by an AI coding agent (lead) and,
for about 20–25 minutes each, three specialist sub-agents running **in parallel**, plus one read-only
reviewer sub-agent; the candidate supervised, supplied the environment (disk space, LXD install,
Google OAuth, sudo steps) and ran the manual checks. Wall-clock time and agent effort overlap.

| Period (wall clock) | Duration | Work |
|---|---|---|
| 10-06 21:16–22:15 | ~1.0 h | Environment inspection and blockers; schema, config, auth/OIDC, sessions, policy, quotas, operation queue, admin API (lead); LXD adapter + worker + exec, collector + TinyFlux + history socket, Astro dashboard (three parallel sub-agents); integration; security cross-review and fixes; UI wiring checks against a fake LXD |
| 10-06 22:15 → 10-07 06:20 | — | Paused: waiting for disk space, LXD installation and OAuth credentials (not work time) |
| 10-07 06:20–07:00 | ~0.7 h | Real LXD: spike, CPU/disk quota and exec checks, end-to-end API and browser flows, collector independence, idle/one-tab measurements; 3 defects fixed |
| 10-07 10:30–11:50 | ~1.3 h | Real Google bootstrap and invitation with the candidate's accounts; candidate's browser walkthrough; fixes for issues the candidate hit (quota message, owner access, reinstatement, flaky test) |
| 10-07 13:40–14:45 | ~1.1 h | systemd install, reboot recovery, privilege-boundary check, clean-checkout checks, systemd measurements, report |
| **Total active** | **≈ 4.1 h** | of which ≈ 1.2 h was the candidate's interactive steps |

Rough split of the active time: backend/auth/API 25 %, LXD integration/worker 15 %, collector/TSDB
10 %, dashboard 10 %, real-environment verification and debugging 25 %, documentation/report 15 %.

## Key Decisions

The full records with rejected alternatives are in [docs/decisions.md](docs/decisions.md) (D1–D14)
and the interview answers to all twelve open questions in
[docs/interview-walkthrough.md](docs/interview-walkthrough.md). Summary of the most important:

1. **The LXD socket is root.** Only two non-network processes (worker, collector) can reach it; the
   HTTP API cannot and never imports pylxd. Residual risk: compromising the worker/collector is
   compromising the host.
2. **Default-deny authorization** declared per route and enforced at start-up, plus shared object
   checks used by both API and worker; unassigned existing containers return 403.
3. **Quota = allocation, charged to one owner**, reserved atomically in SQLite; at the limit the
   request is rejected with bounds, nothing running is touched.
4. **Opaque server-side sessions** with real revocation; invitations and bootstrap use one-time
   secrets bound to the Google sign-in and to Google's immutable `sub`.
5. **Fixed-cost metrics:** one collector polls every 10 s whether or not anyone watches; browsers read
   a SQLite cache; history is sharded with finite retention and ≤ 600 points per chart series.

## Issues Encountered and Solutions

Real ones, in order (details in docs/ai-usage.md and docs/verification.md):

- C: drive was full, blocking WSL/LXD — raised as a blocker; the candidate freed space.
- Idempotency hashing included a freshly generated id, and replays were re-validated before lookup —
  replays failed. Fixed by hashing the client request and looking up replays first.
- Security cross-review found an open redirect in the static file handler, a rate limit that one
  client could exhaust for everyone, orphaned `creating` rows after revocation, a logout cookie that
  could not clear `__Host-` cookies, and 500s on wrong JSON types. All fixed with regression tests.
- Falcon does not split `a,b` query values by default → history requests with several metrics were
  rejected (found in the browser).
- The dashboard read `{"operation": …}` as the operation → pending-operation polling never ended
  (found in a real browser run on LXD).
- LXD reports nanosecond timestamps; Python 3.10 cannot parse them → uptime showed "unknown"
  (found on real LXD).
- A CSP checker passed on an empty build → it now fails when no HTML is found.
- Candidate-reported: a fresh admin with zero quota saw "Insufficient capacity" (misleading) → the form
  now says whose quota is the limit; a user could not see a container charged to them → the owner now
  gets access automatically; a revoked user could not be re-invited → reinstatement via a new
  invitation for the same Google account; a collector test failed only on hosts up for > 1 h → fixed.
- systemd install: a non-existent per-user group and Gunicorn 26's control socket in a missing home
  directory broke the first install → fixed; a boot-order race with LXD's socket → ordering + recovery log.

## What You Learned

> Drafted with the AI assistant from this project's log and verification record; to be checked and
> rewritten in the candidate's own words before submission.

**Security is mostly about where the boundary sits, not about the user ID.** I expected that running
the API as a non-root user would make it "low privilege". It would not: any process that can open the
LXD socket can create a privileged container and mount the host's root filesystem, so `lxd` group
membership *is* root. The useful boundary was keeping the socket away from the one process that parses
untrusted HTTP, and proving it (`deploy/check-permissions.sh`: the API user gets `Permission denied`).

**Authorization has to be impossible to forget.** Checking roles inside each handler works until someone
adds an endpoint and forgets. Declaring a policy per route and failing start-up when one is missing
turned "remember to check" into "cannot start without checking". The same object check also has to run
again in the worker, because a user can be revoked between submitting a job and the job running.

**"Atomic" needs a concrete mechanism.** Two requests can each fit within a quota and together exceed it.
Checking and reserving inside one `BEGIN IMMEDIATE` SQLite transaction serializes them; a test with two
threads racing for the last two cores showed exactly one succeeding. I also learned not to hold that
transaction while calling LXD or Google.

**A timeout is not a failure.** When a call to LXD times out, the change may or may not have happened.
Treating that as "failed" would release quota for a container that exists. Separating *rejected* (nothing
happened) from *uncertain* (reconcile against LXD before deciding) was the most important design idea in
the worker, and the reason commands are never retried automatically.

**LXD's settings do not always mean what they look like.** `limits.cpu.allowance: 50%` is only a soft
share under contention; a real cap is `N×P ms / 100 ms`, which I confirmed by reading `cpu.max` inside
the container. On btrfs, `df` inside the container shows the whole pool, so the only real evidence of a
disk quota was writing past it and getting "Quota exceeded".

**Unknown is not zero.** Missing metrics, a collector restart or a counter reset must produce gaps, not
fabricated zeros. Rates need a monotonic clock and a valid previous sample; usage needs a coverage
figure so a gap is never read as idle time.

**Real environments find bugs that mocks cannot.** With 186 passing unit tests, the real runs still found:
LXD's nanosecond timestamps break Python 3.10's date parser; Falcon does not split `a,b` query values;
the dashboard read an operation envelope as the operation, so polling never stopped; the first systemd
install failed on a missing group and on Gunicorn 26's control socket; and the collector raced LXD's
socket at boot. One test only failed on a machine that had been up for more than an hour.

**Measure instead of claiming.** "Lightweight" became numbers: about 0.14 % of one CPU and ~72 MiB for the
three services, and the collector's cost did not change between zero and five open tabs. I also learned
the difference between RSS (counts shared libraries in every process) and PSS (shares them fairly), and
that systemd's cgroup accounting can measure services without root.

**UX problems are often correct behaviour explained badly.** A new admin saw "Insufficient capacity" when
the real cause was their own zero quota; a user's quota showed a container they could not see. Both were
working as designed, but the design was surprising; the fixes were a clearer message and granting the
owner access by default.

**Working with AI agents.** Parallel agents produced a lot of code quickly, but only because the contracts
(schema, API shapes, state keys, file ownership) were written first. Their output still needed review: a
security review found an open redirect and a rate limit one client could exhaust for everyone. The parts
I must be able to explain without help are the transaction boundaries, the privilege boundary, the CPU %
formula and the exec deadline/cleanup logic.

## Bonus Features Implemented

- Container adoption (explicit, safety-checked) for pre-existing containers.
- Hard CPU quota translation (`cores × % ms / 100 ms`) instead of soft shares — verified in `cpu.max`.
- `limits.processes` per container; no swap.
- Default-deny route registry with a start-up check; import-boundary test for the API.
- Durable, idempotent operations with reconciliation of uncertain LXD outcomes.
- systemd units with separate service users; Caddy config for HTTPS mode.
- CI workflow (GitHub Actions: backend on Python 3.10 and 3.12, dashboard check/build/CSP — passing); 187 backend tests; CSP check of the frontend build;
  measurement scripts for `/proc` and systemd cgroups; `deploy/check-permissions.sh` proving the API
  cannot reach the LXD socket.
- Reinstating a revoked user, bound to the same Google account.

## Resource Measurements

**Machine:** Windows 11 laptop, WSL2 Ubuntu 22.04.5 (kernel 6.6.87.2), 10 logical CPUs, 7.6 GiB RAM
visible to WSL; LXD 5.21.8 with a btrfs loop pool. Python 3.10.12.

**Processes measured:** Gunicorn master + one gthread worker (API, `deploy/gunicorn.conf.py`),
`hsm.worker`, `hsm.collector`. LXD itself and the containers are *not* included.

**Method:** `scripts/measure.py` samples `/proc` once per second: CPU = Δ(utime+stime)/wall time, where
100 % = one logical CPU; RSS from `VmRSS`; PSS from `smaps_rollup` (shared pages divided among
sharers, so PSS totals are the fair sum — RSS totals double-count shared libraries).

**A. Same code run as the developer user** (`/proc` sampling; PSS available):

| Scenario | Duration | CPU avg (all app processes) | PSS total | RSS total | Raw data |
|---|---|---|---|---|---|
| Idle, 2 running containers, no browser | 600 s | **0.11 %** of one CPU | **82.2 MiB** | 125.5 MiB | `measurements/idle-2-containers-600s.json` |
| One visible dashboard tab | 300 s | **0.15 %** | 82.8 MiB | 126.2 MiB | `measurements/one-tab-300s.json` |

Per process (idle): API master 12.8 MiB PSS, API worker 31.0 MiB PSS, collector 26.3 MiB PSS,
worker 12.1 MiB PSS; collector CPU 0.06 %, worker 0.03 %, API 0.02 %.

**B. Installed systemd services** (`scripts/measure.py --systemd`: per-unit cgroup `cpu.stat` and
`memory.current`/`anon`; reading other users' PSS would need root):

| Scenario | Duration | Total CPU | API | Worker | Collector | Memory (cgroup / anon) | Raw data |
|---|---|---|---|---|---|---|---|
| No tabs, 2 containers | 300 s | **0.14 %** | 0.03 % | 0.03 % | 0.08 % | 72.2 / 67.8 MiB | `measurements/systemd-no-tabs-300s.json` |
| **Five visible tabs** | 300 s | **0.32 %** | 0.21 % | 0.04 % | **0.07 %** | 73.9 / 68.4 MiB | `measurements/systemd-five-tabs-300s.json` |

The collector's cost does not change with open tabs (0.077 % → 0.073 %); its cycle time stayed
20–22 ms with 0 missed cycles (`measurements/collector-health-snapshots.jsonl`). Only the API's cost
grows: each *visible* tab makes one `/api/containers` request per 10 s (hidden tabs make none).

**Network per tab:** one visible tab made 9 API requests (≈19 KB uncompressed) in its first minute —
3 on load, then one poll every 10 s; five tabs made 47 requests (≈72 KB).

**Payloads** (`measurements/http-payloads.json`; 4 series per chart, `max_points=300`):

| Request | Points/series | Bytes | gzip | p50 / p95 |
|---|---|---|---|---|
| `/api/containers` (2 containers) | — | 2,630 | 796 | 1.4 / 2.1 ms |
| history 1 h (raw, 20 s) | 181 | 14,901 | 868 | 3.3 / 4.9 ms |
| history 24 h (5-min rollups) | 289 | 23,536 | 1,237 | 3.4 / 4.3 ms |
| history 30 d | 299 | 24,334 | 1,576 | 3.5 / 4.0 ms |

Caveats: containers were young, so most long-range points were still null (gzip sizes will grow with
real values; uncompressed size is set by point count); repeated requests hit the collector's small
result cache. Collection cost scaling beyond 2 containers was not measured (a cycle took 20–46 ms).

## Known Limitations

Unfinished, untested or deliberately cut — be explicit about these in the interview:

- **Scale:** exercised with 2–3 containers on one host. Collection took 20–46 ms per cycle; behaviour at
  tens of containers (cycle time, TinyFlux scan time, SQLite write load) was not measured.
- **HTTPS mode** (`HSM_DEPLOYMENT_MODE=https`, `deploy/Caddyfile`, `HSM_TRUSTED_PROXY`) is implemented
  and unit-tested (Secure `__Host-` cookies) but was not deployed; all real runs used `http://localhost`.
- **Terminal:** one command at a time, no PTY, no persistent `cd`. Commands run as root (admins) can leave
  background processes behind (the `timeout` wrapper kills only the foreground process); for normal
  users every uid-1500 process is killed after each command (verified).
- **Memory reduction** checks current use + 64 MiB headroom, but the guest can still allocate between
  the check and the change. Disk is expansion-only (btrfs cannot safely shrink online).
- **External rename** of an LXD instance is handled by the identity marker but was only tested with a
  fake LXD source; external deletion was observed for real (two test containers → tombstoned).
- **Boot race:** right after a WSL boot the collector once got `EACCES` from LXD's socket; units are now
  ordered after `snap.lxd.daemon.unix.socket` and recovery is logged, but a second reboot to confirm the
  ordering was not done. The collector retries every 10 s regardless.
- **WSL:** WSL stops the distro and these services shortly after the last terminal closes; a native
  Ubuntu host does not.
- **Backup/restore** is documented (SQLite backup API, stop collector to copy metrics) but not tested.
- **Invitations** are shared as a one-time link; no email is sent. Reinstatement of a revoked user is
  only possible for the same Google account.
- **Accounting:** unmanaged containers without CPU/memory limits block new allocations until adopted or
  covered by `HSM_EXTERNAL_RESERVE_*`; this is deliberate but strict.
- **Residual privilege risk:** the worker and collector hold root-equivalent LXD access; the API and
  worker share SQLite, so a compromised API can queue jobs (the worker re-validates them).
- **Not implemented (P2):** snapshots, alerts, metric export, persistent web terminal, multi-host.
- **`hsm test-session`** (local-http only) exists for browser tests and measurements; it is a local
  operator command that requires write access to the database, and is audited.

## AI Tool Usage

See [docs/ai-usage.md](docs/ai-usage.md). In short: the implementation was produced by Claude Code
(Claude Opus 5.5) from the candidate's plan, with three parallel specialist sub-agents and one
read-only security reviewer sub-agent; the lead agent reviewed, integrated and verified everything,
and the corrections it made are listed there. The candidate must review the code before the
interview (checklist in docs/interview-walkthrough.md §6).
