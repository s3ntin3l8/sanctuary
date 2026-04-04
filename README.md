# The Sanctuary — Lead Counsel Edition

A privacy-first legal case management workspace for active litigation. All AI runs locally. No data leaves the machine.

## Features

- **Russian Doll Document Protocol** — Nested document hierarchy with originator colour stripes (Court / Opposing Counsel / Own Lawyer), provenance footers, and L-connector indentation
- **Case Directory** — Active and closed cases with status badges, grouped by litigation lifecycle
- **Case Stream** — Tabbed view (Review / Chronology / Entities) with split-pane document detail panel
- **Triage Inbox** — Processing centre for unlinked documents, with originator filtering and inline metadata editor
- **Master Timeline** — Cross-case chronological feed
- **Animated Sidebar** — Collapsible focus mode with choreographed fade transitions and zero FOUC
- **Dual Light/Dark Mode** — Semantic token system; toggle via `.dark` class on `<html>`. Light: Quiet Authority palette. Dark: official Stitch Case Organizer palette
- **Docling Ingestion** — PDF → Markdown conversion with heuristic metadata extraction

## Planned

- AI management summaries via local Ollama (Qwen 3.5 9B)
- Semantic search via SQLite-Vec + `nomic-embed-text` embeddings
- PDF preview in right pane (PDF.js)
- Global entity pivot (people, deadlines, expenses)

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+ / FastAPI |
| Frontend | HTMX + Alpine.js |
| Styling | Tailwind CSS v4 (semantic design tokens) |
| Database | SQLite + `sqlite-vec` |
| AI | Local Ollama — Qwen 3.5 9B |
| PDF Ingestion | Docling |

## Running

```bash
# Backend
source venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Tailwind (watch mode)
npx tailwindcss -i static/input.css -o static/styles.css --watch
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

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

## License

Copyright © 2025 Sanctuary Legal. All rights reserved.
