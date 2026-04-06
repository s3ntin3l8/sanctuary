# Agent Task Brief — The Sanctuary (Lead Counsel Edition)

> Drop-in context for any agent picking up this project. Read this before touching anything.

---

## Project Identity

**The Sanctuary** is a privacy-first legal case management platform for a single user managing active litigation. All AI runs locally via Ollama. No data leaves the machine. The aesthetic is "Quiet Sanctuary" — high information density, dark slate palette, minimal chrome.

**Stack:**
- Backend: Python 3.12+ / FastAPI
- Frontend: HTMX (server comms) + Alpine.js (local UI state)
- Styling: Tailwind CSS v4 with dual light/dark token system (`static/input.css`)
- Database: SQLite + Alembic migrations + `sqlite-vec` extension (column ready, integration pending)
- AI: Local Ollama — **qwen3.5:9b** for summaries/extraction
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
| `/costs/new` | Cost creation form (global) |
| `/cases/{id}/costs/new` | Cost creation form (pre-selected for case) |
| `/contacts` | Relationship Intelligence Hub (aggregated from Document.sender) |
| `/contacts/{sender_name}` | Contact detail panel |
| `/document/{doc_id}` | Document detail page |
| `/document/{doc_id}/extractions` | Extraction panel for a document |
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

### Management Summary
Every document in the right pane has a 3-bullet AI summary (Legal Significance, Required Action, Financial Impact). Generated post-ingestion via FastAPI `BackgroundTasks` with status tracking (`pending`/`generated`/`failed`/`stale`). Re-trigger via `POST /document/{doc_id}/summarize`.

### Vertical Identity Header
Must remain `sticky top-0`. Hierarchy: Case Title (XL Bold) → Court ID (Mono) → Internal ID (Mono). Status badge anchored far right.

---

## What Has Been Built (as of Apr 6, 2026)

### Basic Search (Apr 6, 2026)
- **Search API endpoint** — `/api/search?q=...` returns JSON with documents, cases, contacts
- **Search results page** — `/search?q=...` full-page results with documents, cases, contacts sections
- **Header search component** — Alpine.js dropdown: input appears on button click, immediately focused and ready to type, autocomplete results in popup
- **Keyboard shortcut** — Cmd+K focuses search input (base.html global listener)
- **Settings model** — UserSettings and SavedSearch tables for preference persistence, localStorage auto-persist for theme and sidebar state

### Python Upgrade (Apr 5, 2026)
- **Upgraded from Python 3.9 to 3.12** — Project now uses Python 3.12.13 (Homebrew)
- **Fixed datetime deprecation** — Replaced all `datetime.utcnow()` with `datetime.now()` for Python 3.12+ compatibility
- **Fixed regex compatibility** — Fixed inline `(?i)` flag in `normalization.py` to use compiled regex pattern for Python 3.12+
- **Added httpx dependency** — Required for AI summary service, was missing from requirements.txt
- **Fixed timezone-aware/naive datetime mismatch** — SQLite stores naive datetimes, all code now uses naive `datetime.now()` consistently

### Phase 1 — Production-Ready Foundations (Apr 6, 2026)
- **Environment variable for database URL** — `DATABASE_URL` env-var in `app/config.py` with fallback to `data/sanctuary.db`
- **Rate limiting middleware** — `slowapi` added; all POST routes limited to 20 req/min
- **Pagination on `/triage`** — `?limit=50&offset=0` query params added
- **Template safety** — `safe_markdown` Jinja filter registered (`app/main.py`); applied to `document_details.html`; `striptags` added to `timeline.html`
- **AI summary refactor** — Fire-and-forget `asyncio.create_task` replaced with FastAPI `BackgroundTasks`; model updated `qwen2.5:7b` → `qwen3.5:9b`
- **Code quality fixes** — Removed duplicate imports in `pages.py`; removed duplicate `resolved_by_month` computation in `case_stream`; removed dead code in `upload_form`; removed duplicate `htmx:afterSwap` listener in `base.html`; removed duplicate `trigger_summary_background` definition in `ai_summary.py`; added missing `templates` import in `actions.py`

### Post-Phase 1 Fixes
- **Hide _TRIAGE from case directory** — Filtered from `/cases` route to exclude virtual inbox from case list
- **Deadlines/hearings side-by-side** — Grid changed to `lg:grid-cols-2` for screens ≥1024px

