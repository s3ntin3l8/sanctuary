# Agent Task Brief — The Sanctuary (Lead Counsel Edition)

> Drop-in context for any agent picking up this project. Read this before touching anything.

---

## Project Identity

**The Sanctuary** is a privacy-first legal case management platform for a single user managing active litigation. All AI runs locally via Ollama. No data leaves the machine. The aesthetic is "Quiet Sanctuary" — high information density, dark slate palette, minimal chrome.

**Stack:**
- Backend: Python 3.9+ / FastAPI
- Frontend: HTMX (server comms) + Alpine.js (local UI state)
- Styling: Tailwind CSS v4 with dual light/dark token system (`static/input.css`)
- Database: SQLite + Alembic migrations + `sqlite-vec` extension (column ready, integration pending)
- AI: Local Ollama — **qwen2.5:7b** for summaries/extraction
- PDF Ingestion: **Docling** → Markdown

---

## Layout Architecture

Three-pane split view — never break this structure:
- **18% left:** Sidebar navigation (collapsible to icon strip via Focus Mode)
- **47% center:** Document stream / case timeline
- **35% right:** Contextual workspace (AI summaries, metadata, PDF preview)

Three states: `DEFAULT` (all visible), `FOCUS` (sidebar collapsed to icons), `STREAM_ONLY` (right pane hidden).

---

## Routing

| Route | Purpose |
|---|---|
| `/` or `/dashboard` | Global cross-case overview |
| `/triage` | Unlinked documents inbox (split-pane: card list + detail) |
| `/cases` | Case directory (Active / Closed grouping) |
| `/cases/{id}` | Case stream (Russian Doll chronology, Calendar, Costs tabs) |
| `/timeline` | Master timeline across all cases |
| `/costs` | Legal cost overview with alerts, manual entry, per-case tables |
| `/contacts` | Relationship Intelligence Hub (aggregated from Document.sender) |

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
Every document in the right pane has a 3-bullet AI summary (Legal Significance, Required Action, Financial Impact). Generated post-ingestion (fire-and-forget) with status tracking (`pending`/`generated`/`failed`/`stale`). Re-trigger via `POST /document/{doc_id}/summarize`.

### Vertical Identity Header
Must remain `sticky top-0`. Hierarchy: Case Title (XL Bold) → Court ID (Mono) → Internal ID (Mono). Status badge anchored far right.

---

## What Has Been Built (as of Apr 5, 2026)

### Structure & Infrastructure
- Modular FastAPI: `routers/pages.py` (GET), `routers/actions.py` (POST), `helpers.py`, `constants.py`, `config.py`, `dependencies.py`
- Alembic migrations (idempotent initial schema), `pool_pre_ping=True` on SQLite engine
- All enums single-sourced in `app.models.database`; dead `schemas.py` and `Expense` model removed

### Database Models
- **Case** — status tracking (`INTAKE`→`CLOSED`), seeded on startup
- **Document** — `parent_id`, `case_id`, originator metadata, `content_embedding` (sqlite-vec ready), AI summary columns (`ai_summary`, `ai_summary_status`)
- **Deadline / Hearing** — with `source_document_id` linkage
- **LegalCost** — full German Kostenrecht (`CostCategory` × `CostStatus`, `streitwert`, `gebuehren_faktor`, `is_reimbursable`)

### Pages & Features
- **Dashboard** — data-driven metrics, upcoming deadlines/hearings, overdue costs card, recent documents
- **Triage** — split-pane (38% card list + 62% detail), originator filter, promote-to-deadline/hearing, inline metadata editor
- **Case Stream** — Russian Doll chronology, Calendar (CRUD deadlines/hearings), Costs tab (4-metric strip + table), section scroll tracking with active highlighting
- **Costs** — 4-metric summary, per-case tables, manual entry form (Alpine.js auto-calc gross from net+VAT), overdue/"due soon" alerts
- **Contacts** — aggregated from `Document.sender`, searchable/filterable list, HTMX detail panel with stats + document timeline
- **Notifications** — dropdown panel at body level (`fixed z-[9999]`), shows overdue deadlines, upcoming events, pending review, overdue costs

### Ingestion & AI
- Docling pipeline with lazy converter init, file type validation, comprehensive error handling
- Enhanced metadata extraction: weighted originator keywords, German court file numbers, German date formats, signature block detection
- Expanded deadline extraction: "within X days", "by [date]", "deadline:" patterns with relative date calculation
- Ollama-powered 3-bullet summaries via `qwen2.5:7b`, fire-and-forget post-ingestion trigger

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

