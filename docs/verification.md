# Verification evidence

Only checks that were actually run are recorded as passed. Times are Asia/Colombo (+05:30).

## Environment

| Item | Value |
|---|---|
| Host | Windows 11 Pro 10.0.26300, 10 logical CPUs |
| WSL | 2.6.3.0, kernel 6.6.87.2-microsoft-standard-WSL2, Ubuntu 22.04.5, systemd PID 1, cgroup v2 (cpu, memory, pids, io) |
| WSL memory | 7751 MiB total, 2048 MiB swap |
| Python | 3.13.7 (Windows, unit tests); 3.10.12 (WSL target) |
| Node | 22.15.0 (Windows) |
| LXD | 5.21.8 LTS (snap rev 40958), pool `hsm-btrfs` (btrfs, loop, 30 GiB), bridge `hsmbr0` 10.46.67.1/24, restricted project `hsm` (installed by the user with `scripts/setup-dev-host.sh` on 2026-10-07) |
| Images | `hsm/alpine-3.22` (6cd53e54490b), `hsm/ubuntu-24.04` (ee3016f85cc4) |
| Linux venv | `~/.venvs/hsm`, Python 3.10.12, pinned `requirements-dev.txt` |
| Disk | C: ≈11 GB free after the user freed space; repository on F: |

## Results

| When | Check | Command | Result |
|---|---|---|---|
| 2026-10-06 21:16 | Environment inspection | `wsl … cat /etc/os-release; snap list; sudo -n true; …` | Recorded above; sudo needs a password; LXD absent |
| 2026-10-06 21:2x | Kernel capability check | `/proc/filesystems`, `/proc/config.gz`, cgroup controllers | `CONFIG_BTRFS_FS=m`, `CONFIG_BLK_DEV_LOOP=y`, `CONFIG_CFS_BANDWIDTH=y`, cgroup2 with cpu/memory/pids → btrfs loop pool and hard CPU quota feasible (unverified until LXD exists) |
| 2026-10-06 21:3x | Migration from empty DB, idempotent re-run, FK + WAL on | `pytest tests/test_db.py` (Windows venv) | **pass** (2) |
| 2026-10-06 21:4x | Auth/policy/admission/CSRF/cross-user/last-admin/creation validation | `pytest tests/test_api_security.py` | **pass** (23) |
| 2026-10-06 21:4x | Quota accounting incl. two-thread race for one budget | `pytest tests/test_quotas.py` | **pass** (11) |

| 2026-10-06 21:58 | Dashboard build, typecheck, CSP scan | `npm run check && npm run build && npm run check:csp` | **pass**: 0 TS errors; 10 HTML + 15 assets, no inline scripts/styles/handlers/external origins; JS 31.4 KiB gzip total |
| 2026-10-06 22:00 | **UI wiring check** against the real Falcon API (Windows dev server) with a *seeded* SQLite DB and the real worker loop driving the in-memory **FakeAdapter** — not LXD evidence | browser pane + `fetch` | admin overview renders host allocation, unmanaged/unsafe labels and the honest "LXD unavailable" banner; container user sees only the assigned container, the API returns 403 for an unassigned id and the UI shows "Access denied"; detail page shows stale banner; exec as the user returned 202 → `succeeded`, ran as `hsm` (non-root), output returned verbatim (HTML payload kept as text), idempotent replay returned the same operation (200) |
| 2026-10-06 22:00 | Defect found by the browser check | history `metrics=a,b` → 422 | Falcon 4 does not split comma-separated query values; fixed in `api/containers.py:History` + regression test `test_history_accepts_comma_separated_metrics` |
| 2026-10-06 22:01 | Full backend suite | `pytest -q` (Windows) | **pass**: 174 passed, 1 skipped (Unix-socket test; passes on Linux 3.10 per metrics lane) |

Observation: the dev server on Windows shows ~200–300 ms TCP connect time per request
(`localhost` → IPv6 first, server bound to 127.0.0.1, wsgiref without keep-alive); server
time-to-first-byte was 2–3 ms. Performance figures will be taken on Linux, not from this setup.

| 2026-10-07 06:2x | Full backend suite on **Linux Python 3.10.12** (incl. Unix-socket peer-UID test) | `~/.venvs/hsm/bin/python -m pytest -q` | **pass**: 184 passed |
| 2026-10-07 06:2x | **Real LXD spike** on disposable `hsm-spike-d87dc7d5` (project `hsm`, deleted afterwards) | `python scripts/lxd_spike.py` | **pass** — create 502 ms; adapter-built config has only `root`+`eth0`, `security.privileged/nesting=false`, `limits.memory.swap=false`, `limits.processes=500`; **CPU hard quota verified**: 1 core × 50 % → `cpu.max` = `50000 100000`, live change to 2 × 50 % → `100000 100000`; live memory change → cgroup limit 536870912; disk grown 1024→2048 MiB live; `last_used_at` changes on restart (uptime source verified); exec as root (exit 3) and as `uid=1500(hsm)` (exit 4) with separate stdout/stderr and the fixed env; guest not in `wheel`; 64 KiB output cap → `stdout_truncated=true`; `sleep` past a 5 s deadline → `timed_out` (exit 137) for root and guest; safety evaluation of the live instance: no reasons |
| 2026-10-07 06:3x | **Root-disk quota enforcement** (btrfs qgroups) on disposable `hsm-qcheck-a66f2b4e` | `EXEC_DEADLINE_SECONDS=5 python scripts/lxd_quota_check.py` | **pass** — `dd` of 1536 MiB into a 1024 MiB root: `dd: error writing '/root/fill': Quota exceeded` after 1013.5 MB. Note: `df` inside the container shows the whole pool, so `df` is not evidence of the quota |
| 2026-10-07 06:3x | **No guest process survives a deadline** | same script: `sleep 300 & sleep 300` as uid 1500 | **pass** — `timed_out` after 6.2 s; `ps` afterwards: no uid-1500 processes (`none-left`) |

