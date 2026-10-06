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
