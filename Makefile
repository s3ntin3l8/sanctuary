# Sanctuary Development Makefile

PYTHON_EXE := /home/bjoern/.pyenv/versions/3.12.13/bin/python
PYTHON := .venv/bin/python
UVICORN := .venv/bin/uvicorn
PYTEST := $(PYTHON) -m pytest
PRECOMMIT := .venv/bin/pre-commit
ALEMBIC := .venv/bin/alembic

.PHONY: help setup run watch-css test test-unit test-integration test-e2e seed migrate lint clean

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
	$(PYTHON_EXE) -m venv .venv
	$(PYTHON) -m pip install --upgrade pip

setup: .venv ## Install dependencies and pre-commit hooks
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m playwright install chromium
	npm install
	$(PRECOMMIT) install
	$(PRECOMMIT) install --hook-type pre-push

run: ## Start the FastAPI development server
	$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload

run-debug: ## Start server with DEBUG logging enabled
	LOG_LEVEL=debug DEBUG=True $(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload --log-level debug

watch-css: ## Watch and build Tailwind CSS v4
	npx @tailwindcss/cli -i static/input.css -o static/styles.css --watch

test-unit: ## Run unit tests
	$(PYTEST) --ignore=tests/e2e -m unit

test-integration: ## Run integration tests
	$(PYTEST) --ignore=tests/e2e -m integration

seed: ## Reset database and seed with advanced triage combinations
	rm -f data/sanctuary.db
	$(PYTHON) scripts/seed_dummy_data.py

reset: ## Delete all data (database, files, vectors) and start fresh
	@echo "Resetting data..."
	rm -f data/sanctuary.db*
	rm -rf data/_TRIAGE
	rm -rf data/ai_debug
	rm -rf data/scans/*
	# Delete all case directories (ADV-XXX-X)
	find data -maxdepth 1 -type d -name "ADV-*" -exec rm -rf {} +
	# Ensure scan directories exist
	mkdir -p data/scans/incoming data/scans/processing data/scans/processed data/scans/failed
	@echo "Data cleared. Running migrations..."
	$(MAKE) migrate
	@echo "Reset complete."

migrate: ## Run database migrations
	$(ALEMBIC) upgrade head

lint: ## Run pre-commit hooks on all files
	$(PRECOMMIT) run --all-files

clean: ## Clean up temporary files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -f test_sanctuary.db
