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
- **Code quality fixes** — Removed duplicate imports in `pages.py`; removed duplicate `resolved_by_month` computation in `case_stream`; removed dead code in `upload_form`; removed duplicate `htmx:afterSwap` listener in `base.html`; removed duplicate `trigger_summary_background` definition in `ai_summary.py`; added missing `templates` import in `actions.py`

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
- **Case Stream** — Five tabs (Review, Chronology, Calendar, Costs, Entities), Russian Doll chronology, Calendar (CRUD deadlines/hearings), Costs tab (4-metric strip + table), section scroll tracking with active highlighting
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

## Roadmap

Prioritization rule: prefer low-effort / low-complexity items with clear user impact first.

### Quick Wins

#### Improve Ingestion

> **Status key:** 🔴 Bug fix (can cause runtime errors) · 🟡 Improvement (robustness/UX) · 🟢 Partially fixed

1. ~~🔴 **Duplicate route definitions in `actions.py`**~~ — **FIXED**: Removed duplicate `update_case_deadline` (was lines 556-579). Fixed buggy `create_case_hearing`: `Deadline` → `Hearing`, `due_at` → `scheduled_for`, added `location` field, `deadline_errors` → `hearing_errors`.
2. ~~🔴 **Docling converter race condition**~~ — **FIXED**: `_get_converter()` now uses double-checked locking with `threading.Lock` (`ingestion.py:22-41`).
3. ~~🔴 **`missing_parent` never computed**~~ — **FIXED**: `compute_review_reasons()` now checks `if not doc.parent_id` and appends `"missing_parent"` (`ingestion.py:721-722`).
4. ~~🔴 **No `parent_id` existence validation**~~ — **FIXED**: `ingest_file()` queries parent exists before Document creation, returns `HTTPException(400)` if not found (`ingestion.py:850-854`).
5. ~~🟡 **H&M normalization too aggressive**~~ — **FIXED**: Added word boundary anchors `r"\b(?i)h\s*&\s*m\b|\bh\s+and\s+m\b"` (`normalization.py:9`).
6. ~~🟡 **No file size validation**~~ — **FIXED**: `MAX_FILE_SIZE = 50MB` constant, tracks bytes during chunked read, returns `413` if exceeded, cleans up partial file (`ingestion.py:16, 778-786`).
7. ~~🟡 **No deduplication**~~ — **FIXED**: `content_hash` column (`String(64)`, indexed) added to `Document` model. SHA-256 computed during save loop. Duplicate check: same hash + same `case_id` → `HTTPException(409)` with cleanup. Alembic migration `9f86d081884c` (idempotent).
8. ~~🟡 **Content snippet limits are arbitrary**~~ — **FIXED**: `extract_case_id()` scans full content (no limit), `extract_originator()` 8000 chars, `extract_sender()` 8000 chars, `extract_schedule_candidates()` 5000 chars. Named constants at module level.
9. ~~🟡 **Date extraction is greedy**~~ — **FIXED**: `extract_received_date()` scans header region (first 1000 chars) first, falls back to broader 3000-char scan. Dates in quoted correspondence no longer win over actual document dates.
10. ~~🟢 **`extract_clean_title()` bypasses H&M normalization**~~ — **FIXED**: All 4 return paths now apply `normalize_hm()` internally.
11. ~~🟢 **Triage uses `"_triage"` as case_id**~~ — **FIXED**: Real `_TRIAGE` case record seeded on startup. Unassigned docs get `case_id = "_TRIAGE"` instead of `None`. Sidebar/notification counts and triage page filter by `case_id == '_TRIAGE'`. Promotion endpoints reassign from `_TRIAGE` to target case.

#### Ingestion Pipeline Hardening

1. 🔴 **Docling conversion timeout** — `ingest_file()` calls `asyncio.to_thread(convert_to_md)` with no timeout. A corrupt or complex PDF could hang indefinitely. Fix: wrap in `asyncio.wait_for(..., timeout=120)`.
2. ~~🔴 **Fire-and-forget AI summary is fragile**~~ — **FIXED (Phase 1)**: Replaced `asyncio.create_task` with FastAPI `BackgroundTasks` via `trigger_summary_background()` (`ai_summary.py`, `actions.py`).
3. 🟡 **No case_id existence validation** — `ingest_file()` accepts any string as `final_case_id` (line 848). If extraction produces a bogus case ID, document gets orphaned. Fix: query `Case` table; if not found, default to `_TRIAGE`.
4. 🟡 **File path vs DB case_id inconsistency** — Directory uses `case_id or "_triage"` (lowercase, line 792) but DB uses `"_TRIAGE"` (uppercase, line 848). Fix: use `final_case_id` for directory path too.
5. ~~🟡 **AI summary model mismatch**~~ — **FIXED (Phase 1)**: Updated `MODEL` constant from `qwen2.5:7b` to `qwen3.5:9b` (`ai_summary.py:10`).
6. 🟡 **Case_id scan limit on large files** — `extract_case_id()` scans full content with no limit. A 50MB PDF → 10MB+ markdown × 6 regex patterns could be slow. Fix: add reasonable cap (e.g., 20000 chars).
7. 🟡 **Upload success response is generic** — Just "File ingested successfully" with no link to case stream. Fix: return case link and document ID.
8. 🟢 **No Docling output quality check** — If Docling returns mostly whitespace or repeated patterns, it's saved as valid content. Fix: add heuristic check (e.g., unique line ratio, minimum non-whitespace chars).

