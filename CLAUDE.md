# CLAUDE.md: The Sanctuary

Privacy-first legal case management. All AI runs locally via Ollama. "Quiet Sanctuary" aesthetic — high density, dark slate, minimal chrome.

## Stack
* **Backend:** Python 3.12+ / FastAPI
* **Frontend:** HTMX + Alpine.js
* **Styling:** Tailwind CSS v4 (`static/input.css` dual light/dark tokens)
* **DB:** SQLite + Alembic + `sqlite-vec`
* **AI:** Ollama (`qwen3.5:9b` summaries, `nomic-embed-text` embeddings)
* **Ingestion:** Docling (PDF → Markdown)

## Rules
* **Management Summary:** 3-bullet (Legal Significance, Action/Deadline, Financial Impact).
* **Triage:** No `case_id`/`parent_id` → Triage Inbox.

## Run
```bash
make setup      # Install/Update
make run        # Terminal 1: App
make watch-css  # Terminal 2: CSS
make seed       # Seed Data
make test       # Run Tests
```
`get_db()` in `app/dependencies.py`. Migrations: `alembic revision --autogenerate -m "..." && alembic upgrade head`
