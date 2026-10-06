# Blockers

Updated: 2026-10-06 21:35 +05:30

| # | Unmet dependency | Impact | Required input | Independent work still possible |
|---|---|---|---|---|
| B1 | `sudo` in WSL needs a password; LXD and `python3-venv` are not installed | No real LXD spike, no Linux venv, no real worker/collector run | Run `sudo bash scripts/setup-dev-host.sh` inside Ubuntu-22.04 | Schema, auth, policy, API, worker/collector logic against a fake adapter; Astro UI |
| B2 | Google OAuth web client not configured | No real sign-in; Phase 1/3 browser gate cannot pass | Client ID/secret in `.env`, redirect URI `http://localhost:8000/auth/google/callback`, test users | OIDC code with mocked token verification tests |
| B3 | Bootstrap admin identity not confirmed | Bootstrap config | The Google email for the first Admin, plus 1–2 more accounts for invited/uninvited tests | — |
| B4 | No target GitHub repository / push authorization | Cannot push incremental commits | Approval to create a private repo from the template (or an existing URL) | Local commits continue |
| B5 | Exact soft-deadline cutoff unknown | Schedule risk | Cutoff time from the task email, if stated | — |