### Structure & Infrastructure
- Modular FastAPI: `routers/pages.py` (GET), `routers/actions.py` (POST), `helpers.py`, `constants.py`, `config.py`, `dependencies.py`
- Alembic migrations (idempotent initial schema), `pool_pre_ping=True` on SQLite engine
- All enums single-sourced in `app.models.database`; dead `schemas.py` and `Expense` model removed

### Database Models
- **Case** — status tracking (`INTAKE`→`CLOSED`), seeded on startup
- **Document** — `parent_id`, `case_id`, originator metadata, `content_hash` (SHA-256 dedup), `content_embedding` (sqlite-vec ready), AI summary columns (`ai_summary`, `ai_summary_status`)
- **Deadline / Hearing** — with `source_document_id` linkage
- **LegalCost** — full German Kostenrecht (`CostCategory` × `CostStatus`, `streitwert`, `gebuehren_faktor`, `is_reimbursable`)

### Pages & Features
- **Dashboard** — data-driven metrics, upcoming deadlines/hearings, overdue costs card, recent documents
- **Triage** — split-pane (38% card list + 62% detail), originator filter, promote-to-deadline/hearing, inline metadata editor
- **Case Stream** — Five tabs (Review, Chronology, Calendar, Costs, Entities stub), Russian Doll chronology, Calendar (CRUD deadlines/hearings), Costs tab (4-metric strip + table), section scroll tracking with active highlighting
- **Review cards** — extracted to reusable `partials/review_card.html` partial with explicit IDs for HTMX targeting. Full card re-render on mark reviewed, link parent, unlink parent. Parent picker groups resolved docs by month, filters to `needs_review=False` only, shows "Child of: [parent title]" badge.
- **Chronology** — document count badge in header matching Review/Calendar pattern. Content previews use `| striptags` for safe rendering.
- **Upload modal** — per-file-type icons, HTMX loading spinner, backdrop blur + transitions
- **Costs** — 4-metric summary, per-case tables, manual entry form (Alpine.js auto-calc gross from net+VAT), overdue/"due soon" alerts
- **Contacts** — aggregated from `Document.sender`, searchable/filterable list, HTMX detail panel with stats + document timeline
- **Notifications** — dropdown panel at body level (`fixed z-[9999]`), shows overdue deadlines, upcoming events, pending review, overdue costs

### Ingestion & AI
- Docling pipeline with **thread-safe** lazy converter init (`threading.Lock`), file type validation, **file size limit (50MB)**, comprehensive error handling
- **Content deduplication** — SHA-256 hash of uploaded bytes, checked against existing docs per case; `409 Conflict` on duplicate with cleanup
- Enhanced metadata extraction: weighted originator keywords, German court file numbers, German date formats, signature block detection
- **Smarter extraction windows** — case_id scans full content, originator/sender scan 8000 chars, date extraction prefers header region (first 1000 chars) over deeper content
- **Self-contained title extraction** — `extract_clean_title()` applies `normalize_hm()` internally
- Expanded deadline extraction: "within X days", "by [date]", "deadline:" patterns with relative date calculation
- Ollama-powered 3-bullet summaries via `qwen3.5:9b`, `BackgroundTasks` post-ingestion trigger
- **`missing_parent` review reason** — auto-computed during ingestion when `parent_id` is None
- **Real triage case** — `_TRIAGE` case record seeded on startup, unassigned docs get `case_id = "_TRIAGE"`, promotion reassigns to target case

### Styling
- Dual light/dark mode via CSS cascade from `input.css` (no hardcoded JS theme objects)
- Unified typography (`text-[10px]` section headers, `font-mono` metric values), consistent padding/shadows across all pages
- Shared `empty_state.html` component used everywhere

---

## Optimizations

All complete except: **self-host frontend assets** — `base.html` still loads Google Fonts, Alpine.js, and HTMX from CDNs.

---

## Roadmap (Open Items Only)

Prioritization rule: prefer low-effort / low-complexity items with clear user impact first.

### Ingestion Pipeline

1. ~~🟢 **No Docling output quality check**~~ — **FIXED**: Added `is_valid_docling_output()` function that rejects empty/whitespace/repetitive content (`ingestion.py:736-746`).
2. ~~🟢 **Cost Extraction**~~ — **FIXED**: Added `extract_cost_candidates()` with RVG/GKG/EUR/Streitwert detection, stored in `cost_candidates` JSON column (`ingestion.py:749-793`, `database.py`).
3. ~~🟢 **Email Ingestion**~~ — **FIXED**: Added `.eml` support with header parsing and thread detection via `parse_eml_file()` (`ingestion.py:796-838`).