1. 🔴 **Duplicate route definitions in `actions.py`** — `update_case_deadline` defined twice (lines 496, 556 — identical). `create_case_hearing` defined twice (lines 522, 582) — the **first version is buggy**: creates `Deadline` instead of `Hearing` and references undefined `due_at` (line 548 → `NameError`). Remove the duplicate block (lines 556-579) and fix the first `create_case_hearing` to use `Hearing` + `scheduled_for`.
2. 🔴 **Docling converter race condition** — `_get_converter()` at `ingestion.py:20-36` has TOCTOU race: two concurrent uploads both see `_converter is None` and each create a `DocumentConverter`. Called inside `asyncio.to_thread()` at line 789. Fix: wrap init in `threading.Lock`.
3. 🔴 **`missing_parent` never computed** — `constants.py:126` defines `missing_parent` in `REVIEW_FIELD_LABELS` and `unlink-parent` adds it (actions.py:142), but `compute_review_reasons()` at `ingestion.py:702-728` checks 7 reasons and never flags `missing_parent` when `parent_id` is None. Add the check.
4. 🔴 **No `parent_id` existence validation** — `ingest_file()` accepts `parent_id` at line 740, passes it directly to `Document` at line 811 with no DB lookup. Bogus ID causes FK constraint 500. Note: `link_parent` endpoint *does* validate (actions.py:82-87). Fix: query parent exists before Document creation, return 400.
5. 🟡 **H&M normalization too aggressive** — regex `r"(?i)h\s*&\s*m|h\s+and\s+m"` at `normalization.py:9` matches "height & mass", "hazard & maintenance" etc. Fix: add word boundary anchors `r"\b(?i)h\s*&\s*m\b|\bh\s+and\s+m\b"`.
6. 🟡 **No file size validation** — Files read in 1MB chunks at `ingestion.py:769-777` with no upper bound. Could exhaust memory. Add max file size check (e.g., 50MB) before saving.
7. 🟡 **No deduplication** — Same PDF uploaded twice creates two `Document` records. No SHA-256 hash check, no filename+case_id uniqueness constraint. Add content hash on `Document` model + check before insert.
8. 🟡 **Content snippet limits are arbitrary** — `extract_case_id()` scans 2000 chars, `extract_originator()` 3000, `extract_sender()` 3000, `extract_schedule_candidates()` 5000. Metadata beyond these offsets silently missed. Consider smarter windowing (header section for sender/date, full text for case_id).
9. 🟡 **Date extraction is greedy** — `extract_received_date()` at `ingestion.py:405-411` returns first match per tier. A date in quoted prior correspondence wins over actual document date. Consider scoring candidates by proximity to "received"/"dated"/"from" keywords.
10. 🟢 **`extract_clean_title()` bypasses H&M normalization** — Pipeline order ensures normalization runs first (line 790) before title extraction (line 800), so content passed in *is* normalized. But the function itself doesn't call `normalize_hm()` — fragile to call-order changes. Fix: apply `normalize_hm()` inside the function for self-containment.
11. 🟢 **Triage uses `"_triage"` as case_id** — `case_id` column is nullable, promotion endpoints reject docs without `case_id` (actions.py:384, 427). The `"_triage"` string is only a filesystem directory, not a DB value. Lower severity than originally described. Consider: create a real "triage" case record so `case_id` can be non-nullable.

#### Case Stream Improvements

1. ~~**"Link to Parent" button is dead**~~ — Implemented: Alpine.js dropdown lists top-level docs, `POST /document/{doc_id}/link-parent` and `POST /document/{doc_id}/unlink-parent` endpoints with validation (same case, not self, no circular refs). Button toggles between `link` and `link_off` icons.
2. 🔴 **"Mark Reviewed" broken target** — `hx-target="closest div"` at `case_stream.html:117` resolves to the inner action-buttons `<div>`, not the card container. After `outerHTML` swap, the card retains a broken button area. Fix: change to `hx-target="closest .bg-surface-container"` or give the card an explicit ID.
3. 🔴 **Raw markdown in card previews** — 3 locations slice `doc.content` with zero markdown cleaning: review card `[:150]` (line 110), chronology `[:150]` (line 200), child docs `[:120]` with `\| safe` (line 250 — actively harmful, renders Docling HTML unescaped). Fix: add `| striptags` or a markdown-to-text filter.
4. 🟡 **No document count badge on Chronology** — Review shows "X PENDING" (`case_stream.html:91`), Calendar shows "X UPCOMING" (line 286), Chronology header at line 176 has nothing. Add `{{ documents|length }} DOCS` badge.

#### Upload Modal Polish

