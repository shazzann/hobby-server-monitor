#!/usr/bin/env bash
# Development runner: API (dev WSGI server), worker and collector as three
# separate processes, exactly like production but in one terminal.
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=${VENV:-.venv}
PY="$VENV/bin/python"

"$VENV/bin/hsm" init-db
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

"$PY" -m hsm.collector & pids+=($!)
"$PY" -m hsm.worker & pids+=($!)
"$VENV/bin/hsm" serve-dev & pids+=($!)
wait -n