#### Case Stream Improvements

1. ~~**"Link to Parent" button is dead**~~ — Implemented: Alpine.js dropdown lists top-level docs, `POST /document/{doc_id}/link-parent` and `POST /document/{doc_id}/unlink-parent` endpoints with validation (same case, not self, no circular refs). Button toggles between `link` and `link_off` icons.
2. ~~🔴 **"Mark Reviewed" broken target"~~ — **FIXED**: `hx-target="closest div"` → `hx-target="closest .bg-surface-container"` — targets the card root, not the inner action-buttons div. Card also now has explicit `id="review-card-{id}"` for HTMX targeting from link/unlink endpoints.
3. ~~🔴 **Raw markdown in card previews**~~ — **FIXED**: Added `| striptags` filter to all 3 content slice locations: review card (line 110), chronology (line 200), child docs (line 250). Replaced dangerous `\| safe` with `\| striptags`.
4. ~~🟡 **No document count badge on Chronology**~~ — **FIXED**: Added `{{ documents|length }} DOCS` badge to Chronology header, matching Review/Calendar pattern.

#### Upload Modal Polish

1. ~~🟡 **Visual polish**~~ — **FIXED**: Added per-file-type icons (PDF→`picture_as_pdf`, DOCX→`description`, etc.) via Alpine.js `setFileIcon()`. Added HTMX loading spinner (`hx-indicator="#upload-spinner"`) inside Upload button. Upload button shows spinner during request.
2. ~~🟡 **Parent link logic**~~ — **FIXED**: Review card extracted to reusable `partials/review_card.html` partial. Parent picker now: (a) groups resolved docs by month with sticky headers, (b) filters to `needs_review=False` only, (c) returns full card HTML after link/unlink with "Child of: [parent title]" badge and updated review status. `resolve_triage`, `link-parent`, and `unlink-parent` all return full card re-renders via `hx-target="#review-card-{id}"`.

### Next Layer: Medium Effort / High Value

#### 7. Cost Extraction in Ingestion Pipeline
- `extract_cost_candidates()` in ingestion (regex + heuristics, later Ollama)
- Detect: RVG position references, GKG keywords, EUR amounts, Streitwert mentions
- Surface candidates in document detail pane; add `POST /document/{doc_id}/promote/cost`

#### 7a. Email Ingestion
- Parse `.eml` files, extract headers (From, To, Subject, Date) directly instead of heuristically
- Thread detection via In-Reply-To / References headers
- Attachment extraction from emails

#### 8. Keyboard Shortcuts and Command Affordances
- Shortcuts for search, theme toggle, case navigation, closing panes, section jumps

#### 9. Advanced Filtering & Saved Searches
- Multi-criteria filters and saved queries across main working views

#### 10. Error Handling & Resilience
- Retries, fallbacks, visible user-facing failure states beyond ingestion

#### 11. Responsive Workspace Strategy
- Breakpoints for sidebar collapse, pane stacking, right-pane behavior
- Tablet/mobile readability

#### 11a. Settings and Preferences Model
- Preference storage, dashboard customization, focus-mode persistence

### Heavier Bets

#### 12. Semantic Search with SQLite-Vec
- `content_embedding` column exists on `Document` — generate embeddings via Ollama (`nomic-embed-text`)
- `/search?q=...` with similarity ranking, highlights/context

#### 12a. Global Search Experience
- Search surface or command palette, keyboard-first invocation, quick-result previews

#### 13. PDF Preview in Contextual Workspace
- PDF viewer in document detail page, `/api/documents/{id}/pdf`, PDF.js with text-layer highlighting

#### 14. Global Entity Pivot
- Cross-document aggregation for people, deadlines, expenses
- Extraction pipeline, entity index table, `/entities` with filtering

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

#### 21c. Template Safety Filters
- ~~Add `| striptags` or markdown-to-text Jinja filter~~ — **FIXED (Phase 1)**: `safe_markdown` filter registered in `app/main.py`. Applied to `document_details.html:20` and `timeline.html:140`. Existing `striptags` on `case_stream.html:136, 185` and `review_card.html:15` confirmed.
- ~~Add HTMX loading indicators to upload form~~ — Already implemented (`hx-indicator="#upload-spinner"`)

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