1. 🟡 **Visual polish** — Has backdrop blur + transitions (`upload_form.html:1-4`), file type list (line 30). Missing: per-file-type icons, HTMX progress indicator during upload (`hx-indicator`), visual success/error states beyond server-rendered response text.
2. 🟡 **Parent link logic** — Review card picker (`case_stream.html:126-148`) and upload form (`upload_form.html:34-44`) both use flat dropdowns of all top-level docs. Consider: (a) grouping by date, (b) filtering to `needs_review=False` candidates, (c) search/filter for large cases, (d) visual relationship feedback after linking (e.g., "Child of: [parent title]"). Note: review card picker uses `hx-swap="outerHTML"` which replaces the form element — picker disappears but action buttons lose form structure.

### Next Layer: Medium Effort / High Value

#### 7. Cost Extraction in Ingestion Pipeline
- `extract_cost_candidates()` in ingestion (regex + heuristics, later Ollama)
- Detect: RVG position references, GKG keywords, EUR amounts, Streitwert mentions
- Surface candidates in document detail pane; add `POST /document/{doc_id}/promote/cost`

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
- Add `| striptags` or markdown-to-text Jinja filter for Docling content previews (prevents unescaped HTML rendering)
- Make `extract_clean_title()` self-contained by applying `normalize_hm()` internally
- Add HTMX loading indicators to upload form (`hx-indicator` or CSS spinner)

---

## Key Files

```
alembic.ini                      — Alembic configuration
alembic/
  env.py                         — Alembic environment: imports models, runs migrations
  versions/
    698c5f71bf23_initial_full_schema.py  — Idempotent migration: creates all tables
app/
  __init__.py                    — Package marker
  main.py                        — FastAPI app creation, lifespan (DB init + seed), router registration
  config.py                      — DB URL, engine, SessionLocal, Jinja2Templates
  dependencies.py                — get_db() FastAPI dependency
  constants.py                   — Meta dicts: ORIGINATOR_COLORS/ICONS, CASE/COST status meta, REVIEW_FIELD_LABELS
  helpers.py                     — Shared utilities: render_page, build_sidebar_counts, build_notifications, formatters, cost summary
  routers/
    __init__.py                  — Package marker
    pages.py                     — All GET page routes
    actions.py                   — All POST mutation routes
  models/
    __init__.py                  — Re-exports all models and enums
    database.py                  — SQLAlchemy models: Case, Document, Deadline, Hearing, LegalCost, enums
  services/
    __init__.py                  — Package marker
    ai_summary.py                — Ollama-powered 3-bullet summaries
    ingestion.py                 — Docling ingestion pipeline (hardened, enhanced extraction)
    normalization.py             — H&M normalization utility
  templates/
    base.html                    — Root layout; .dark class toggle, notifications panel, localStorage
    partials/
      sidebar.html               — Animated collapsible sidebar
      page_header.html           — Shared sticky page header
      secondary_header.html      — Sub-header row for case stream
      header_controls.html       — Search / Notifications / Theme toggle
      empty_state.html           — Shared empty-state renderer
      triage_card.html           — Triage card with metadata editor + promote buttons
      document_details.html      — Right-pane document view with dynamic AI summary
      document_extraction_panel.html — Schedule candidates + linked deadlines/hearings
      case_schedule_panel.html   — Deadlines + hearings CRUD panel
      cost_row.html              — Reusable HTMX cost row
      cost_form.html             — Inline cost creation form with Alpine.js auto-calc
      contact_detail.html        — Contact detail panel: stats, timeline, case links
    pages/
      dashboard.html             — Data-driven: cases, deadlines, hearings, documents, overdue costs
      triage.html                — Split-pane triage inbox
      case_directory.html        — Active / Closed case grid
      case_stream.html           — Russian Doll timeline + Calendar + Costs tabs + split doc pane
      timeline.html              — Cross-case chronology
      costs.html                 — Legal costs: metrics, alerts, tables, add cost form
      contacts.html              — Relationship Intelligence Hub
static/
  input.css                      — Tailwind source: light @theme + .dark overrides
  styles.css                     — Compiled output (regenerate with npx tailwindcss ...)
templates/
  quiet_authority/DESIGN.md      — Light mode design spec
  stitch_case_organizer_dark/    — Dark mode design spec + Tailwind color config reference
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

### Dummy Data Seed Script

`seed_dummy_data.py` populates the database with ~100 realistic documents across 4 cases for development and UI testing.

```bash
# Reset DB and seed fresh data
rm -f data/sanctuary.db
venv/bin/python seed_dummy_data.py
```

**Generates:** 4 cases (ADV-992-K, ADV-804-M, ADV-331-P, ADV-550-R), ~98 documents (10 content templates), ~23 parent-child relationships, ~17 deadlines, ~13 hearings, ~20 costs. H&M normalization applied, ~15% marked `needs_review`, `random.seed(42)` for reproducibility.
