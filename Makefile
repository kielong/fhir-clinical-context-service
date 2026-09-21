# ORIGIN: AI — drafted by Claude Code, reviewed by Kiel
.PHONY: install lock up down logs test lint format smoke

install:  ## create .venv (Python 3.12) and install the locked versions + the project + dev tools
	python3.12 -m venv .venv
	.venv/bin/pip install -r requirements.lock -e '.[dev]'

lock:  ## re-pin requirements.lock from pyproject.toml (run after changing its dependencies)
	.venv/bin/pip-compile --strip-extras --quiet --output-file=requirements.lock pyproject.toml

up:  ## build and start the whole stack in the background
	docker compose up --build -d

down:  ## stop the stack; volumes (Postgres data, Ollama models) are KEPT. Never add -v.
	docker compose down

logs:
	docker compose logs -f --tail=100

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy

format:
	.venv/bin/ruff check --fix . && .venv/bin/ruff format .

smoke:  ## prove the running stack works: /health and one packet
	scripts/smoke.sh
