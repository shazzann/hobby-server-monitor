# Agent handoff / resume checkpoint

Updated: 2026-10-07 14:45 +05:30

- Repo `F:\Projects\robotic gen`, branch `main`, local commits only (no remote yet: B4).
- Installed deployment on the candidate's WSL: `/opt/hsm`, `/etc/hsm/hsm.env`, `/var/lib/hsm`,
  units `hsm-api`, `hsm-worker`, `hsm-collector` (enabled). Re-run `sudo bash deploy/install.sh` after
  code changes (it restarts the units).
- Real accounts: the candidate's admin Google account and a second (container user) account; one pending
  invitation for a third address.
- Dev/test data dirs (not used by systemd): `~/.local/share/hsm`, `~/.local/share/hsm-e2e`.
- Local helper scripts (git-ignored): `.pytest-tmp/e2e/*.sh`.
- All lanes integrated; tests: Linux 3.10 187 passed; Windows 186 passed + 1 skipped (Unix socket).

## Next concrete actions
1. B4: with approval, `gh repo create shazzann/hobby-server-monitor --private --source . --push`.
2. Optional: second WSL reboot to confirm the LXD socket ordering (look for no `LXD unavailable`).
3. Candidate: "What You Learned", demo, submission email.
