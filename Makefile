PY ?= .venv/bin/python

.PHONY: setup sandbox test test-fast web dev

setup:
	python3 -m venv .venv && .venv/bin/pip install -e 'backend[dev]'

sandbox:
	docker build -t bisect-sandbox:py3.11 --build-arg PY=3.11 sandbox/

test:
	cd backend && ../.venv/bin/pytest -q

test-fast:
	cd backend && ../.venv/bin/pytest -q -m "not docker and not network"
