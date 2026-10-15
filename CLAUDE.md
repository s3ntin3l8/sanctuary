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
* **Frontend:** React 19 + Vite + TypeScript SPA in `frontend/` (React Router, TanStack Query, `openapi-fetch`); FastAPI serves the built bundle from `frontend/dist` and returns `spa_index()` for every page route. No server-rendered HTML.
* **Styling:** Tailwind CSS v4 via `@tailwindcss/vite` — tokens in `frontend/src/styles/index.css` (design reference in `docs/design/`); fonts self-hosted through `@fontsource`
* **DB:** PostgreSQL + Alembic + `pgvector`
* **AI:** Auto-detect ollama / lmstudio / openai
* **Ingestion:** Docling (PDF → Markdown)
* **Rate limiting:** `slowapi`

## Key data model concepts

* `IngestBatch` — email/scan group; case assignment cascades to all children. Keeps the email's `message_id`, `in_reply_to` and `thread_refs` (References) for every email, attachments or not
* `Proceeding` — court level within a case (AG → OLG → BGH); graphs are scoped per proceeding
* `DocumentRelationship` — typed N:N edges (`replies_to`, `references`, `attaches_as_proof`, `supersedes`, `cited_by`, `encloses`, `copy_of`). `copy_of` (copy → original, written by `copy_detector` after ENRICH: same-day, tied by sender/AZ/reference, ≥75% word-5-gram containment on distinctive text, i.e. beyond the sender's shared boilerplate) marks a document filed again, e.g. an exhibit copying an earlier ruling; the copy skips RELATIONSHIPS/CLAIMS and action items and folds onto its original in graph and timeline. `confidence` is the provenance: `ai_detected` (suggestion), `user_confirmed`/`user_created`, or `email_header` (derived deterministically from In-Reply-To/References by `thread_header_linker`; exact In-Reply-To → `replies_to` and closes the thread, References-only → `references` and does not; when no header resolves, the nearest earlier batch of the same Gmail `threadId` (from `GmailMessageIndex`) earns a non-closing `references`; a matching `ai_detected` edge is upgraded, and a stale `email_header` edge is replaced when a nearer ancestor arrives). Rejecting an edge records it in `RejectedRelationship`; `insert_edge_if_absent` never writes a rejected (from, to, type) again
* AI relationship detection treats "prior" as *earlier document date* (`issued_date`, id as tie-break; undated docs fall back to arrival order), so a late-scanned old letter is still seen by the newer docs: after a doc's detection completes, `relationship_backfill` re-queues (≤10, no cascade) the newer docs that ran before it existed; candidates come from two pools tagged `Via=thread|topic` in the prompt (same Aktenzeichen/file/proceeding then nearest date, vs. pgvector KNN)
* **Family context** (`app/services/family_context.py`) — a document's family is its sub-group, else its `parent_id` tree. `propagate`/`propagate_batch` are the single place that spreads a family's case (provenance in `extraction_confidence["case_id"]`: absent = own reference, `inherited`, `user_set`) and its proceeding. The proceeding only reaches members that print no Aktenzeichen of their own: observed beats inherited, so an exhibit copying another matter's ruling is never filed into the live proceeding, and never creates, forks or closes one. The exception is a family headed by a court relay: the court decides where it files what it forwards (a filing sent under a closed Aktenzeichen arrives under the new one), so the relay's proceeding governs every member while `az_court` stays as printed. The slicer enforces the matching structure: a part opening with a court letterhead and a forwarding sentence (read from the full page text, not the stored head) makes every following part its enclosure until a court letter to the same recipient follows (`_apply_relay_spans`). `is_exhibit` = an enclosure of a party's filing (never of the user's own email body: that is transport, so a court letter they forward keeps its deadlines); `is_relay` = a court wrapper
* **Slicing** — a scanned PDF becomes an `AWAITING_SLICING` batch (`ingest_scanned_file`) and is cut before any document exists. A PDF that arrives as an email attachment is already a document, so it is split afterwards: `POST /api/v1/slicing/documents/{id}` → review at `/document/{id}/slice` → confirm replaces it by parts (`slicing_service.confirm_document_slices`). Parts take the source's place (same parent/sub-group; a lead under the mail, attachments under the lead), are handed its per-page OCR (`meta.prefilled_extraction`, consumed by EXTRACT; `ingestion/page_text.py`) and the source is deleted. Refused (`has_user_data`) while it has pins, reactions, enclosures or user/agent-confirmed evidence or edges. After EXTRACT, a PDF nobody has cut that was read page by page gets `meta.packet_starts` (`slicer.likely_document_starts`: strong start cues only, no AI), shown in the review as a hint with a Split button; nothing is held back
* `Claim` + `ClaimEvidence` — atomic factual assertions and their evidence chain (the Truth Map)
* `UserReaction` — triage reactions (🚩/✅/🔍/⚖️) stored and recalled by AI during case brief and document enrichment
* `ActionItem` — deadlines and court dates extracted from documents, first-class records
* `Document.significance_tier` — AI-assigned; drives graph visibility
* `Document.court_relay` + `Document.attributed_originator` — true sender behind court routing
* `DocumentPin` — passage-anchored margin annotations (distinct from `UserReaction`; stores span offsets)
* `LegalCost` — German RVG/GKG/JVEG cost tracking per proceeding; `CostCategory` + `CostStatus` enums
* `Entity` — extracted named entities (`EntityType`: person, org, court, law_firm, …)
* `ApiToken` + `ApiTokenCase` — bearer credential an external agent uses on the MCP endpoint (`/mcp`, bearer-only, enforced in `AuthGateMiddleware` regardless of `AUTH_ENABLED`). Acts as its owner and can never exceed them: `all_cases` is a true wildcard (future cases included), otherwise the `ApiTokenCase` rows; `access` is `read` or `read_write`. Only the sha256 of the secret is stored; revocation is soft. Managed in Settings → Agent access (`/api/v1/settings/agent-access`). `kind` is `static` (pasted) or `oauth`: MCP clients that only speak OAuth connect through Sanctuary's own authorization server (`app/mcp/oauth.py`, SDK handlers for `/authorize`, `/token`, `/register`, `/revoke`, metadata per request; public clients, PKCE S256 only; consent page `/oauth/authorize`, `/api/v1/oauth/*`). Redeeming a consent code mints an `oauth` `ApiToken` + `OAuthGrant` whose `token_hash` is the current 1-hour access token; refresh tokens rotate and reuse of a rotated one revokes the grant. Rows an agent writes (`UserReaction`, `DocumentPin`, `Claim`, `ClaimEvidence`, `ActionItem`, `DocumentRelationship`, `IngestBatch`) carry a nullable `agent_token_id` (null = the user's own); the SPA shows a "via agent" badge and `ActionItem`s with a tag survive AI re-enrichment. Admins see every token, can revoke any, read its audited activity and flip the instance-wide `mcp_enabled` switch (`AppSettings.settings_json`, default on; off → `/mcp` answers 503 before any token check and minting is refused) in Settings → Admin → Agent access (`/api/v1/admin/agent-access`)
* `UserSettings` — single-user preferences (model selection, UI flags)
* `Conversation` + `ConversationMessage` — chat sessions with case context

