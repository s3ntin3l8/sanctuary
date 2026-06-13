# CLAUDE.md: The Sanctuary

Privacy-first legal case management. All AI runs locally via Ollama. "Quiet Sanctuary" aesthetic — high density, dark slate, minimal chrome, zero unnecessary chrome.

## What this is

A **case intelligence engine**, not a document archive. Documents are evidence. Cases are the primary object. The user navigates by graph and claim — never by file list.

## Core mental models

* **Email is the atom** — one email = one `IngestBatch`; documents from the same email are a family
* **Court is infrastructure** — cover letters are relays; show the true sender, collapse the wrapper
* **Three layers:** Structural (who said what to whom) → Factual (what's contested) → Strategic (cost exposure, action items, case clock)
* **Triage is a strategy session** — user reactions (🚩 Lies / ✅ True / 🔍 Needs Proof / ⚖️ Precedent) are first-class data the AI recalls later
* **Documents surface as HUDs** — AI-highlighted key passages, not raw PDFs; the one sentence that matters is already marked
* **Significance tiers** — `critical / significant / informational / administrative`; 900 letters collapse to ~150 visible nodes by default
* **No magic numbers** — cost deltas are factual; timelines are ranges with rationale; no synthetic probabilities

## Stack
* **Backend:** Python 3.12+ / FastAPI + Celery (background tasks)
* **Frontend:** HTMX + Alpine.js
* **Styling:** Tailwind CSS v4 (`static/input.css` dual light/dark tokens)
* **DB:** SQLite + Alembic + `sqlite-vec`
* **AI:** Auto-detect ollama / lmstudio / openai
* **Ingestion:** Docling (PDF → Markdown)
* **Rate limiting:** `slowapi`

## Key data model concepts

* `IngestBatch` — email/scan group; case assignment cascades to all children
* `Proceeding` — court level within a case (AG → OLG → BGH); graphs are scoped per proceeding
* `DocumentRelationship` — typed N:N edges (`replies_to`, `references`, `attaches_as_proof`, `supersedes`, `cited_by`, `encloses`)
* `Claim` + `ClaimEvidence` — atomic factual assertions and their evidence chain (the Truth Map)
* `UserReaction` — triage reactions (🚩/✅/🔍/⚖️) stored and recalled by AI during case brief and document enrichment
* `ActionItem` — deadlines and court dates extracted from documents, first-class records
* `Document.significance_tier` — AI-assigned; drives graph visibility
* `Document.court_relay` + `Document.attributed_originator` — true sender behind court routing
* `DocumentPin` — passage-anchored margin annotations (distinct from `UserReaction`; stores span offsets)
* `LegalCost` — German RVG/GKG/JVEG cost tracking per proceeding; `CostCategory` + `CostStatus` enums
* `Entity` — extracted named entities (`EntityType`: person, org, court, law_firm, …)
* `UserSettings` — single-user preferences (model selection, UI flags)
* `Conversation` + `ConversationMessage` — chat sessions with case context

## Vector search

Embeddings are stored as f32 blobs in the `document_vectors` sqlite-vec virtual table:

```sql
CREATE VIRTUAL TABLE document_vectors USING vec0(
    document_id INTEGER PRIMARY KEY,
    embedding float[768]
);
```

Dimension is configured via `AI_EMBED_DIM` in `app/config.py` (default 768 for nomic-embed-text). KNN queries use `WHERE embedding MATCH :blob ORDER BY distance LIMIT :k`. The `alembic/env.py` loads the sqlite-vec extension before running migrations. Search merges vector results with `ilike` results in `app/services/search_service.py`.

## Routes

All routes follow REST conventions. See `app/api/` for the complete listing.

**First-class views:** Case management (`/cases/*`), Triage (`/triage`), Chat (`/api/chat/*`), Contacts (`/contacts/{sender}`), Costs (`/costs`), Settings (`/settings*`, `/api/settings/*`), Upload (`/upload`), Slicing (`/ingest/slice/*`).

## Navigation and ID conventions

* **Sidebar** is a 56px icon-rail nav (Home, Cases, Search, Settings). It is not a case list.
* **`Case.id`** (e.g. `ADV-024-A`) is the lead identifier in: top-bar pill, breadcrumb, URLs, chat, reports.
* **Breadcrumb format:** `Cases › ADV-024-A · Case Title`
* Per-court Aktenzeichen lives on `Proceeding.az_court` — it is context, never the primary identity.

## Rules
* **Pre-release — clean as you go.** Working with test data only. When a field, table, model, route, or template becomes unused or superseded, **remove it in the same change** — no deprecation shims, no backwards-compat layers, no "keep for now" comments. Migrations drop columns; templates lose unused branches; obsolete routes disappear. No dead code accumulates before v1.
* **Internal ID is the lead everywhere.** `Case.id` (e.g. `ADV-024-A`) is shown in sidebar, breadcrumb, URLs, chat, reports. Per-court Aktenzeichen lives on `Proceeding.az_court` — context, never identity.
* **Management Summary:** 3-bullet (Legal Significance, Action/Deadline, Financial Impact).
* **Triage:** No `case_id`/`parent_id` → Triage Inbox. Bundle by `ingest_batch_id`.
* **Graph first:** primary case view is the correspondence swim-lane graph, not a document list.
* **AI answers cite sources** — every AI response references the document and passage it drew from.
* **Before editing any file, read it first. Before modifying a function, grep for all callers. Research before you edit.
* **Email body is transport-only.** When an email has attachments, the email body is intentionally discarded during ingest — the body is a cover note only; all substantive correspondence from the lawyer arrives as attached PDF letters. Do not "fix" this.

## Run
```bash
make setup      # Install/Update
make run        # Terminal 1: App
make worker     # Terminal 2: Celery worker (required when CELERY_TASK_ALWAYS_EAGER=false)
make watch-css  # Terminal 3: CSS
make seed       # Seed Data
make test       # Run Tests
make lint       # Pre-commit hooks
make migrate    # Run migrations
```
`get_db()` in `app/dependencies.py`. Migrations: `alembic revision --autogenerate -m "..." && alembic upgrade head`

**Testing — run serially.** The suite shares one SQLite test DB and has no per-process isolation. Run **one** `pytest`/`make test` at a time and wait for it; never launch overlapping runs (a `conftest.py` lock fails fast on the second). The DB is tmpfs-backed (`/dev/shm`) so a single run is disk-cheap.
