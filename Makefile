# Sanctuary Development Makefile

PYTHON_EXE ?= $(shell command -v python3.12 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null)
PYTHON := .venv/bin/python
UVICORN := .venv/bin/uvicorn
PYTEST := $(PYTHON) -m pytest
PRECOMMIT := .venv/bin/pre-commit
ALEMBIC := .venv/bin/alembic

# Single source of truth for the two-queue Celery split (see app/tasks/celery_app.py).
CELERY := $(PYTHON) -m celery -A app.tasks.celery_app
INGEST_WORKER := $(CELERY) worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4
AI_WORKER := $(CELERY) worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3
BEAT := $(CELERY) beat --loglevel=INFO

.PHONY: help setup run run-stable run-debug server worker worker-ingest worker-ai watch-css test test-unit test-integration test-e2e test-e2e-isolated seed migrate lint clean redis db-up prod prod-down _check-no-celery

test: ## Run all tests (excludes E2E)
	rm -rf .pytest_cache __pycache__ app/__pycache__ app/*/__pycache__ app/*/*/__pycache__ 2>/dev/null || true
	$(PYTEST) --ignore=tests/e2e -p no:cacheprovider

test-e2e: ## Run E2E tests (requires running server on localhost:8000)
	$(PYTEST) -m e2e

test-e2e-isolated: db-up ## Run E2E tests against a fully throwaway server + DB (mirrors CI; never touches your dev data)
	@echo "Resetting throwaway e2e DB..."
	@docker compose exec -T db psql -U $(POSTGRES_USER) -d postgres -c "DROP DATABASE IF EXISTS sanctuary_test_e2e" >/dev/null
	@docker compose exec -T db psql -U $(POSTGRES_USER) -d postgres -c "CREATE DATABASE sanctuary_test_e2e" >/dev/null
	@echo "Migrating throwaway DB..."
	@DATABASE_URL="postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:5432/sanctuary_test_e2e" $(ALEMBIC) upgrade head
	@echo "Starting throwaway app server..."
	@DATABASE_URL="postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:5432/sanctuary_test_e2e" \
	 CELERY_TASK_ALWAYS_EAGER=true \
	 AUTH_ENABLED=false \
	 SESSION_SECRET="e2e-isolated-ephemeral-not-a-real-secret" \
	 $(UVICORN) app.main:app --host 127.0.0.1 --port 8000 > /tmp/sanctuary_test_e2e_server.log 2>&1 & \
	 echo $$! > /tmp/sanctuary_test_e2e_server.pid; \
	 trap 'kill "$$(cat /tmp/sanctuary_test_e2e_server.pid)" 2>/dev/null; rm -f /tmp/sanctuary_test_e2e_server.pid' EXIT INT TERM; \
	 for i in $$(seq 1 30); do curl -sf http://127.0.0.1:8000 >/dev/null && break; sleep 1; done; \
	 DATABASE_URL="postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@localhost:5432/sanctuary_test_e2e" $(PYTEST) -m e2e; \
	 status=$$?; \
	 echo "--- throwaway server log (tail) ---"; tail -50 /tmp/sanctuary_test_e2e_server.log || true; \
	 exit $$status

-include .env
export

# Defaults if not set in .env
HOST ?= 127.0.0.1
PORT ?= 8000
POSTGRES_USER ?= sanctuary
POSTGRES_PASSWORD ?= sanctuary
POSTGRES_DB ?= sanctuary

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' Makefile | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.venv: ## Create virtual environment
	@test -n "$(PYTHON_EXE)" || (echo "ERROR: No python3.12/python3/python found on PATH. Install Python 3.12 first." && exit 1)
	$(PYTHON_EXE) -m venv .venv
	$(PYTHON) -m pip install --upgrade pip

setup: .venv ## Install dependencies (prod + dev/test) and pre-commit hooks
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m playwright install chromium
	npm install
	$(PRECOMMIT) install
	$(PRECOMMIT) install --hook-type pre-push

redis: ## Start Redis (Docker)
	docker compose up redis -d --wait

db-up: ## Start Postgres (Docker)
	docker compose up db -d --wait

prod: ## Start the full stack via Docker Compose (docker-compose.yml + docker-compose.override.yml, if present)
	docker compose pull
	docker compose up -d

prod-down: ## Stop the stack started by `make prod`
	docker compose down

_check-no-celery: ## Internal: refuse to start if celery beat/workers already running
	@if pgrep -f "celery -A app.tasks.celery_app (beat|worker)" >/dev/null 2>&1; then \
		echo "ERROR: celery beat or worker already running:"; \
		pgrep -fa "celery -A app.tasks.celery_app (beat|worker)" | sed 's/^/  /'; \
		echo "Kill them first, then re-run. Otherwise you get duplicate beats (every scheduled task fires N× → recover-pipeline storms)."; \
		exit 1; \
	fi

run: _check-no-celery db-up redis ## Start Postgres+Redis, web server, ingest worker (OCR), AI worker, and beat scheduler
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload & \
	$(INGEST_WORKER) & \
	$(BEAT) & \
	trap 'kill 0' EXIT INT TERM; \
	$(AI_WORKER)

run-stable: _check-no-celery db-up redis ## Start without --reload (use for ingestion/pipeline testing — avoids recovery loops)
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) & \
	$(INGEST_WORKER) & \
	$(BEAT) & \
	trap 'kill 0' EXIT INT TERM; \
	$(AI_WORKER)

run-debug: _check-no-celery db-up redis ## Start server with DEBUG logging (+ Postgres, Redis, both workers)
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload --log-level debug & \
	LOG_LEVEL=debug DEBUG=True $(INGEST_WORKER) & \
	$(BEAT) & \
	trap 'kill 0' EXIT INT TERM; \
	LOG_LEVEL=debug DEBUG=True $(AI_WORKER)

server: ##  web server
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload

worker: _check-no-celery ## Start both Celery workers (ingest + ai) and beat scheduler
	@$(INGEST_WORKER) & \
	$(BEAT) & \
	trap 'kill 0' EXIT INT TERM; \
	$(AI_WORKER)

worker-ingest: ## Start only the ingest (OCR) Celery worker
	$(INGEST_WORKER)

worker-ai: ## Start only the AI Celery worker (LLM/embeddings/light I/O)
	$(AI_WORKER)

watch-css: ## Watch and build Tailwind CSS v4
	npx @tailwindcss/cli -i static/input.css -o static/styles.css --watch

test-unit: ## Run unit tests
	$(PYTEST) --ignore=tests/e2e -m unit

test-integration: ## Run integration tests
	$(PYTEST) --ignore=tests/e2e -m integration

seed: db-up ## Reset database (drop+recreate schema) and seed with advanced triage combinations
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head
	$(PYTHON) scripts/seed_dummy_data.py

migrate: db-up ## Run database migrations
	$(ALEMBIC) upgrade head

lint: ## Run pre-commit hooks on all files
	$(PRECOMMIT) run --all-files

clean: ## Clean up temporary files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
