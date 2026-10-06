# Developer and reviewer commands. Every target does real work and fails honestly.
# Run from the repository root on Ubuntu/WSL. Override VENV to keep the venv on a
# Linux filesystem when the checkout lives on /mnt/<drive>.
VENV ?= .venv
PY := $(VENV)/bin/python
NPM ?= npm

.PHONY: help venv init-db bootstrap build-ui check-ui test verify dev smoke-lxd measure clean-dev

help:
	@grep -E '^[a-z-]+:.*## ' Makefile | sed 's/:.*## /\t/'

venv: ## create the Python virtualenv and install pinned dependencies
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -r backend/requirements-dev.txt
	$(VENV)/bin/pip install -q --no-deps -e backend

init-db: ## create or upgrade the SQLite schema (empty machine -> ready database)
	$(VENV)/bin/hsm init-db

bootstrap: ## print the one-time first-admin setup link
	$(VENV)/bin/hsm bootstrap

build-ui: ## install locked frontend dependencies and build static assets to dashboard/dist
	cd dashboard && $(NPM) ci && $(NPM) run build

check-ui: ## typecheck the dashboard and verify the CSP-safe build output
	cd dashboard && $(NPM) run check && $(NPM) run check:csp

test: ## backend unit and API tests (fake LXD; no host changes)
	cd backend && ../$(PY) -m pytest -q

verify: test build-ui check-ui ## everything that runs without real LXD/Google

dev: ## run API, worker and collector in the foreground (Ctrl-C stops all three)
	VENV=$(VENV) bash scripts/dev.sh

smoke-lxd: ## real LXD checks on disposable hsm-smoke-* containers in project hsm only
	$(PY) scripts/lxd_smoke.py

measure: ## sample CPU/RSS/PSS of the running app processes (see docs/verification.md)
	$(PY) scripts/measure.py --seconds $${SECONDS_TO_SAMPLE:-600}

clean-dev: ## remove local development state (database, metrics) - not LXD containers
	rm -rf data
