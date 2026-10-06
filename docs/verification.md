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
| LXD | **not installed** (blocked: B0 disk space, B1 sudo) |
| Disk | C: ≈0.5 GB free (B0); repository on F: |

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
| Real LXD spike (create/limits/exec/delete, CPU quota, disk quota, uptime source) | not run | B0, B1 |
| Real Google sign-in with two accounts | not run | B2, B3 |
| Collector/worker/frontend lanes | in progress | — |
| Resource measurements | not run | needs LXD |