### Next Layer: Medium Effort / High Value

#### 8. Keyboard Shortcuts and Command Affordances
- **FIXED**: Cmd+K focus search, Cmd+D toggle theme, Cmd+/ focus search, Esc close modals (base.html)

#### 9. Advanced Filtering & Saved Searches
- **FIXED**: URL filter params (?originator, ?needs_review, ?search) on triage; SavedSearch model with localStorage auto-persist

#### 10. Error Handling & Resilience
- **FIXED**: Improved htmx:responseError handler with specific messages for 413, 409, 400, 500, network errors

#### 11. Responsive Workspace Strategy
- Breakpoints for sidebar collapse, pane stacking, right-pane behavior
- Tablet/mobile readability

#### 11a. Settings and Preferences Model
- **FIXED**: UserSettings and SavedSearch tables, localStorage auto-persist for theme and sidebar state

### Heavier Bets

#### 12. Semantic Search with SQLite-Vec
- `content_embedding` column exists on `Document` — generate embeddings via Ollama (`nomic-embed-text`)
- `/search?q=...` with similarity ranking, highlights/context

#### 12a. Global Search Experience
- **FIXED**: Basic search with `/api/search` JSON endpoint, `/search` results page, header autocomplete dropdown

#### 13. PDF Preview in Contextual Workspace
- PDF viewer in document detail page, `/api/documents/{id}/pdf`, PDF.js with text-layer highlighting

#### 14. Global Entity Pivot
- Cross-document aggregation for people, deadlines, expenses
- Extraction pipeline, entity index table, `/entities` with filtering
- **IMPLEMENTED (per-case)**: Entity table in database, extraction on ingestion (persons, financial, legal categories), Entities tab in case stream shows grouped entities
- **INVESTIGATE**: Global `/entities` page across all cases — aggregation, filtering, search

#### 14a. Jurisdiction-Agnostic Cost System
- `jurisdiction` field on `Case` (`DE`, `UK`, `US`, `OTHER`)
- Refactor `CostCategory` to jurisdiction-neutral labels

#### 15. Focus Mode
- True focus states for sidebar collapse and context-pane reduction
- Persist chosen mode locally, mobile-safe layout

#### 16. Customizable Dashboard
- Preference storage in SQLite, show/hide controls for cards and panels

#### 16a. AI Review and Approval States
- Track `generated`, `reviewed`, `stale`, `failed`
- Human approval visible before AI output treated as accepted work

### Foundations

#### 17. Document Comparison & Version History
- Diff view, revision history, change tracking

#### 18. API Documentation & Public Endpoints
- Formal endpoint docs, auth model, external integration guidance

#### 18a. Backup, Restore, and Export
- Local backup/restore flows, case export bundles

#### 18b. Audit Trail and Activity Log
- Event logging for edits, status changes, generated outputs, promotions
- Readable activity history by case and document

#### 19. Performance Optimizations
- Pagination, lazy loading, infinite scroll, query tuning

#### 19a. Search Indexing and Background Jobs
- Background processing for embeddings, AI summaries, extraction

#### 19b. Batch Upload
- Upload multiple files at once, each processed independently with individual success/error reporting
- Progress indicator for batch operations

#### 19c. Re-ingestion Pipeline
- Ability to re-run extraction on existing documents (e.g., after Docling update or extraction improvements)
- Bulk re-ingest by case or by date range

#### 19d. Extraction Confidence Scores
- Store confidence per extracted field (sender: 0.95, date: 0.7) instead of binary extracted/not-extracted
- Use confidence to prioritize review queue

#### 19e. Content Embedding Pipeline
- `content_embedding` column exists on `Document` but is never populated
- Generate embeddings via Ollama (`nomic-embed-text`) on ingestion
- Enable semantic search across documents

#### 19f. Upload Success UX
- Return case stream link after successful upload
- Show document preview or metadata summary
- Option to immediately promote to deadline/hearing from upload result

#### 20. Test Suite
- Unit, integration, and UI/E2E coverage
- Priority: ingestion pipeline (extraction functions, normalization, review reasons), route dedup validation, HTMX target correctness

#### 21. Database Connection / Runtime Hardening
- Better pooling/health handling as concurrency grows

#### 21a. Seeding and Demo-Data Strategy
- Separate demo data, dev seeding, and production-safe initialization paths

#### 21b. Delete, Archive, and Undo Flows
- Safe delete/archive behavior, lightweight undo/recovery patterns

---

## Key Files