## Vector search

Embeddings live as `pgvector` columns directly on their owning tables — no separate vector table:

```python
embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
```

on `DocumentChunk.embedding` (passage-level document retrieval) and `Claim.embedding` (semantic claim dedup). The ORM type is deliberately **unsized**: the width lives only in the database column, set by the baseline migration from `AI_EMBED_DIM` in `app/config.py` (default 768 for nomic-embed-text). Never declare `Vector(N)` in the model — SQLAlchemy's batched INSERT casts to that width and rejects vectors from a resized column. Changing the dimension requires clearing the column and resizing (`ALTER TABLE ... ALTER COLUMN embedding TYPE vector(N)`, see Settings → AI → Rebuild Index). KNN queries use pgvector's `<->` (L2) operator via SQLAlchemy's `Column.l2_distance(vec)`, HNSW-indexed. Search merges vector results with `ilike` results in `app/services/search_service.py`.

## Routes

All routes follow REST conventions. See `app/api/` for the complete listing: `app/api/v1/*` is the JSON API, `app/api/{home,spa_pages,settings_page}.py` are the page routes that hand the SPA shell to the browser, `app/api/auth*.py` the login/OIDC flow.

**Typed JSON API for the SPA:** `app/api/v1/*` — every route declares a Pydantic `response_model` (`app/schemas/`), raises `ApiError(status, code, detail)` and gets the uniform `{detail, code}` error body. `make api-types` regenerates `frontend/src/api/{openapi.json,schema.d.ts}`; a unit test fails when the committed schema is stale. SPA-owned paths return `spa_index()` (`app/spa.py`); the client calls them through `openapi-fetch` (`frontend/src/api/client.ts`).

**First-class views:** Cases (`/cases`, `/cases/{id}` SPA; `/api/v1/cases/{id}` detail, `/graph`, `/timeline`, `/truthmap`, `/financials`, `/shares`, `/api/v1/claims/*`, `/api/v1/costs/*`, `/api/v1/proceedings/*`), Triage (`/triage` SPA, `/api/v1/triage/*`, `/api/v1/documents/*`, `/api/v1/upload`, `/api/v1/slicing/*`), Document HUD (`/document/{id}` SPA, `/api/v1/documents/{id}/reader`, pins `/api/v1/pins/*`), Chat (`/api/v1/chat/*`, SSE streaming), Contacts (`/contacts?name=` SPA, `/api/v1/contacts`), Costs ledger (`/costs` SPA, `/api/v1/costs`, `/api/v1/cases/{id}/costs`), Search (`/search` SPA, `/api/v1/search`), Settings (`/settings/*` SPA, `/api/v1/settings/*`, `/api/v1/admin/*`), Slicing review (`/ingest/slice/{batch_id}` SPA).

