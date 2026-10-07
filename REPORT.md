# Final Report — Hobby Server Monitor

Status at the time of writing: **draft, being completed during the sprint**. Every number below
was measured on the machine described in "Resource Measurements"; anything not yet verified is
marked as such. Evidence log: [docs/verification.md](docs/verification.md).

## Time Spent

Sprint started 2026-10-06 21:16 (Asia/Colombo). Work was done by an AI coding agent (lead) plus
three parallel specialist sub-agents for ~25 minutes each, under the candidate's supervision;
wall-clock time and agent effort overlap, so both are given.

| Area | Wall-clock (approx.) | Notes |
|---|---|---|
| Environment inspection, LXD host script, blockers | 0.5 h | WSL2 + systemd present; LXD absent; C: drive full (resolved by the candidate) |
| Backend foundation, auth, policy, quotas, API | 1.0 h (lead) | in parallel with the three lanes below |
| LXD adapter, worker, exec (control specialist) | ~0.3 h | parallel |
| Collector, TinyFlux, history socket (metrics specialist) | ~0.4 h | parallel |
| Astro dashboard (frontend specialist) | ~0.4 h | parallel |
| Security cross-review + fixes | 0.3 h | 5 confirmed defects fixed |
| Waiting for host prerequisites (disk, LXD install, OAuth) | overnight | not counted as work |
| Real-LXD verification, E2E, browser checks, measurements | 1.0 h+ | 2026-10-07 from 06:20 |
| Documentation / report | ongoing | |

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

## What You Learned

(To be written by the candidate.)

## Bonus Features Implemented

- Container adoption (explicit, safety-checked) for pre-existing containers.
- Hard CPU quota translation (`cores × % ms / 100 ms`) instead of soft shares — verified in `cpu.max`.
- `limits.processes` per container; no swap.
- Default-deny route registry with a start-up check; import-boundary test for the API.
- Durable, idempotent operations with reconciliation of uncertain LXD outcomes.
- systemd units with separate service users; Caddy config for HTTPS mode.
- CI workflow; 185 backend tests; CSP check of the frontend build; measurement scripts.

## Resource Measurements

**Machine:** Windows 11 laptop, WSL2 Ubuntu 22.04.5 (kernel 6.6.87.2), 10 logical CPUs, 7.6 GiB RAM
visible to WSL; LXD 5.21.8 with a btrfs loop pool. Python 3.10.12.

**Processes measured:** Gunicorn master + one gthread worker (API, `deploy/gunicorn.conf.py`),
`hsm.worker`, `hsm.collector`. LXD itself and the containers are *not* included.

**Method:** `scripts/measure.py` samples `/proc` once per second: CPU = Δ(utime+stime)/wall time, where
100 % = one logical CPU; RSS from `VmRSS`; PSS from `smaps_rollup` (shared pages divided among
sharers, so PSS totals are the fair sum — RSS totals double-count shared libraries).

| Scenario | Duration | CPU avg (all app processes) | PSS total | RSS total | Raw data |
|---|---|---|---|---|---|
| Idle, 2 running containers, no browser | 600 s | **0.11 %** of one CPU | **82.2 MiB** | 125.5 MiB | `measurements/idle-2-containers-600s.json` |
| One dashboard tab | 300 s | _pending_ | | | `measurements/one-tab-300s.json` |
| Five dashboard tabs | 300 s | _pending_ | | | |

Per process (idle): API master 12.8 MiB PSS, API worker 31.0 MiB PSS, collector 26.3 MiB PSS,
worker 12.1 MiB PSS; collector CPU 0.06 %, worker 0.03 %, API 0.02 %.

One visible tab: 9 API requests and ≈19 KB (uncompressed) in its first minute — 3 on load, then one
`/api/containers` every 10 s.

## Known Limitations

(Being completed; see docs/interview-walkthrough.md §5.)

- Real Google sign-in not yet performed (pending the candidate's OAuth client).
- systemd deployment and reboot recovery not yet exercised on this host.

## AI Tool Usage

See [docs/ai-usage.md](docs/ai-usage.md). In short: the implementation was produced by Claude Code
(Claude Opus 5.5) from the candidate's plan, with three parallel specialist sub-agents and one
read-only security reviewer sub-agent; the lead agent reviewed, integrated and verified everything,
and the corrections it made are listed there. The candidate must review the code before the
interview (checklist in docs/interview-walkthrough.md §6).
