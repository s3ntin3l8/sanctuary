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
| Task queue | Celery + Redis (background AI pipeline) |
| Frontend | HTMX + Alpine.js |
| Styling | Tailwind CSS v4 (semantic design tokens) |
| Database | SQLite + Alembic migrations + `sqlite-vec` |
| AI | Local Ollama / LM Studio / OpenAI-compatible — auto-detected |
| Ingestion | Docling (PDF → Markdown) & EML Parser |

## Quick Start

Python venv: python3.12 -m venv .venv
You can use the provided `Makefile` for common development tasks:

```bash
make setup      # Install dependencies and hooks
make run        # Start FastAPI server (http://127.0.0.1:8000)
make worker     # Start Celery worker (Terminal 2 — required for AI pipeline)
make watch-css  # Watch/build Tailwind CSS (Terminal 3)
make test       # Run all tests
make seed       # Reset and seed database
make migrate    # Run Alembic migrations
```

By default, `CELERY_TASK_ALWAYS_EAGER=false` — start `make worker` before uploading documents, or set `CELERY_TASK_ALWAYS_EAGER=true` in `.env` to run tasks synchronously in-process (handy for local dev without Redis).

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
| `AI_PROVIDER` | `ollama` | AI backend: `ollama`, `lmstudio`, `openai`, or `auto` |
| `AI_BASE_URL` | `http://127.0.0.1:11434` | Base URL for the AI provider |
| `AI_SUMMARY_MODEL` | `qwen3.5-9b-16k:latest` | Model for document analysis and summaries |
| `AI_EMBED_MODEL` | `nomic-embed-text:v1.5` | Model for semantic embeddings |
| `AI_EMBED_DIM` | `768` | Embedding dimension (must match model output) |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | Run tasks in-process synchronously (dev-only) |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker URL (not needed when eager=true) |