| 2026-10-07 06:25 | **Full stack on real LXD** (collector + worker + API via `scripts/dev.sh`, separate test data dir, test sessions from `hsm test-session` because Google is not configured yet) | `/api/health`, `/api/creation-options` | collector cycle 4 ms, `lxd_available=true`, worker heartbeat fresh; options discovered from LXD: images `hsm/alpine-3.22`, `hsm/ubuntu-24.04`; pool `hsm-btrfs` (btrfs, quota-capable); network `hsmbr0`; bounds = min(owner quota, host − reserves) |
| 2026-10-07 06:26 | **Real E2E flow** (scripted HTTP client; container `e2e-969e3d`) | `.pytest-tmp/e2e/flow.py` (local helper) | create → `succeeded` in 1.5 s; second create over the user's 2-core quota → 409 `OUT_OF_BOUNDS` (`cpu_cores` allowed 1..1); user GET before assignment → **403**; after assignment the user's list contains only that container and live metrics (IPv4 10.46.67.129, 5 processes); user exec → ran as `uid=1500(hsm)`, exit 7, stderr separate, 1.5 s incl. queueing; admin limits 2 cores × 100 %, 512 MiB → `succeeded` in 1.0 s, cgroup `cpu.max 200000 100000`, `memory.max 536870912`; restart → `succeeded` 4.6 s; after unassignment user detail **403** and exec **403** immediately |
| 2026-10-07 06:27 | Defect found on real LXD | uptime `started_at_unparsable` | LXD `last_used_at` has nanoseconds; Python 3.10 `fromisoformat` accepts only 3/6 fraction digits → `timeutil.parse` normalises to 6 digits; test `tests/test_timeutil.py`; Linux suite 185 passed; after restart uptime = 107 s with quality `uptime_source_last_used_at` |
| 2026-10-07 06:28 | **Collector independence** (API and worker stopped, no browser) | `.pytest-tmp/e2e/independence.sh`: collector alone; read heartbeat/latest sample with `sqlite3` and count raw TinyFlux rows every 10 s | **pass** — API unreachable (`000`); heartbeat and `latest_metrics.sampled_at` advanced 00:58:33 → :43 → :53 → 00:59:05; cycle 31–46 ms; `missed_cycles=0`; raw rows 15 → 18 (history continued from the previous run's shard); SIGTERM → clean stop, exit 0 |

pylxd 2.4.2 prints a harmless `UserWarning: unknown attribute "requestor" on Operation` with LXD 5.21.

### What the security tests assert (backend/tests/test_api_security.py)
- every route declares a policy; a responder without one fails start-up;
- unauthenticated → 401 on private routes, `Cache-Control: no-store`;
- logout revokes the server session; the old cookie is rejected afterwards;
- unsafe methods need the session's CSRF token *and* `Origin: PUBLIC_BASE_URL`;
- revocation and demotion take effect on the next request;
- last active admin cannot be demoted or revoked;
- a user lists only assigned containers (SQL filter); detail/history/usage/exec of an
  unassigned container → 403; unknown id → 404; admin-only routes → 403;
- operation results: other users 403; admin sees status but not another user's exec output;
  the actor loses access to the result after unassignment;
- uninvited Google account → `NOT_INVITED`; returning users matched by `sub`, not email;
  unverified email, forged nonce, wrong audience rejected; OAuth state single-use and
  browser-bound (wrong binding consumes the state);
- invitation: wrong email rejected, correct email admitted and bound to `sub`, replay rejected;
  only the hash of the invitation secret is stored; cross-origin admission POST rejected;
- bootstrap: wrong email rejected, configured email + secret admitted as admin, replay rejected,
  re-issuing a bootstrap secret refused afterwards;
- admission endpoint rate-limited (429 after 10 attempts / 10 min / peer);
- create rejects unknown fields (`raw.lxc`), invalid names, non-offered images/pools;
  idempotent replay returns the same operation; same key + different body → 409;
- static file serving cannot escape `dashboard/dist`.

Google ID tokens are **mocked** in these tests (exchange and signature verification are
replaced); a real Google sign-in has not been performed yet (blocked: B2).

## Not yet run / blocked

| Check | Status | Blocker |
|---|---|---|
| Real Google sign-in with two accounts | not run | B2, B3 |
| Collector/worker/frontend lanes | in progress | — |
| Resource measurements | not run | needs LXD |
