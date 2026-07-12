# Sanctuary Development Makefile

PYTHON_EXE ?= $(shell command -v python3.12 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null)
PYTHON := .venv/bin/python
UVICORN := .venv/bin/uvicorn
PYTEST := $(PYTHON) -m pytest
PRECOMMIT := .venv/bin/pre-commit
ALEMBIC := .venv/bin/alembic

.PHONY: help setup run run-stable run-debug server worker worker-ingest worker-ai watch-css test test-unit test-integration test-e2e seed migrate lint clean redis db-up _check-no-celery

test: ## Run all tests (excludes E2E)
	rm -rf .pytest_cache __pycache__ app/__pycache__ app/*/__pycache__ app/*/*/__pycache__ 2>/dev/null || true
	$(PYTEST) --ignore=tests/e2e -p no:cacheprovider

test-e2e: ## Run E2E tests (requires running server on localhost:8000)
	$(PYTEST) -m e2e

-include .env
export

# Defaults if not set in .env
HOST ?= 127.0.0.1
PORT ?= 8000

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

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

_check-no-celery: ## Internal: refuse to start if celery beat/workers already running
	@if pgrep -f "celery -A app.tasks.celery_app (beat|worker)" >/dev/null 2>&1; then \
		echo "ERROR: celery beat or worker already running:"; \
		pgrep -fa "celery -A app.tasks.celery_app (beat|worker)" | sed 's/^/  /'; \
		echo "Kill them first, then re-run. Otherwise you get duplicate beats (every scheduled task fires N× → recover-pipeline storms)."; \
		exit 1; \
	fi

run: _check-no-celery ## Start Postgres+Redis, web server, ingest worker (OCR), AI worker, and beat scheduler
	docker compose up db redis -d --wait
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload & \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4 & \
	$(PYTHON) -m celery -A app.tasks.celery_app beat --loglevel=INFO & \
	trap 'kill 0' EXIT INT TERM; \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3

run-stable: _check-no-celery ## Start without --reload (use for ingestion/pipeline testing — avoids recovery loops)
	docker compose up db redis -d --wait
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) & \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4 & \
	$(PYTHON) -m celery -A app.tasks.celery_app beat --loglevel=INFO & \
	trap 'kill 0' EXIT INT TERM; \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3

run-debug: _check-no-celery ## Start server with DEBUG logging (+ Postgres, Redis, both workers)
	docker compose up db redis -d --wait
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload --log-level debug & \
	LOG_LEVEL=debug DEBUG=True $(PYTHON) -m celery -A app.tasks.celery_app worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4 & \
	$(PYTHON) -m celery -A app.tasks.celery_app beat --loglevel=INFO & \
	trap 'kill 0' EXIT INT TERM; \
	LOG_LEVEL=debug DEBUG=True $(PYTHON) -m celery -A app.tasks.celery_app worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3

server: ##  web server
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload

worker: _check-no-celery ## Start both Celery workers (ingest + ai) and beat scheduler
	@$(PYTHON) -m celery -A app.tasks.celery_app worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4 & \
	$(PYTHON) -m celery -A app.tasks.celery_app beat --loglevel=INFO & \
	trap 'kill 0' EXIT INT TERM; \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3

worker-ingest: ## Start only the ingest (OCR) Celery worker
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4

worker-ai: ## Start only the AI Celery worker (LLM/embeddings/light I/O)
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3

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
