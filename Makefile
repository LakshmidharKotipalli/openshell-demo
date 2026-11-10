PY := .venv/bin/python
PORT ?= 8000

.PHONY: help install setup preflight run replay test e2e record reset hard-reset

help:
	@echo "make install     install pinned OpenShell $$(cat .openshell-version) + bridge networking (one time)"
	@echo "make setup       venv, provider profile, provider (reads OPENROUTER_API_KEY from .env)"
	@echo "make preflight   PASS/FAIL checklist before the demo"
	@echo "make run         start the console on http://127.0.0.1:$(PORT)"
	@echo "make replay      same, but replays a recorded run (no model / sandbox needed)"
	@echo "make test        pytest (log parsing, policy rule generation)"
	@echo "make e2e         scripted end-to-end flow against a running 'make run'"
	@echo "make record      record a real run to recordings/success.json (needs 'make run')"
	@echo "make reset       delete demo sandboxes, restore clean state"

install:
	sh scripts/install-openshell.sh

setup:
	sh scripts/setup.sh

preflight:
	sh scripts/preflight.sh

run:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT)

replay:
	REPLAY=1 $(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT)

test:
	$(PY) -m pytest -q tests

e2e:
	$(PY) scripts/e2e.py --url ws://127.0.0.1:$(PORT)/ws

record:
	$(PY) scripts/e2e.py --url ws://127.0.0.1:$(PORT)/ws --record recordings/success.json

reset:
	sh scripts/reset.sh

hard-reset:
	sh scripts/reset.sh --hard
