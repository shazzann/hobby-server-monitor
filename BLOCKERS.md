# Blockers

Updated: 2026-10-06 21:35 +05:30

| # | Unmet dependency | Impact | Required input | Independent work still possible |
|---|---|---|---|---|
| B1 | ~~LXD not installed~~ resolved 2026-10-07 (user ran setup-dev-host.sh) | — | — | — |
| B2 | Google OAuth web client not configured | No real sign-in; Phase 1/3 browser gate cannot pass | Client ID/secret in `.env`, redirect URI `http://localhost:8000/auth/google/callback`, test users | OIDC code with mocked token verification tests |
| B3 | Bootstrap admin identity not confirmed | Bootstrap config | The Google email for the first Admin, plus 1–2 more accounts for invited/uninvited tests | — |
| B4 | No target GitHub repository / push authorization | Cannot push incremental commits | Approval to create a private repo from the template (or an existing URL) | Local commits continue |
| B0 | ~~C: full~~ resolved 2026-10-07 by the user (≈11 GB free) | — | — | — |
| B5 | Exact soft-deadline cutoff unknown | Schedule risk | Cutoff time from the task email, if stated | — |
