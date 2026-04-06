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
python3.12 -m venv venv && venv/bin/pip install -r requirements.txt
# Terminal 1
venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
# Terminal 2
npx @tailwindcss/cli -i static/input.css -o static/styles.css --watch
# Seed
rm -f data/sanctuary.db && venv/bin/python seed_dummy_data.py
```
`get_db()` in `app/dependencies.py`. Migrations: `alembic revision --autogenerate -m "..." && alembic upgrade head`