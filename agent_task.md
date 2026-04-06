# Agent Task Brief — The Sanctuary (Lead Counsel Edition)

> Drop-in context for any agent picking up this project. Read this before touching anything.

---

## Project Identity

**The Sanctuary** is a privacy-first legal case management platform for a single user managing active litigation. All AI runs locally via Ollama. No data leaves the machine. The aesthetic is "Quiet Sanctuary" — high information density, dark slate palette, minimal chrome.

**Stack:**
- Backend: Python 3.12+ / FastAPI
- Frontend: HTMX (server comms) + Alpine.js (local UI state)
- Styling: Tailwind CSS v4 with dual light/dark token system (`static/input.css`)
- Database: SQLite + Alembic migrations + `sqlite-vec` extension
- AI: Local Ollama — **qwen3.5:9b** for summaries/extraction, **nomic-embed-text** for search
- PDF Ingestion: **Docling** → Markdown

---

## Layout Architecture

Three-pane split view — never break this structure:
- **Left:** Sidebar navigation (collapsible to icon strip via Focus Mode)
- **Center:** Document stream / case timeline (47% when doc selected, otherwise flex-1)
- **Right:** Contextual workspace (AI summaries, metadata, PDF preview, 53% when doc selected)

Three states: `DEFAULT` (all visible), `FOCUS` (sidebar collapsed to icons), `STREAM_ONLY` (right pane hidden).

---

## Routing

| Route | Purpose |
|---|---|
| `/` | Dashboard — global cross-case overview |
| `/triage` | Unlinked documents inbox (split-pane: card list + detail). Supports `?limit=50&offset=0` pagination |
| `/cases` | Case directory (Active / Closed grouping) |
| `/cases/{id}` | Case stream (Russian Doll chronology, Calendar, Costs tabs) |
| `/timeline` | Master timeline across all cases |
| `/costs` | Legal cost overview with alerts, manual entry, per-case tables |
| `/entities` | Global Entity Pivot — cross-case aggregation of people, orgs, and legal concepts |
| `/contacts` | Relationship Intelligence Hub (aggregated from Document.sender) |
| `/search` | Full-page semantic and text search results |
| `/document/{doc_id}` | Document detail page |
| `/upload` | Upload form (partial) |

---

## Critical Business Rules (Non-Negotiable)

### The H&M Rule
Every instance of "H&M" (retail clothing expenses) must be rendered in **ALL CAPS**. Enforced via `app/services/normalization.normalize_hm()` during ingestion and as Jinja2 `|hm` filter in templates.

### The Russian Doll Protocol
- Documents check for `parent_id`; children indented 24px with SVG L-connector
- `border-l-4` originator stripe: `#0369A1` (Court), `#B91C1C` (Opposing), `#047857` (Own)
- Footer: *"Via: Email from [Sender] on [Date]"*

### Triage Logic
Any document without `case_id` or `parent_id` defaults to Triage Inbox.

---

## What Has Been Built (as of Apr 6, 2026)

### Core Ingestion & Extraction
- **Docling Pipeline** — PDF to Markdown with heuristic metadata extraction (court file numbers, dates, sign-offs).
- **Email Support** — `.eml` ingestion with header parsing and thread detection.
- **Deduplication** — SHA-256 content hashing to prevent duplicate uploads.
- **Management Summaries** — 3-bullet AI summaries generated via local Ollama (`qwen3.5:9b`).
- **Entity Extraction** — Automated extraction of Persons, Organizations, and Legal Categories during ingestion.

### Search & Discovery
- **Hybrid Search** — Global search supporting both `sqlite-vec` semantic similarity (via `nomic-embed-text`) and standard text pattern matching.
- **Global Entity Pivot** — A master view aggregating and ranking all extracted entities across all cases.
- **Relationship Hub** — Contact management automatically aggregated from document senders.

### UI / UX Foundations
- **UI Standardization** — Uniform "Secondary Status Bar" across all pages; standardized padding, alignment, and "Russian Doll" nesting.
- **Dual Theme** — Full light/dark mode support using Tailwind v4 semantic tokens.
- **Animated Workspace** — Collapsible sidebar, split-pane document views, and smooth Alpine.js transitions.
- **Offline Infrastructure** — 100% local hosting of Alpine.js, HTMX, and Google Fonts (Inter, Manrope).
- **Shortcuts** — `Cmd+K` (Search), `Cmd+D` (Theme), `Cmd+/` (Focus Search), `Esc` (Close).

### Data & Infrastructure
- **Modern Backend** — Python 3.12+ / FastAPI / SQLAlchemy / Alembic.
- **Resilience** — Standardized JSON error responses for HTMX toasts; `slowapi` rate limiting.
- **Configurable Logic** — User-definable review triggers based on AI extraction confidence.

---

## Roadmap (Open Items Only)

### Phase 2: Advanced Workspace & Performance
1. **PDF Preview** — Integrated PDF viewer in document details using PDF.js with text-layer highlighting.
2. **Focus Mode Enhancement** — Refined mobile-safe layout and persistent local focus states.
3. **Jurisdiction-Agnostic Costs** — Refactor `LegalCost` to support non-German jurisdictions (UK/US/Other).
4. **Performance Tuning** — Lazy loading/infinite scroll for the Timeline and Triage feed.

### Phase 3: Data Resilience & Integration
5. **Backup & Export** — Local backup/restore flows and case export bundles (ZIP/JSON).
6. **Audit Trail** — Event logging for edits, status changes, and AI generations.
7. **Document Comparison** — Side-by-side diff view for document versions or related filings.
8. **Batch Operations** — Multi-file upload with progress tracking and bulk re-ingestion.

### Phase 4: System Hardening
9. **Test Suite** — Comprehensive unit/integration tests for the ingestion pipeline and UI targets.
10. **Runtime Hardening** — Improved database pooling and health checks for high-concurrency usage.

---

## Key Files

```
app/
  routers/
    pages.py                     — GET routes (Dashboard, Triage, Stream, Search, Entities)
    actions.py                   — POST routes (Upload, Link, Summarize, Costs)
  models/
    database.py                  — SQLAlchemy models (Case, Document, Entity, LegalCost)
  services/
    ingestion.py                 — Docling pipeline & metadata extraction
    ai_summary.py                — Ollama summary generation
    embeddings.py                — Ollama semantic embeddings
    normalization.py             — H&M rule enforcement
  templates/
    base.html                    — Root layout, JS global listeners, toast system
    pages/                       — Full page views
    partials/                    — Reusable components (Review cards, detail panels)
static/
  input.css                      — Tailwind source (tokens & dual-mode logic)
  vendor/                        — Self-hosted assets (Alpine, HTMX)
  fonts/                         — Self-hosted Google Fonts
seed_dummy_data.py               — Dev seed script (~100 docs, entities included)
```