```
alembic.ini                      — Alembic configuration
alembic/
  env.py                         — Alembic environment: imports models, runs migrations
  versions/
    698c5f71bf23_initial_full_schema.py  — Idempotent migration: creates all tables
    9f86d081884c_add_content_hash_to_documents.py  — Adds SHA-256 dedup column (idempotent)
app/
  __init__.py                    — Package marker
  main.py                        — FastAPI app creation, lifespan (DB init + seed), router registration, rate limiter, Jinja filters
  config.py                      — DB URL (env-var), engine, SessionLocal, Jinja2Templates
  dependencies.py                — get_db() FastAPI dependency
  constants.py                   — Meta dicts: ORIGINATOR_COLORS/ICONS, CASE/COST status meta, REVIEW_FIELD_LABELS
  helpers.py                     — Shared utilities: render_page, build_sidebar_counts, build_notifications, formatters, cost summary
  routers/
    __init__.py                  — Package marker
    pages.py                     — All GET page routes (triage has pagination)
    actions.py                   — All POST mutation routes (rate-limited 20/min)
  models/
    __init__.py                  — Re-exports all models and enums
    database.py                  — SQLAlchemy models: Case, Document, Deadline, Hearing, LegalCost, enums
  services/
    __init__.py                  — Package marker
    ai_summary.py                — Ollama-powered 3-bullet summaries (qwen3.5:9b, BackgroundTasks)
    ingestion.py                 — Docling ingestion pipeline (hardened, enhanced extraction)
    normalization.py             — H&M normalization utility
  templates/
    base.html                    — Root layout; .dark class toggle, notifications panel, localStorage, toast system
    partials/
      sidebar.html               — Animated collapsible sidebar
      page_header.html           — Shared sticky page header
      secondary_header.html      — Sub-header row for case stream
      header_controls.html       — Search / Notifications / Theme toggle
      empty_state.html           — Shared empty-state renderer
      review_card.html           — Reusable review card with HTMX targeting, parent picker, status badges
      triage_card.html           — Triage card with metadata editor + promote buttons
      document_details.html      — Right-pane document view with dynamic AI summary (safe_markdown filter)
      document_extraction_panel.html — Schedule candidates + linked deadlines/hearings
      case_schedule_panel.html   — Deadlines + hearings CRUD panel
      cost_row.html              — Reusable HTMX cost row
      cost_form.html             — Inline cost creation form with Alpine.js auto-calc
      contact_detail.html        — Contact detail panel: stats, timeline, case links
    pages/
      dashboard.html             — Data-driven: cases, deadlines, hearings, documents, overdue costs
      triage.html                — Split-pane triage inbox (paginated)
      case_directory.html        — Active / Closed case grid
      case_stream.html           — Russian Doll timeline + Calendar + Costs tabs + split doc pane
      timeline.html              — Cross-case chronology (striptags on content)
      costs.html                 — Legal costs: metrics, alerts, tables, add cost form
      contacts.html              — Relationship Intelligence Hub
      search.html                — Full-page search results (documents, cases, contacts)
static/
  input.css                      — Tailwind source: light @theme + .dark overrides
  styles.css                     — Compiled output (regenerate with npx tailwindcss ...)
seed_dummy_data.py               — Dev seed script: ~100 docs across 4 cases
agent_task.md                    — This file
```

---

## Development Notes

- Run server: `uvicorn app.main:app --reload`
- Tailwind watch: `npx tailwindcss -i static/input.css -o static/styles.css --watch`
- Alembic: `alembic upgrade head` runs automatically on startup; to create a new migration: `alembic revision --autogenerate -m "description"`
- SQLite DB at `data/sanctuary.db`; seed data runs on every startup (idempotent per-case)
- Ollama must be running locally at `http://localhost:11434` for AI features
- Index a file after editing: `index_file { "path": "/abs/path" }` (jCodemunch MCP)

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/sanctuary.db` | Database connection string |

### Dummy Data Seed Script

`seed_dummy_data.py` populates the database with ~100 realistic documents across 4 cases for development and UI testing.

```bash
# Reset DB and seed fresh data
rm -f data/sanctuary.db
venv/bin/python seed_dummy_data.py
```

**Generates:** 4 cases (ADV-992-K, ADV-804-M, ADV-331-P, ADV-550-R), ~98 documents (10 content templates), ~23 parent-child relationships, ~17 deadlines, ~13 hearings, ~20 costs. H&M normalization applied, ~15% marked `needs_review`, `random.seed(42)` for reproducibility.
