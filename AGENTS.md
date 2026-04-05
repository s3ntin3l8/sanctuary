# AGENTS.md — The Sanctuary

## Quick Start

```bash
# Start dev server (no hot reload — restart after code changes)
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# Rebuild Tailwind CSS after template class changes
npm run build:css          # or: npx @tailwindcss/cli -i static/input.css -o static/styles.css --minify
```

Database lives at `data/sanctuary.db`. Alembic migrations auto-run on server start.

## Architecture

- **Entry point:** `app/main.py` — FastAPI app, mounts `/static`, includes `pages` and `actions` routers
- **Routers:** `app/routers/pages.py` (GET page renders), `app/routers/actions.py` (POST mutations)
- **Templates:** `app/templates/` — Jinja2, `{% extends "base.html" %}` for pages, no-extend for partials
- **HTMX target elements** must have unique IDs. Modal roots (`#upload-modal-root`) go in `base.html` at body level so `position: fixed` works (nested `overflow-hidden` clips fixed elements)
- **Alpine.js** doesn't auto-scan HTMX-swapped content. A global `htmx:afterSwap` listener in `base.html` calls `Alpine.scan()` — don't add per-element `hx-on` handlers

## Key Conventions

- **`render_page()` helper** (`app/helpers.py`) — use for full pages that need sidebar counts/notifications. Use `templates.TemplateResponse()` directly for partials (avoids unnecessary DB queries)
- **`secondary_status` Jinja set-block** — the case stream's status badge area. Upload/Cost buttons go here
- **`case_schedule_panel.html`** partial — shared between dashboard and case stream calendar sections
- **`cost_form.html`** partial — accepts `preselected_case_id`/`preselected_case_title` to hide the case dropdown
- **`upload_form.html`** partial — modal with drag-and-drop, `case_id` hidden field, optional `parent_id` dropdown
- **Design tokens** defined in `static/input.css` — Tailwind v4, no config file needed

## HTMX Gotchas

- Version: **1.9.10** — uses `hx-on:event` (single colon), NOT `hx-on::event` (double colon added in 1.9.12)
- `hx-target="#id"` uses `document.getElementById()` — works across the full document
- `hx-swap="outerHTML"` replaces the target element entirely; `hx-swap="innerHTML"` replaces its children
- `hx-encoding="multipart/form-data"` required for file uploads
- The `htmx:responseError` handler in `base.html` shows a toast on any non-2xx response

## DB / Migrations

```bash
# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations manually (they auto-run on server start)
alembic upgrade head
```

- SQLite, single file at `data/sanctuary.db`
- `get_db()` in `app/dependencies.py` — FastAPI `Depends()` pattern
- No `pyproject.toml` — dependencies in `requirements.txt`

## UI Consistency Rules (from CLAUDE.md)

- Section headers: `text-[10px] font-bold uppercase tracking-widest`
- Metric values: `text-3xl font-black font-mono`
- Card padding: `p-5` for metric cards, `p-6` for section panels
- Originator stripe colors: Blue (Court), Red (Opposing), Green (Own Lawyer)
- "H&M" entity must always be ALL CAPS

## References

- `CLAUDE.md` — design system, Russian Doll protocol, H&M rule, navigation structure
- `agent_task.md` — roadmap, completed items, open issues
