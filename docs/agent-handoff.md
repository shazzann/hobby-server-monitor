# Agent handoff / resume checkpoint

Updated: 2026-10-06 ~22:10 +05:30 (sprint start 21:16)

- Repo `F:\Projects\robotic gen`, branch `main`, local commits only (no push remote: B4). Working tree clean after this commit.
- Last passed gate: Phases 1–2 code complete for all lanes (API/auth/policy/quotas, worker+exec, collector+TinyFlux, Astro UI, adopt, CI);
  backend 183 passed / 1 skipped on Windows Python 3.13 with fake LXD; dashboard check/build/CSP pass; UI wiring checked in browser
  against the real API with a seeded DB + FakeAdapter (docs/verification.md).
- NOT done (blocked): real LXD spike (`scripts/lxd_spike.py`), real Google sign-in, Linux venv/clean install, systemd install,
  measurements, REPORT.md. Blockers B0 (C: full), B1 (sudo/LXD), B2/B3 (OAuth, admin email), B4 (GitHub), B5 (cutoff) in BLOCKERS.md.
- Specialist agents: all finished; none running.
- Local scratch (git-ignored): `.pytest-tmp/uidev/{seed.py,fake_worker.py,data}` UI wiring fixture; `.venv-win` Windows venv.

## Next concrete actions (in order)
1. After B0/B1: `python3 -m venv ~/.venvs/hsm && ~/.venvs/hsm/bin/pip install -r backend/requirements-dev.txt && ~/.venvs/hsm/bin/pip install --no-deps -e backend`;
   `HSM_DATA_DIR=~/.local/share/hsm`; run `pytest` on Linux 3.10; run `scripts/lxd_spike.py`; record results.
2. `make dev` in WSL with real LXD; create/limits/exec/delete via browser; verify collector independence (API stopped).
3. After B2/B3: `hsm bootstrap`, real Google admin sign-in, invite second account, uninvited third account denied.
4. `sudo bash deploy/install.sh`; API-cannot-reach-LXD check; reboot test; `scripts/measure.py` idle/1-tab/5-tab; `scripts/bench_http.py`.
5. Write REPORT.md with actual numbers; finalize README; push after B4 approval.
