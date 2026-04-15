# The Sanctuary — Lead Counsel Edition

A privacy-first legal case management workspace for active litigation. All AI runs locally. No data leaves the machine.

## Features

- **Hybrid Search** — Global search with autocomplete (Cmd+K). Combines **Semantic Similarity** (via local `nomic-embed-text` embeddings) with high-speed text pattern matching.
- **Global Entity Pivot** — Cross-case aggregation and ranking of extracted persons, organizations, and legal concepts.
- **Russian Doll Document Protocol** — Nested document hierarchy with originator colour stripes (Court / Opposing Counsel / Own Lawyer), provenance footers, and L-connector indentation.
- **Case Stream** — Tabbed view (Review / Calendar / Costs / Entities) with split-pane document detail workspace.
- **Triage Inbox** — Processing centre for unlinked documents, featuring extraction confidence-based review triggers and inline metadata editing.
- **Master Timeline** — Global chronological feed across all litigation matters.
- **Legal Cost Tracking** — Specialized German Kostenrecht support (RVG/GKG), 4-metric summaries, and automated overdue alerts.
- **Relationship Intelligence Hub** — Contact management automatically aggregated from document senders.
- **Privacy First** — 100% offline operation. All frontend assets (Alpine.js, HTMX, Fonts) and AI models are hosted locally.
- **Dual Theme** — Semantic design tokens supporting high-contrast light and dark modes.
- **Intelligent Ingestion** — Docling-powered PDF conversion and `.eml` email parsing with heuristic metadata extraction.
- **AI Management Summaries** — 3-bullet summaries (Legal Significance, Action, Finance) generated locally via Ollama.

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+ / FastAPI |
| Frontend | HTMX + Alpine.js |
| Styling | Tailwind CSS v4 (semantic design tokens) |
| Database | SQLite + Alembic migrations + `sqlite-vec` |
| AI | Local Ollama — Qwen 3.5 9B & Nomic Embed Text |
| Ingestion | Docling & EML Parser |

## Quick Start

You can use the provided `Makefile` for common development tasks:

```bash
make setup      # Install dependencies and hooks
make run        # Start FastAPI server (http://127.0.0.1:8000)
make watch-css  # Watch/build Tailwind CSS
make test       # Run all tests
make seed       # Reset and seed database
```

## Docker Deployment

The fastest way to deploy the full stack:

```bash
cp .env.example .env
# Note: Update OLLAMA_BASE_URL to http://host.docker.internal:11434 if using local Ollama
docker-compose up -d
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/sanctuary.db` | Database connection string |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Base URL for local Ollama instance |
| `OLLAMA_SUMMARY_MODEL` | `qwen3.5:9b` | Ollama model for document summaries |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Ollama model for semantic embeddings |

Alembic migrations run automatically on server startup. AI features require [Ollama](https://ollama.com/) running locally.

## Seed Data

Populate the database with ~100 realistic documents, parent-child links, costs, and extracted entities:

```bash
make seed
```

## Design System

Tokens are defined in `static/input.css`. Light mode is the default. Dark mode activates when the `.dark` class is present on `<html>`.

| Token | Light | Dark |
|---|---|---|
| `surface` | `#f8fafb` | `#0b1326` |
| `surface-container` | `#e8eff1` | `#171f33` |
| `primary` | `#45636b` | `#57f1db` |
| `on-surface` | `#2a3437` | `#dae2fd` |

## License

Copyright © 2025-2026 Sanctuary Legal. All rights reserved.
