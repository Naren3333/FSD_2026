.PHONY: setup up down test lint build check integration e2e contracts
PYTHON ?= python
setup:
	$(PYTHON) scripts/setup.py
up:
	docker compose up -d --build
down:
	docker compose down
test check lint:
	$(PYTHON) scripts/check.py
build:
	docker compose build
integration:
	$(PYTHON) scripts/integration.py
e2e:
	cd apps/web && npm run test:e2e
contracts:
	$(PYTHON) scripts/export_contracts.py
