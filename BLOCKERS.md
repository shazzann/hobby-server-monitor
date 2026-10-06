# Blockers

Updated: 2026-10-06 21:35 +05:30

| # | Unmet dependency | Impact | Required input | Independent work still possible |
|---|---|---|---|---|
| B1 | `sudo` in WSL needs a password; LXD and `python3-venv` are not installed | No real LXD spike, no Linux venv, no real worker/collector run | Run `sudo bash scripts/setup-dev-host.sh` inside Ubuntu-22.04 | Schema, auth, policy, API, worker/collector logic against a fake adapter; Astro UI |
| B2 | Google OAuth web client not configured | No real sign-in; Phase 1/3 browser gate cannot pass | Client ID/secret in `.env`, redirect URI `http://localhost:8000/auth/google/callback`, test users | OIDC code with mocked token verification tests |
| B3 | Bootstrap admin identity not confirmed | Bootstrap config | The Google email for the first Admin, plus 1–2 more accounts for invited/uninvited tests | — |
| B4 | No target GitHub repository / push authorization | Cannot push incremental commits | Approval to create a private repo from the template (or an existing URL) | Local commits continue |
| B0 | **C: has ~0.5 GB free** (npm cache 7.5 GB; WSL disk `ext4.vhdx` is on C:) | WSL cannot create swap; LXD install, images and the btrfs pool (sparse, up to 30 GiB) cannot grow; temp files fail | User decision: (a) `npm cache clean --force` (~7.5 GB, re-downloadable) and/or (b) `wsl --shutdown` then `wsl --manage Ubuntu-22.04 --move D:\WSL\Ubuntu-22.04` (WSL 2.6.3 supports it; D: has 64 GB free) **before** running B1 | Tests run with TEMP on F:; Windows-side development continues |
| B5 | Exact soft-deadline cutoff unknown | Schedule risk | Cutoff time from the task email, if stated | — |
