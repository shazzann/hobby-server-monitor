# Agent handoff / resume checkpoint

Updated: 2026-10-06 21:52 +05:30

- Repository: `F:\Projects\robotic gen` (WSL: `/mnt/f/Projects/robotic gen`), branch `main`,
  history based on the current template (`template` remote). No push remote configured (B4).
- Last passed gate: Phase 1 foundation (schema, auth, policy, quotas, API) — unit tests only.
- Active: three background specialists (control, metrics, frontend) editing their own paths;
  only the lead commits.
- Services running: none.
- Test command (Windows): `$env:TEMP='F:\Projects\robotic gen\.pytest-tmp'; .\.venv-win\Scripts\python -m pytest -q` from `backend/`.
- Blockers: see BLOCKERS.md (B0 disk space on C:, B1 LXD install needs sudo, B2/B3 Google OAuth).

## Next concrete actions
1. Integrate specialist output; run the full suite; commit per lane.
2. When LXD exists: Linux venv at `~/.venvs/hsm`, `HSM_DATA_DIR=~/.local/share/hsm`, run the spike,
   then `make dev` and the browser flow.
3. When OAuth exists: bootstrap admin, invite second account, record evidence.
