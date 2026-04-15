# Sanctuary Development Makefile

PYTHON := .venv/bin/python
UVICORN := .venv/bin/uvicorn
PYTEST := $(PYTHON) -m pytest
PRECOMMIT := .venv/bin/pre-commit
ALEMBIC := .venv/bin/alembic

.PHONY: help setup run watch-css test test-unit test-integration seed migrate lint clean

-include .env
export

# Defaults if not set in .env
HOST ?= 127.0.0.1
PORT ?= 8000

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0s %s\n", $$1, $$2}'

setup: ## Install dependencies and pre-commit hooks
	$(PYTHON) -m pip install -r requirements.txt
	$(PRECOMMIT) install
	$(PRECOMMIT) install --hook-type pre-push

run: ## Start the FastAPI development server
	$(UVICORN) app.main:app --host $(HOST) --port $(PORT) --reload

watch-css: ## Watch and build Tailwind CSS v4
	npx @tailwindcss/cli -i static/input.css -o static/styles.css --watch

test: ## Run all tests
	$(PYTEST)

test-unit: ## Run unit tests
	$(PYTEST) -m unit

test-integration: ## Run integration tests
	$(PYTEST) -m integration

seed: ## Reset database and seed with dummy data
	rm -f data/sanctuary.db
	$(PYTHON) scripts/seed_dummy_data.py

migrate: ## Run database migrations
	$(ALEMBIC) upgrade head

lint: ## Run pre-commit hooks on all files
	$(PRECOMMIT) run --all-files

clean: ## Clean up temporary files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -f test_sanctuary.db
