# Tasks

- Start: **2026-10-06 21:16 +05:30** (Asia/Colombo)
- Feature freeze: T+14h → 2026-10-07 11:16 · Release target: T+21h → 2026-10-07 18:16 · Hard stop of sprint: 2026-10-07 21:16
- Soft deadline 2026-10-07 (cutoff time unknown), hard deadline 2026-10-09.

## Phase status

| Phase | Gate | Status |
|---|---|---|
| 0 | Environment inspected, blockers raised, LXD spike | Inspection done; LXD spike **blocked on B1** |
| 1 | Foundation: schema, contracts, auth, policy | Done (unit-tested; real Google pending B2) |
| 2 | Lanes: collector, worker, frontend | Code done + integrated; real LXD pending B0/B1 |
| 3 | Real admin→create→invite→user flow | Not started |
| 4 | Exec, limits, history, retention, accounting, audit | Not started |
| 5 | Freeze; security/race/recovery tests | Not started |
| 6 | Clean install, restart, measurements | Not started |
| 7 | README/REPORT/handoff | Not started |

## Ownership

| Area | Owner | Files |
|---|---|---|
| Schema, config, auth, policy, quotas, operations queue, API, integration | Lead | `backend/hsm/{config,db,security,errors,timeutil,lxd_safety}.py`, `backend/hsm/{auth,services,api}/`, `backend/hsm/app.py`, `backend/hsm/cli.py` |
| LXD adapter, worker, exec | Control specialist | `backend/hsm/integrations/`, `backend/hsm/worker.py`, `backend/tests/test_worker*.py`, `backend/tests/test_lxd_adapter*.py` |
| Collector, TinyFlux, history socket, usage | Metrics specialist | `backend/hsm/collector/`, `backend/hsm/history_client.py`, `backend/tests/test_collector*.py` |
| Astro UI | Frontend specialist | `dashboard/` |

## Next step

See docs/agent-handoff.md "Next concrete actions".
