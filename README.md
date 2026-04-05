# The Sanctuary — Lead Counsel Edition

A privacy-first legal case management workspace for active litigation. All AI runs locally. No data leaves the machine.

## Features

- **Russian Doll Document Protocol** — Nested document hierarchy with originator colour stripes (Court / Opposing Counsel / Own Lawyer), provenance footers, and L-connector indentation
- **Case Directory** — Active and closed cases with status badges, grouped by litigation lifecycle
- **Case Stream** — Tabbed view (Review / Calendar / Costs) with split-pane document detail panel
- **Triage Inbox** — Processing centre for unlinked documents, with originator filtering and inline metadata editor
- **Master Timeline** — Cross-case chronological feed
- **Legal Cost Tracking** — German Kostenrecht (RVG/GKG), 4-metric summaries, overdue alerts
- **Contact Hub** — Relationship intelligence aggregated from document senders
- **Animated Sidebar** — Collapsible focus mode with choreographed fade transitions and zero FOUC
- **Dual Light/Dark Mode** — Semantic token system; toggle via `.dark` class on `<html>`
- **Docling Ingestion** — PDF → Markdown conversion with heuristic metadata extraction
- **AI Summaries** — 3-bullet management summaries generated locally via Ollama

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+ / FastAPI |
| Frontend | HTMX + Alpine.js |
| Styling | Tailwind CSS v4 (semantic design tokens) |
| Database | SQLite + Alembic migrations + `sqlite-vec` |
| AI | Local Ollama — Qwen 3.5 9B |
| PDF Ingestion | Docling |

## Running

```bash
# 1. Create virtual environment (first time only)
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Start backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 3. Tailwind (watch mode, separate terminal)
npx tailwindcss -i static/input.css -o static/styles.css --watch
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/sanctuary.db` | Database connection string |

Alembic migrations run automatically on server startup.

## Seed Data

Populate the database with ~100 realistic documents across 4 test cases:

```bash
rm -f data/sanctuary.db
venv/bin/python seed_dummy_data.py
```

## Design System

Tokens are defined in `static/input.css`. Light mode is the default. Dark mode activates when the `.dark` class is present on `<html>`.

| Token | Light | Dark |
|---|---|---|
| `surface` | `#f8fafb` | `#0b1326` |
| `surface-container` | `#e8eff1` | `#171f33` |
| `primary` | `#45636b` | `#57f1db` |
| `on-surface` | `#2a3437` | `#dae2fd` |
| `outline-variant` | `#a9b4b7` | `#3c4a46` |

Reference designs: `templates/quiet_authority/` (light) · `templates/stitch_case_organizer_dark/` (dark)

## Rate Limiting

All POST mutation endpoints are rate-limited to **20 requests per minute** via `slowapi`. The triage inbox supports pagination (`?limit=50&offset=0`).

## License

Copyright © 2025 Sanctuary Legal. All rights reserved.
