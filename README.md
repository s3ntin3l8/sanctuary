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
# Note: Update AI_BASE_URL to http://host.docker.internal:11434 if using local AI
docker-compose up -d
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/sanctuary.db` | Database connection string |
| `AI_BASE_URL` | `http://localhost:11434` | Base URL for local AI instance |
| `AI_SUMMARY_MODEL` | `qwen3.5:9b` | AI model for document summaries |
| `AI_EMBED_MODEL` | `nomic-embed-text` | AI model for semantic embeddings |

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

## Database Schema

The database uses SQLite with the following tables:

### Core Tables

| Table | Description |
|-------|-------------|
| `documents` | Uploaded legal documents with content, metadata, and extraction results |
| `cases` | Legal matters (e.g., ADV-992-K) with status tracking |
| `entities` | Extracted persons, organizations from documents, aggregated per case |
| `deadlines` | Court deadlines with due dates and completion status |
| `hearings` | Scheduled court dates with location and description |

### Document Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `title` | String | Document filename/title |
| `content` | Text | Converted PDF/email content (Markdown) |
| `content_embedding` | Text | Vector embedding for semantic search |
| `case_id` | String (FK) | Linked case (e.g., ADV-992-K, _TRIAGE for unlinked) |
| `file_path` | String | Raw file storage path |
| `content_hash` | String | SHA-256 for duplicate detection |
| `originator_type` | Enum | COURT / OPPOSING / OWN / UNKNOWN |
| `sender` | String | Extracted sender email/name |
| `received_date` | DateTime | When document was received |
| `created_at` | DateTime | Upload timestamp |
| `needs_review` | Boolean | Flag for triage |
| `review_reasons` | JSON | List of review triggers |
| `ingest_status` | Enum | PENDING / PROCESSING / COMPLETED / FAILED |
| `ingest_error` | Text | Error message if failed |
| `ingest_started_at` | DateTime | Processing start time |
| `ingest_completed_at` | DateTime | Processing complete time |
| `ai_summary` | JSON | {"legal_significance", "required_action", "financial_impact"} |
| `ai_summary_created_at` | DateTime | Summary generation time |
| `ai_summary_status` | String | pending / generated / failed / approved |
| `ai_summary_approved_at` | DateTime | Human approval timestamp |
| `cost_candidates` | JSON | Extracted RVG/GKG amount candidates |
| `extraction_confidence` | JSON | {"sender": "high", "date": "medium", ...} |
| `meta` | JSON | Page counts, headings, chunking info |
| `parent_id` | Integer (FK) | Parent document for nesting |

### Case Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Internal case ID (e.g., ADV-992-K) |
| `title` | String | Case title |
| `court_id` | String | Official docket number |
| `status` | Enum | INTAKE / ACTIVE / CLOSED |
| `jurisdiction` | Enum | DE (German) |
| `created_at` | DateTime | Creation timestamp |
| `closed_at` | DateTime | Closure timestamp |

### Entity Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `case_id` | String (FK) | Link to case |
| `type` | Enum | PERSON / ORGANIZATION / DATE / AMOUNT / OTHER |
| `name` | String | Entity name |
| `source_document_id` | Integer (FK) | Source document |
| `extra_data` | JSON | Confidence, positions, context |
| `created_at` | DateTime | Extraction timestamp |

### Event Tables (Deadlines & Hearings)

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `case_id` | String (FK) | Link to case |
| `title` | String | Event title |
| `description` | Text | Event details |
| `due_at` / `scheduled_for` | DateTime | Due date / hearing time |
| `completed` | Boolean | Deadline completion status |
| `location` | String | Hearing location |
| `source_document_id` | Integer (FK) | Source document |
| `created_at` | DateTime | Creation timestamp |

### Enums

| Enum | Values |
|------|--------|
| `OriginatorType` | COURT, OPPOSING, OWN, UNKNOWN |
| `IngestStatus` | PENDING, PROCESSING, COMPLETED, FAILED |
| `EntityType` | PERSON, ORGANIZATION, DATE, AMOUNT, OTHER |
| `CaseStatus` | INTAKE, ACTIVE, CLOSED |
| `CostStatus` | PENDING, APPROVED, PAID, DISPUTED |
| `Jurisdiction` | DE |

### LegalCost Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `case_id` | String (FK) | Link to case |
| `category` | Enum | GERICHTSKOSTEN, ANWALTSKOSTEN, ANWALTSKOSTEN_GEGNER, SACHVERSTAENDIGER, VORSCHUSS, VOLLSTRECKUNG, AUSLAGEN |
| `status` | Enum | OFFEN, genehmigt, bezahlt, streitig |
| `title` | String | Human-readable label (e.g., "Verfahrensgebühr 1. Instanz") |
| `rvg_position` | String | Statutory position (e.g., "Nr. 3100 VV RVG") |
| `amount_net` | Float | Net amount in EUR |
| `vat_rate` | Float | VAT rate (0.19 for lawyer, 0.0 for court) |
| `amount_gross` | Float | Gross amount (net + VAT) |
| `amount_paid` | Float | Amount already paid |
| `amount_reimbursed` | Float | Amount reimbursed by opposing party |
| `streitwert` | Float | Value in dispute (basis for calculations) |
| `gebuehren_faktor` | Float | RVG factor (e.g., 1.3) |
| `is_reimbursable` | Boolean | reimbursable under §91 ZPO |
| `issued_at` | DateTime | Invoice date |
| `due_at` | DateTime | Payment due date |
| `created_at` | DateTime | Creation timestamp |

### User Settings Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `user_id` | String | User identifier (default: "single_user") |
| `settings_json` | JSON | Theme, sidebar state, dashboard config |
| `updated_at` | DateTime | Last update timestamp |

## License

Copyright © 2025-2026 Sanctuary Legal. All rights reserved.
