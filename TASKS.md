# Tasks

- Start: **2026-10-06 21:16 +05:30** (Asia/Colombo). Soft deadline 2026-10-07 (cutoff unknown), hard deadline 2026-10-09.

## Phase status

| Phase | Gate | Status |
|---|---|---|
| 0 | Environment, blockers, real LXD spike | **Passed** (LXD 5.21.8, btrfs quotas, hard CPU quota, exec verified) |
| 1 | Foundation: schema, contracts, auth, policy | **Passed** (real Google bootstrap + invitation with the candidate's accounts) |
| 2 | Collector, worker, frontend | **Passed** (real metrics, history, collector independence) |
| 3 | Admin → create → invite → user flow | **Passed** (scripted on real LXD + candidate's browser walkthrough) |
| 4 | Exec, limits, history, retention, accounting, audit | **Passed** (retention verified with controlled timestamps in tests only) |
| 5 | Security/race/recovery tests | **Passed** (cross-review fixes, quota race test, reconciliation tests) |
| 6 | Clean install, restart, measurements | **Passed** (clean checkout, systemd + reboot, idle/1-tab/5-tab, payloads) |
| 7 | README/REPORT/handoff | **Done**; candidate to write "What You Learned" |

## Remaining (needs the candidate)

- [x] GitHub: private repo https://github.com/shazzann/hobby-server-monitor created and pushed; CI green.
- [ ] Write "What You Learned" in REPORT.md in your own words.
- [ ] Record a short demo (optional but useful).
- [ ] Share the repository with dev@roboticgen.co and reply to the task email.
- [ ] Review the code with docs/interview-walkthrough.md §6.