**MCP endpoint:** `/mcp` (Streamable HTTP, stateless; `app/mcp/`, mounted in `app/main.py` via `McpGateway`) lets external agents read case data with a bearer `ApiToken`. Every tool takes a `McpPrincipal` (`app/mcp/principal.py`) and enforces access through it (`require_case`, `require_document`, `readable_case_ids`) — never ad hoc. Tool bodies are plain sync `f(db, principal, …)` functions run on a worker thread by `app/mcp/db.run`; shared logic lives in services (`case_overview_service`, `document_review_service`, …), never imported from `app/api`. Document text is returned fenced with `serializers.untrusted()`.

## Navigation and ID conventions

* **Sidebar** is a 56px icon-rail nav (Home, Triage, Cases; theme, search/⌘K, processing queue, profile menu → Settings). It is not a case list. SPA: `frontend/src/shell/`.
* **`Case.id`** (e.g. `ADV-024-A`) is the lead identifier in: top-bar pill, breadcrumb, URLs, chat, reports.
* **Breadcrumb format:** `Cases › ADV-024-A · Case Title`
* Per-court Aktenzeichen lives on `Proceeding.az_court` — it is context, never the primary identity.

## Rules
* **Pre-release — clean as you go.** Working with test data only. When a field, table, model, route, or template becomes unused or superseded, **remove it in the same change** — no deprecation shims, no backwards-compat layers, no "keep for now" comments. Migrations drop columns; templates lose unused branches; obsolete routes disappear. No dead code accumulates before v1.
* **Internal ID is the lead everywhere.** `Case.id` (e.g. `ADV-024-A`) is shown in sidebar, breadcrumb, URLs, chat, reports. Per-court Aktenzeichen lives on `Proceeding.az_court` — context, never identity.
* **Management Summary:** 3-bullet (Legal Significance, Action/Deadline, Financial Impact). A court relay (cover letter forwarding someone else's filing) gets one line instead: the court's instruction (zur Kenntnisnahme / Stellungnahme binnen X / Verfügung), whose deadline is kept as an action item. Exhibits (enclosures of a party's filing) never create action items.
* **Triage:** No `case_id`/`parent_id` → Triage Inbox. Bundle by `ingest_batch_id`.
* **Graph first:** primary case view is the correspondence swim-lane graph (`frontend/src/features/cases/dashboard/GraphTab.tsx`, layout from `CaseGraphService`), not a document list.
* **AI answers cite sources** — every AI response references the document and passage it drew from.
* **Before editing any file, read it first. Before modifying a function, grep for all callers. Research before you edit.
* **Email body is transport-only.** When an email has attachments, the email body is intentionally discarded during ingest — the body is a cover note only; all substantive correspondence from the lawyer arrives as attached PDF letters. Do not "fix" this. The exception is the user's **own** mail (sender in `gmail_own_addresses` — the connected account's address plus its verified send-as aliases, read from Gmail on connect and refreshed daily by `connect_gmail`; Settings → Gmail → "Also import mail I sent"): their text is the substance, so its body is kept alongside the attachments. Stored bodies hold the new text only — quoted reply history is stripped (`strip_quoted_history`), while the attachment manifest is still read from the full body.

## Run
```bash
make setup      # Install/Update
make run        # App + the three Celery workers (ingest, ai, maintenance) + beat scheduler (Terminal 1)
make watch-frontend  # Terminal 2: rebuild the SPA on change (frontend/dist, served by the app)
make frontend-test   # SPA typecheck + lint + vitest
make api-types  # Regenerate the SPA's API types after changing app/api/v1
make seed       # Seed Data
make test       # Run Tests
make lint       # Pre-commit hooks
make migrate    # Run migrations
```
`make run` starts everything needed for the AI pipeline in one process group — no separate worker terminal needed (default `CELERY_TASK_ALWAYS_EAGER=false`). To run the web server and workers as separate processes instead (e.g. to restart one without the other), use `make server` + `make worker` in two terminals. `make worker-ingest` / `make worker-ai` / `make worker-maintenance` start a single queue standalone. The `maintenance` queue (recovery sweep, thread scan, log pruning) has its own worker so the sweep never waits behind LLM- or OCR-gated work.

`get_db()` in `app/dependencies.py`. Migrations: `alembic revision --autogenerate -m "..." && alembic upgrade head`

**Model cache.** `worker-ingest` and `worker-ai` mount the `model_cache` volume at `/root/.cache` so Docling/HF models are downloaded once, not on every image update. The image uses headless OpenCV and prunes tests/headers/unused Google discovery docs in the builder stage — keep prunes there, never after the `COPY --from`.

**Testing.** `pytest`/`make test` runs across CPU cores by default (pytest-xdist, `-n auto`); each worker starts its own dedicated Postgres+pgvector container (`testcontainers`, session-scoped fixture in `tests/conftest.py`) on a random host port, so workers of the same invocation — and separate, overlapping `pytest` invocations — never contend on a shared database. Requires a working Docker socket.
