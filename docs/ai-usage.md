# AI usage log

The brief encourages AI tools and asks for transparency. This project was implemented
by an AI coding agent (Claude Code, model Claude Opus 5.5) working from the candidate's
`plan.md`, under the candidate's supervision. The candidate supplied the plan, the
environment, credentials and approvals; the agent wrote the code and documents listed
in git history (commits carry a `Co-Authored-By: Claude` trailer).

What the candidate still has to do personally is listed in `docs/interview-walkthrough.md`
("Review before the interview"): read every module, rerun the checks, and be able to
explain the decisions without the assistant.

## Structure of the work

- **Lead agent** (one session): environment inspection, schema/migrations, configuration,
  auth (OIDC, admission, sessions, CSRF), route/object policy, quota accounting,
  operation queue, admin API, deployment files, docs, integration, verification.
- **Three specialist sub-agents** launched by the lead, running in parallel with strict file
  ownership and no git access: control (LXD adapter, worker, exec), metrics (collector,
  TinyFlux store, history socket), frontend (Astro dashboard). The lead reviewed and
  integrated their output and ran the checks itself. Wall-clock time and agent effort
  overlap; REPORT.md separates them.

## Corrections made during implementation (actual, in order)

| Area | What was wrong first | Fix |
|---|---|---|
| Idempotency | Request hash included the freshly minted container id for creates, so a replay could never match | Hash the client's request body; create hashes without the container id (`operations.request_hash`) |
| Idempotency | Replays were re-validated before the key lookup, so a replayed create failed with `NAME_TAKEN` and a replayed action with `INVALID_STATE` | `find_replay` runs before validation in every `submit_*` (`services/containers.py`) |
| Idempotency | Replay check and submit hashed actions under different scopes | explicit `hash_scope="action"` |
| Rate limiting | First draft keyed the limiter on `X-Forwarded-For` (client-controlled) | socket peer address only (`api/common.py:client_ip`) |
| Falcon | Global trailing-slash stripping would have looped with the static directory redirect | removed; API routes have no trailing slash |
| Config | Relative `HSM_DATA_DIR` depended on the process's working directory | anchored at the repository root (`config._path`) |
| OIDC tests | Default arguments bound the real Google functions at import time, so tests could not substitute them | resolved at call time |
| Quota UX | "must be between 1 and 0" when no capacity remained | explicit "nothing available" message |

## Verified library behaviour (read from installed sources, not assumed)

- pylxd 2.4.2 `Instance.execute`: output handlers receive each chunk and disable buffering;
  the call blocks in `wait_for_operation` with no timeout → deadline enforced outside.
- TinyFlux 1.2.0: field values may be `int`, `float` or `None`.
- Authlib 1.8.0: `create_authorization_url(..., code_verifier=...)` adds an S256 challenge;
  `fetch_token(..., code_verifier=..., timeout=...)` passes both through.
- Falcon 4.4: error handlers are chosen by exception MRO and `HTTPStatus` (redirects) has its
  own default handler, so the catch-all `Exception` handler does not swallow redirects.

## Rejected suggestions / scope cuts
(updated as they happen)