Alembic migrations run automatically on server startup. AI features require [Ollama](https://ollama.com/) or a compatible provider. See `.env.example` for all options.

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

The database uses SQLite with the following primary tables.

### Document Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `title` | String | Document filename/title |
| `content` | Text | Docling-converted content (Markdown) |
| `case_id` | String (FK) | Linked case (`_TRIAGE` for inbox) |
| `ingest_batch_id` | Integer (FK) | Parent `IngestBatch` |
| `parent_id` | Integer (FK) | Parent document (cover letter → enclosure nesting) |
| `proceeding_id` | Integer (FK) | Proceeding this document belongs to |
| `file_path` | String | Raw file storage path |
| `content_hash` | String | SHA-256 for dedup (not unique — same file can appear across cases) |
| `internal_id` | String | Human-readable doc ID (e.g., `ADV-024-A-0042`) |
| `originator_type` | Enum | `court / opposing / own / third_party / unknown` |
| `attributed_originator` | String | True author when routed via court (e.g., "Opposing counsel") |
| `court_relay` | Boolean | True if document is a pass-through cover letter |
| `role` | Enum | `cover_letter / enclosure / standalone` |
| `document_type` | Enum | `ruling / motion / statement / annex / relay / correspondence / report / invoice / other` |
| `significance_tier` | Enum | `critical / significant / informational / administrative` |
| `thread_open` | Boolean | True when waiting for a response from the other side |
| `sender` | String | Extracted sender email/name |
| `received_date` | DateTime | When document was received |
| `issued_date` | DateTime | Document date (from content) |
| `ingest_date` | DateTime | Import timestamp |
| `needs_review` | Boolean | Triage flag |
| `review_reasons` | JSON | List of review trigger codes |
| `pipeline_state` | Enum | `pending / running / completed / failed / partial` |
| `pipeline_stages` | JSON | Per-stage status dict (`extract`, `metadata`, `enrich`, …) |
| `ai_summary` | JSON | `{legal_significance, required_action, financial_impact}` |
| `ai_summary_status` | String | `pending / generated / failed / approved` |
| `ai_summary_created_at` | DateTime | Summary generation time |
| `ai_summary_approved_at` | DateTime | Human approval timestamp |
| `key_passages` | JSON | AI-highlighted passages `[{text, rationale, span, kind}]` |
| `cost_delta` | JSON | Financial impact `{amount, direction, description}` |
| `cost_candidates` | JSON | Regex-extracted cost candidates (pre-AI) |
| `extraction_confidence` | JSON | `{sender: high, date: medium, …}` |
| `meta` | JSON | Page counts, headings, chunking info |

### Case Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | Internal lead ID (e.g., `ADV-024-A`) — shown everywhere |
| `title` | String | Case title |
| `status` | Enum | `intake / discovery / pre_trial / trial / post_trial / closed` |
| `jurisdiction` | Enum | `de / uk / us / other` |
| `ingest_date` | DateTime | Creation timestamp |
| `closed_at` | DateTime | Closure timestamp |
| `is_draft` | Boolean | Draft case not yet active |
| `ai_brief` | JSON | Living AI brief `{status_line, key_risks, open_threads, recent_development}` |
| `ai_brief_updated_at` | DateTime | When brief was last regenerated |
| `parties` | JSON | Known actors and their roles |
| `total_cost_exposure` | Integer | Total cost in cents (sum of `LegalCost` rows) |

Per-court Aktenzeichen live on `Proceeding.az_court` — one case has one proceeding per court level.

### Proceeding Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `case_id` | String (FK) | Owning case |
| `court_name` | String | e.g., "Amtsgericht Hamburg" |
| `court_level` | Enum | `ag / lg / olg / bgh / other` |
| `az_court` | String | Court docket number e.g. `003 F 426/25` |
| `subject_matter` | String | e.g., "§ 1671 BGB, Sorgerecht" |
| `status` | Enum | `active / closed` |
| `started_at` / `ended_at` | DateTime | Proceeding timeline |

### IngestBatch Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `source_type` | Enum | `email / scan / manual` |
| `received_at` | DateTime | Email date or file mtime |
| `sender_email` | String | Email `From:` header |
| `subject` | String | Email subject or scan filename |
| `message_id` | String | RFC5322 Message-Id for dedup |
| `raw_source_path` | String | Path to archived original |
| `case_id` | String (FK) | Confirmed case assignment |
| `proceeding_id` | Integer (FK) | Confirmed proceeding |
| `status` | Enum | `pending / processing / completed / failed / awaiting_slicing` |
| `source_hash` | String | Source-level dedup hash |

### DocumentRelationship Fields

| Field | Type | Description |
|-------|------|-------------|
| `from_document_id` | Integer (FK) | Source document |
| `to_document_id` | Integer (FK) | Target document |
| `relationship_type` | Enum | `replies_to / references / attaches_as_proof / supersedes / cited_by` |
| `confidence` | Enum | `ai_detected / user_confirmed / user_created` |
| `notes` | Text | Optional annotation |

### ActionItem Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `case_id` | String (FK) | Owning case |
| `proceeding_id` | Integer (FK) | Owning proceeding |
| `source_document_id` | Integer (FK) | Document that contained this deadline |
| `title` | String | e.g., "Stellungnahme zu Klageerwiderung" |
| `action_type` | Enum | `deadline / court_date / response_required / filing_required` |
| `due_date` | DateTime | Deadline date |
| `status` | Enum | `open / completed / dismissed` |
| `location` | String | Court room (for court_date type) |

### Claim / ClaimEvidence Fields

| Table | Key Fields |
|-------|-----------|
| `Claim` | `id`, `case_id`, `proceeding_id`, `source_document_id`, `claim_type` (`factual/legal/procedural`), `status` (`asserted/contested/refuted/established`), `title`, `description` |
| `ClaimEvidence` | `id`, `claim_id`, `document_id`, `role` (`supports/contests/refutes/cites_as_proof`), `excerpt`, `confidence` |

### UserReaction Fields

| Field | Type | Description |
|-------|------|-------------|
| `document_id` | Integer (FK) | Reacted-to document |
| `reaction_type` | Enum | `lies / true / needs_proof / precedent` |
| `note` | Text | Optional free-text annotation |

### Entity Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `case_id` | String (FK) | Link to case |
| `type` | Enum | `person / organization / date / financial / legal_category / court / law_firm / citation` |
| `name` | String | Entity name |
| `source_document_id` | Integer (FK) | Source document |
| `extra_data` | JSON | Confidence, positions, context |

### LegalCost Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `case_id` | String (FK) | Link to case |
| `proceeding_id` | Integer (FK) | Link to proceeding |
| `category` | Enum | `gerichtskosten / anwaltskosten / anwaltskosten_gegner / sachverstaendiger / vorschuss / vollstreckung / auslagen / sonstiges` |
| `status` | Enum | `offen / bezahlt / erstattet / teilweise / strittig` |
| `title` | String | e.g., "Verfahrensgebühr 1. Instanz" |
| `rvg_position` | String | Statutory position (e.g., "Nr. 3100 VV RVG") |
| `amount_net` | Float | Net amount in EUR |
| `vat_rate` | Float | VAT rate (0.19 for lawyer, 0.0 for court) |
| `amount_gross` | Float | Gross amount (net + VAT) |
| `amount_paid` | Float | Amount paid |
| `amount_reimbursed` | Float | Amount reimbursed under §91 ZPO |
| `streitwert` | Float | Value in dispute |
| `gebuehren_faktor` | Float | RVG factor (e.g., 1.3) |
| `is_reimbursable` | Boolean | Reimbursable under §91 ZPO |
| `issued_at` | DateTime | Invoice date |
| `due_at` | DateTime | Payment due date |

### Conversation / Message Fields

| Table | Key Fields |
|-------|-----------|
| `Conversation` | `id`, `scope_type` (`case/document/global`), `scope_id`, `title`, `created_at` |
| `ConversationMessage` | `id`, `conversation_id`, `role` (`user/assistant`), `content`, `citations` (JSON), `created_at` |

### User Settings

| Field | Description |
|-------|-------------|
| `user_id` | User identifier (default: `single_user`) |
| `settings_json` | Theme, active proceeding per case, dashboard default view, AI model overrides |

### Enums

| Enum | Values |
|------|--------|
| `CaseStatus` | `intake, discovery, pre_trial, trial, post_trial, closed` |
| `OriginatorType` | `court, opposing, own, third_party, unknown` |
| `DocumentRole` | `cover_letter, enclosure, standalone` |
| `DocumentType` | `ruling, motion, statement, annex, relay, correspondence, report, invoice, other` |
| `SignificanceTier` | `critical, significant, informational, administrative` |
| `PipelineState` | `pending, running, completed, failed, partial` |
| `PipelineStage` | `extract, metadata, proceeding_analysis, batch_analysis, enrich, relationships, claims, entities, embeddings` |
| `IngestBatchSourceType` | `email, scan, manual` |
| `IngestBatchStatus` | `pending, processing, completed, failed, awaiting_slicing` |
| `RelationshipType` | `replies_to, references, attaches_as_proof, supersedes, cited_by` |
| `ActionItemType` | `deadline, court_date, response_required, filing_required` |
| `EntityType` | `person, organization, date, financial, legal_category, court, law_firm, citation` |
| `CostCategory` | `gerichtskosten, anwaltskosten, anwaltskosten_gegner, sachverstaendiger, vorschuss, vollstreckung, auslagen, sonstiges` |
| `CostStatus` | `offen, bezahlt, erstattet, teilweise, strittig` |
| `ClaimStatus` | `asserted, contested, refuted, established` |
| `UserReactionType` | `lies, true, needs_proof, precedent` |

### Vector Search

Embeddings live in a `sqlite-vec` virtual table separate from the main schema:

```sql
CREATE VIRTUAL TABLE document_vectors USING vec0(
    document_id INTEGER PRIMARY KEY,
    embedding float[768]   -- dimension set by AI_EMBED_DIM (default 768)
);
```

KNN queries use `WHERE embedding MATCH :blob ORDER BY distance LIMIT :k`.

### User Settings Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | Integer | Primary key |
| `user_id` | String | User identifier (default: "single_user") |
| `settings_json` | JSON | Theme, sidebar state, dashboard config, last-visited timestamps |
| `updated_at` | DateTime | Last update timestamp |

## License

Copyright © 2025-2026 Björn Hansen

Released under the [GNU Affero General Public License v3.0](LICENSE).
