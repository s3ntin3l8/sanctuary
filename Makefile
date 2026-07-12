# Sanctuary Development Makefile

PYTHON_EXE ?= $(shell command -v python3.12 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null)
PYTHON := .venv/bin/python
UVICORN := .venv/bin/uvicorn
PYTEST := $(PYTHON) -m pytest
PRECOMMIT := .venv/bin/pre-commit
ALEMBIC := .venv/bin/alembic

.PHONY: help setup run run-stable run-debug server worker worker-ingest worker-ai watch-css test test-unit test-integration test-e2e test-e2e-isolated seed migrate lint clean redis _check-no-celery

test: ## Run all tests (excludes E2E)
	rm -rf .pytest_cache __pycache__ app/__pycache__ app/*/__pycache__ app/*/*/__pycache__ 2>/dev/null || true
	$(PYTEST) --ignore=tests/e2e -p no:cacheprovider

test-e2e: ## Run E2E tests (requires running server on localhost:8000)
	$(PYTEST) -m e2e

test-e2e-isolated: ## Run E2E tests against a fully throwaway server + DB (mirrors CI; never touches data/sanctuary.db)
	@rm -f /dev/shm/sanctuary_test_e2e.db
	@echo "Migrating throwaway DB..."
	@DATABASE_URL="sqlite:////dev/shm/sanctuary_test_e2e.db" $(ALEMBIC) upgrade head
	@echo "Starting throwaway app server..."
	@DATABASE_URL="sqlite:////dev/shm/sanctuary_test_e2e.db" \
	 CELERY_TASK_ALWAYS_EAGER=true \
	 AUTH_ENABLED=false \
	 SESSION_SECRET="e2e-isolated-ephemeral-not-a-real-secret" \
	 $(UVICORN) app.main:app --host 127.0.0.1 --port 8000 > /tmp/sanctuary_test_e2e_server.log 2>&1 & \
	 echo $$! > /tmp/sanctuary_test_e2e_server.pid; \
	 trap 'kill "$$(cat /tmp/sanctuary_test_e2e_server.pid)" 2>/dev/null; rm -f /tmp/sanctuary_test_e2e_server.pid' EXIT INT TERM; \
	 for i in $$(seq 1 30); do curl -sf http://127.0.0.1:8000 >/dev/null && break; sleep 1; done; \
	 DATABASE_URL="sqlite:////dev/shm/sanctuary_test_e2e.db" $(PYTEST) -m e2e; \
	 status=$$?; \
	 echo "--- throwaway server log (tail) ---"; tail -50 /tmp/sanctuary_test_e2e_server.log || true; \
	 exit $$status

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

_check-no-celery: ## Internal: refuse to start if celery beat/workers already running
	@if pgrep -f "celery -A app.tasks.celery_app (beat|worker)" >/dev/null 2>&1; then \
		echo "ERROR: celery beat or worker already running:"; \
		pgrep -fa "celery -A app.tasks.celery_app (beat|worker)" | sed 's/^/  /'; \
		echo "Kill them first, then re-run. Otherwise you get duplicate beats (every scheduled task fires N× → recover-pipeline storms)."; \
		exit 1; \
	fi

run: _check-no-celery ## Start Redis, web server, ingest worker (OCR), AI worker, and beat scheduler
	docker compose up redis -d --wait
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload & \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4 & \
	$(PYTHON) -m celery -A app.tasks.celery_app beat --loglevel=INFO & \
	trap 'kill 0' EXIT INT TERM; \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3

run-stable: _check-no-celery ## Start without --reload (use for ingestion/pipeline testing — avoids recovery loops)
	docker compose up redis -d --wait
	@$(UVICORN) app.main:app --host $(HOST) --port $(PORT) & \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ingest@%h --loglevel=INFO -Q ingest --concurrency=4 & \
	$(PYTHON) -m celery -A app.tasks.celery_app beat --loglevel=INFO & \
	trap 'kill 0' EXIT INT TERM; \
	$(PYTHON) -m celery -A app.tasks.celery_app worker -n ai@%h --loglevel=INFO -Q ai --concurrency=3

run-debug: _check-no-celery ## Start server with DEBUG logging (+ Redis + both workers)
	docker compose up redis -d --wait
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

seed: ## Reset database and seed with advanced triage combinations (backs up real DB first)
	@if [ -f data/sanctuary.db ]; then \
		BACKUP="data/sanctuary.db.bak.$$(date +%Y%m%d_%H%M%S)"; \
		cp data/sanctuary.db "$$BACKUP"; \
		echo "✓ Backup: $$BACKUP"; \
	fi
	rm -f data/sanctuary.db
	$(PYTHON) scripts/seed_dummy_data.py

migrate: ## Run database migrations (auto-backs up data/sanctuary.db first)
	@if [ -f data/sanctuary.db ]; then \
		BACKUP="data/sanctuary.db.bak.$$(date +%Y%m%d_%H%M%S)"; \
		cp data/sanctuary.db "$$BACKUP"; \
		echo "✓ Backup: $$BACKUP"; \
	fi
	$(ALEMBIC) upgrade head

lint: ## Run pre-commit hooks on all files
	$(PRECOMMIT) run --all-files

clean: ## Clean up temporary files (keeps only the 5 most recent DB backups)
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -f test_sanctuary.db
	@ls -t data/sanctuary.db.bak.* 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true
