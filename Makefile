.PHONY: setup upgrade_local up down wait_ready test lint build check integration db_tests e2e contracts
PYTHON ?= python
setup:
	$(PYTHON) scripts/setup.py
# Brings a setup made by an older scripts/setup.py up to date; the stack must be up for its database and Keycloak steps.
upgrade_local:
	$(PYTHON) scripts/upgrade_local.py
up:
	docker compose up -d --build
down:
	docker compose down
wait_ready:
	$(PYTHON) scripts/wait_ready.py
test check lint:
	$(PYTHON) scripts/check.py
build:
	docker compose build
integration: wait_ready
	$(PYTHON) scripts/integration.py
db_tests: wait_ready
	$(PYTHON) scripts/db_tests.py
e2e: wait_ready
	cd apps/web && npm run test:e2e
contracts:
	$(PYTHON) scripts/export_contracts.py
