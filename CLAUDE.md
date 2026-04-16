# CLAUDE.md: The Sanctuary

Privacy-first legal case management. All AI runs locally via Ollama. "Quiet Sanctuary" aesthetic — high density, dark slate, minimal chrome, zero unnecessary chrome.

Full product vision: `docs/vision.md`

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
* **Backend:** Python 3.12+ / FastAPI
* **Frontend:** HTMX + Alpine.js
* **Styling:** Tailwind CSS v4 (`static/input.css` dual light/dark tokens)
* **DB:** SQLite + Alembic + `sqlite-vec`
* **AI:** Ollama (`qwen3.5:9b` summaries, `nomic-embed-text` embeddings)
* **Ingestion:** Docling (PDF → Markdown)

## Key data model concepts (target)
* `IngestBatch` — email/scan group; case assignment cascades to all children
* `Proceeding` — court level within a case (AG → OLG → BGH); graphs are scoped per proceeding
* `DocumentRelationship` — typed N:N edges (`replies_to`, `references`, `attaches_as_proof`, `supersedes`)
* `Claim` + `ClaimEvidence` — atomic factual assertions and their evidence chain (the Truth Map)
* `UserReaction` — triage reactions stored and recalled by AI
* `ActionItem` — deadlines and court dates extracted from documents, first-class records
* `Document.significance_tier` — AI-assigned; drives graph visibility
* `Document.court_relay` + `Document.attributed_originator` — true sender behind court routing

## Rules
* **Management Summary:** 3-bullet (Legal Significance, Action/Deadline, Financial Impact).
* **Triage:** No `case_id`/`parent_id` → Triage Inbox. Bundle by `ingest_batch_id`.
* **Graph first:** primary case view is the correspondence swim-lane graph, not a document list.
* **AI answers cite sources** — every AI response references the document and passage it drew from.

## Run
```bash
make setup      # Install/Update
make run        # Terminal 1: App
make watch-css  # Terminal 2: CSS
make seed       # Seed Data
make test       # Run Tests
```
`get_db()` in `app/dependencies.py`. Migrations: `alembic revision --autogenerate -m "..." && alembic upgrade head`
