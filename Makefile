PY ?= .venv/bin/python

.PHONY: setup sandbox test test-fast web dev models verify-sandbox seeds

setup:
	python3 -m venv .venv && .venv/bin/pip install -e 'backend[dev]'

sandbox:
	docker build -t bisect-sandbox:py3.11 --build-arg PY=3.11 sandbox/

test:
	cd backend && ../.venv/bin/pytest -q

test-fast:
	cd backend && ../.venv/bin/pytest -q -m "not docker and not network"

web:
	cd web && npm ci && npm run build

dev:
	cd backend && ../.venv/bin/uvicorn bisect_agent.server:app --reload --port 8000

models:
	cd backend && ../.venv/bin/bisect-agent models

verify-sandbox:
	cd backend && ../.venv/bin/python ../scripts/verify_sandbox_seeds.py

seeds:
	cd backend && ../.venv/bin/python ../scripts/run_seeds.py
