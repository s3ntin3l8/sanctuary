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
* **Frontend:** React 19 + Vite + TypeScript SPA in `frontend/` (migration in progress, view by view — see `docs/frontend-migration-plan.md`); views not yet cut over are still Jinja + HTMX + Alpine.js
* **Styling:** Tailwind CSS v4 — SPA tokens in `frontend/src/styles/index.css` (prototype in `docs/design/`); legacy `static/input.css`
* **DB:** PostgreSQL + Alembic + `pgvector`
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

Embeddings live as `pgvector` columns directly on their owning tables — no separate vector table:

```python
embedding: Mapped[list[float] | None] = mapped_column(
    Vector(AI_EMBED_DIM), nullable=True
)
```

on `DocumentChunk.embedding` (passage-level document retrieval) and `Claim.embedding` (semantic claim dedup). Dimension is configured via `AI_EMBED_DIM` in `app/config.py` (default 768 for nomic-embed-text) and baked into the column at migration time; changing it requires clearing the column and resizing (`ALTER TABLE ... ALTER COLUMN embedding TYPE vector(N)`, see Settings → AI → Rebuild Index). KNN queries use pgvector's `<->` (L2) operator via SQLAlchemy's `Column.l2_distance(vec)`, HNSW-indexed. Search merges vector results with `ilike` results in `app/services/search_service.py`.

## Routes

All routes follow REST conventions. See `app/api/` for the complete listing.

**Typed JSON API for the SPA:** `app/api/v1/*` — every route declares a Pydantic `response_model` (`app/schemas/`), raises `ApiError(status, code, detail)` and gets the uniform `{detail, code}` error body. `make api-types` regenerates `frontend/src/api/{openapi.json,schema.d.ts}`; a unit test fails when the committed schema is stale. SPA-owned paths return `spa_index()` (`app/spa.py`); the client calls them through `openapi-fetch` (`frontend/src/api/client.ts`).

**First-class views:** Cases (`/cases`, `/cases/{id}` SPA; `/api/v1/cases/{id}` detail, `/graph`, `/timeline`, `/truthmap`, `/financials`, `/shares`, `/api/v1/claims/*`, `/api/v1/costs/*`, `/api/v1/proceedings/*`), Triage (`/triage` SPA, `/api/v1/triage/*`, `/api/v1/documents/*`, `/api/v1/upload`, `/api/v1/slicing/*`), Document HUD (`/document/{id}` SPA, `/api/v1/documents/{id}/reader`, pins `/api/v1/pins/*`), Chat (`/api/v1/chat/*`, SSE streaming), Contacts (`/contacts?name=` SPA, `/api/v1/contacts`), Costs ledger (`/costs` SPA, `/api/v1/costs`, `/api/v1/cases/{id}/costs`), Search (`/search` SPA, `/api/v1/search`), Settings (`/settings/*` SPA, `/api/v1/settings/*`, `/api/v1/admin/*`), Slicing review (`/ingest/slice/{batch_id}` SPA).

## Navigation and ID conventions

* **Sidebar** is a 56px icon-rail nav (Home, Triage, Cases; theme, search/⌘K, processing queue, profile menu → Settings). It is not a case list. SPA: `frontend/src/shell/`.
* **`Case.id`** (e.g. `ADV-024-A`) is the lead identifier in: top-bar pill, breadcrumb, URLs, chat, reports.
* **Breadcrumb format:** `Cases › ADV-024-A · Case Title`
* Per-court Aktenzeichen lives on `Proceeding.az_court` — it is context, never the primary identity.

## Rules
* **Pre-release — clean as you go.** Working with test data only. When a field, table, model, route, or template becomes unused or superseded, **remove it in the same change** — no deprecation shims, no backwards-compat layers, no "keep for now" comments. Migrations drop columns; templates lose unused branches; obsolete routes disappear. No dead code accumulates before v1.
* **Internal ID is the lead everywhere.** `Case.id` (e.g. `ADV-024-A`) is shown in sidebar, breadcrumb, URLs, chat, reports. Per-court Aktenzeichen lives on `Proceeding.az_court` — context, never identity.
* **Management Summary:** 3-bullet (Legal Significance, Action/Deadline, Financial Impact).
* **Triage:** No `case_id`/`parent_id` → Triage Inbox. Bundle by `ingest_batch_id`.
* **Graph first:** primary case view is the correspondence swim-lane graph (`frontend/src/features/cases/dashboard/GraphTab.tsx`, layout from `CaseGraphService`), not a document list.
* **AI answers cite sources** — every AI response references the document and passage it drew from.
* **Before editing any file, read it first. Before modifying a function, grep for all callers. Research before you edit.
* **Email body is transport-only.** When an email has attachments, the email body is intentionally discarded during ingest — the body is a cover note only; all substantive correspondence from the lawyer arrives as attached PDF letters. Do not "fix" this.

## Run
```bash
make setup      # Install/Update
make run        # App + both Celery workers + beat scheduler (Terminal 1)
make watch-css  # Terminal 2: legacy CSS
make watch-frontend  # Terminal 3: rebuild the SPA on change (frontend/dist, served by the app)
make frontend-test   # SPA typecheck + lint + vitest
make api-types  # Regenerate the SPA's API types after changing app/api/v1
make seed       # Seed Data
make test       # Run Tests
make lint       # Pre-commit hooks
make migrate    # Run migrations
```
`make run` starts everything needed for the AI pipeline in one process group — no separate worker terminal needed (default `CELERY_TASK_ALWAYS_EAGER=false`). To run the web server and workers as separate processes instead (e.g. to restart one without the other), use `make server` + `make worker` in two terminals. `make worker-ingest` / `make worker-ai` start a single queue standalone.

`get_db()` in `app/dependencies.py`. Migrations: `alembic revision --autogenerate -m "..." && alembic upgrade head`

**Testing.** `pytest`/`make test` runs across CPU cores by default (pytest-xdist, `-n auto`); each worker starts its own dedicated Postgres+pgvector container (`testcontainers`, session-scoped fixture in `tests/conftest.py`) on a random host port, so workers of the same invocation — and separate, overlapping `pytest` invocations — never contend on a shared database. Requires a working Docker socket.
