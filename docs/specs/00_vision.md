# Sanctuary — Vision & V1 Design Specification

---

## North Star Vision

Sanctuary is a **case intelligence engine**, not a document archive. Every document that arrives advances the AI's understanding of a case. The primary interface is not a file list — it is the living structure of the legal battle itself.

The fundamental shift: **documents are evidence. Cases are the thing you're actually managing.**

The primary interaction loop:

1. Email arrives from lawyer → documents auto-sorted into cases and proceedings, bundles detected
2. AI reads each document in full case context → updates case brief, extracts action items, identifies factual claims, links to related documents
3. Triage is a strategy session — user reacts to new documents, AI captures those reactions as high-weight context
4. Case view is the correspondence graph — a visual map of who said what to whom, what's contested, what's open
5. User asks questions about any document or case in natural language; AI answers with cited sources

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete — all phases through Phase 8)

### Phase Matrix

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | Data foundation — models, migrations, repositories | ✅ Implemented |
| Phase 2 | Triage redesign — bundle-aware queue, HUD layout, reaction bar | ✅ Implemented |
| Phase 3 | Email ingest pipeline — Gmail OAuth, scan folder, .eml upload, slicing | ✅ Implemented |
| Phase 4 | Document intelligence — key passages, significance, claims, cost delta, relationships | ✅ Implemented |
| Phase 5 | Case AI intelligence — brief, case clock, financial exposure, proceeding scope | ✅ Implemented |
| Phase 6 | Truth Map — contested claims, evidence chain, claim status lifecycle | ✅ Implemented |
| Phase 7 | AI Chat — document + case scoped, SSE streaming, citations, conversation persistence | ✅ Implemented |
| Phase 8 | Correspondence graph — swim-lane SVG, proceeding-scoped, N:N edges, reaction indicators | ✅ Implemented |

### Implementation Deviations

| Feature | Vision | Code | Status |
|---------|--------|------|--------|
| AI chat streaming | WebSocket | HTTP/1.1 chunked transfer with SSE | ✅ Accepted — lower complexity, equivalent latency (~50–100ms/chunk) |
| Image input formats | JPG/HEIC/TIFF listed | PDF-only input; scanners produce PDF natively | ✅ Accepted — simplifies pipeline; image conversion deferred |
| Pin/annotation routes | Nested `/document/:id/annotation` | Flat `/pin/:pin_id` (globally unique PK; `DocumentPin` model) | ✅ Accepted — integration tests confirm flat shape; spec §12b updated |
| Phase 7 AI Chat in HUD | Stub button in v1; drawer in Phase 7 | Fully implemented drawer as part of v1 | ✅ Promoted — shipped earlier than originally phased |

---

## Mental Models

### Primary ingest: email

95% of all case communication arrives via email from the lawyer — one email per case, but potentially multiple document bundles per email. Scanned documents supplement this but are secondary.

The **email is the atom of delivery**. All documents from one email are treated as a family (`IngestBatch`) and processed together.

### Documents have two overlapping structures

**Physical delivery** (how they arrive):
```
Email → Court Cover Letter → [Opposing Statement, Annexes]
                           → [Child Services Report]
```

**Logical communication** (who is actually talking):
```
Opposing Side ──via court──► You   (Statement + Annexes)
Child Services ──via court──► You  (Report)
```

The court is usually a **relay**, not an actor. It routes documents from opposing counsel or third parties under a bureaucratic cover letter ("Begleitschreiben"). Occasionally it adds substance — a deadline, a decision, a comment. The system must distinguish these two roles.

### The Russian doll: parent-child document structure

Court cover letters wrap enclosed documents. Those enclosures may themselves contain attachments used as proof. This creates a hierarchy:

```
Cover Letter (court relay)
├── Opposing Statement
│   └── Anlage K1 — attached as proof, not an independent communication
└── Child Services Report
```

`parent_id` captures the physical containment. A separate `attaches_as_proof` relationship type captures when a document is cited as evidence rather than being an independent actor in the correspondence.

### Proceedings are nested inside cases

A case can escalate through multiple court levels. Each level is a **Proceeding** with its own timeline, parties, and documents:

```
Case ADV-024-A
├── Proceeding: Amtsgericht Hamburg  (§ 1671 BGB, custody)
│   ├── Klage (you)
│   ├── Klageerwiderung (opposing)
│   ├── Beschluss (AG ruling)
│   └── → triggers Beschwerde
└── Proceeding: Oberlandesgericht Hamburg  (Beschwerde)
    ├── Beschwerdeschrift (you)
    ├── Stellungnahme Jugendamt
    └── ...
```

Documents belong to a proceeding. The correspondence graph is scoped per proceeding by default — switching proceeding shows a completely different graph. Cross-proceeding references exist but are visually distinct.

### Case IDs — internal is the lead

A single case carries multiple identifiers in the real world:

| ID | Example | Scope | Stability |
|---|---|---|---|
| **Internal ID** (`Case.id`) | `ADV-024-A` | Your counsel | Permanent, stable across all courts |
| **Court Az** (`Proceeding.az_court`) | `003 F 426/25` (AG), `12 UF 89/25` (OLG) | Per court level | Changes on escalation |
| **External refs** | Jugendamt ref, opposing counsel ref | Per third party | Varies |

**The internal ID is the lead identifier everywhere** — sidebar, breadcrumb, URLs, chat, cross-references, reports. Reasoning:

- It's yours. The matter stays named `ADV-024-A` whether it's at AG, on Beschwerde at OLG, or closed.
- Court Az numbers are context-specific — there is no single "the court ID" for a case that has moved through three courts.
- The internal ID is stable, addressable, human-readable.

Per-court Aktenzeichen live on `Proceeding.az_court`. The pre-Proceeding era `Case.court_id` column was dropped (migration `cc7bed04fc19`).

#### Display rules

| Surface | Primary | Secondary | Format |
|---|---|---|---|
| Sidebar / breadcrumb | Internal ID | — | `ADV-024-A` |
| Triage batch header | Internal ID | Proceeding name | `[ADV-024-A?] · AG Hamburg` |
| Document HUD | Internal ID | Proceeding + Az | `ADV-024-A · AG Hamburg · 003 F 426/25` |
| Case list row | Internal ID + title | Active proceeding | `ADV-024-A — Custody dispute · AG Hamburg` |
| URLs | Internal ID | — | `/cases/ADV-024-A` |
| AI chat answers | Internal ID | Az when quoting court docs | "The ruling [ADV-024-A, AG 003 F 426/25] sets a deadline of…" |

The Az is shown but never lead — it's context for the proceeding, not the identity of the case.

### What the AI assigns at ingest

Phase 4 (document intelligence) populates these fields. The UI shows them when present, renders empty blocks with a processing indicator when still running.

| Field | Purpose | Values |
|---|---|---|
| `Document.significance_tier` | Drives graph visibility and triage sort order | `critical` / `significant` / `informational` / `administrative` |
| `Document.document_type` | Classifies the document | `ruling`, `motion`, `statement`, `annex`, `relay`, `correspondence`, `report`, `invoice`, `other` |
| `Document.attributed_originator` | True sender behind court routing | Free text (e.g., "Opposing counsel", "Jugendamt") |
| `Document.court_relay` | Is this a pass-through cover letter? | boolean |
| `Document.key_passages` | AI-identified significant excerpts | `[{text, rationale, span}, …]` |
| `Document.cost_delta` | Financial impact of this document | `{amount, direction, description}` |
| `Claim` rows | Factual/legal assertions the doc makes | linked via `source_document_id` |
| `DocumentRelationship` rows | Proposed edges to prior docs | `confidence=ai_detected`, user confirms later |

`OriginatorType` now includes `THIRD_PARTY` (amber) for non-court / non-opposing / non-own actors — Jugendamt, Verfahrensbeistand, Sachverständige, etc. These often route via the court but are substantively independent.

### Document relationships are many-to-many

A document can respond to multiple prior documents simultaneously. A letter from opposing counsel may react to both a court ruling AND a child services report. This requires a proper relationship graph, not a single FK:

```
DocumentRelationship:
  from_doc ─[replies_to]──────► ruling
  from_doc ─[replies_to]──────► child_services_report
  from_doc ─[attaches_as_proof]► old_court_order  (citation, not independent)
  from_doc ─[supersedes]───────► earlier_version
```

### Three intelligence layers

All views of a case are powered by three stacked intelligence layers:

```
STRATEGIC LAYER     Case health · Financial delta · Case Clock · Open threads
                            ↑ fed by
FACTUAL LAYER       Truth Map: contested claims · evidence strength · user reactions
                            ↑ fed by
STRUCTURAL LAYER    Correspondence graph: who said what to whom, relationships
                            ↑ fed by
DOCUMENTS           (evidence — not the primary object)
```

### Significance tiers filter the noise

With 900+ court letters and growing, not everything deserves equal attention. Every document gets a tier assigned by AI at ingest:

| Tier | Meaning | Graph visibility |
|---|---|---|
| `critical` | Decision, ruling, deadline | Always shown |
| `significant` | Substantive statement, motion | Shown by default |
| `informational` | Factual update, acknowledgment | Collapsed by default |
| `administrative` | Cover letter relay, receipt confirmation | Hidden by default |

Administrative documents still exist — they're just not rendered as graph nodes unless explicitly expanded. This reduces 900 letters to the ~150–200 that actually matter at the default filter level.

---

## Data Model

### New tables

**`Proceeding`**
```
id, case_id, court_name, court_level (ag|lg|olg|bgh|other),
subject_matter, started_at, ended_at, status (active|closed),
az_court (court file number)
```

**`DocumentRelationship`** — replaces the simple `in_reply_to_id` FK
```
id, from_document_id, to_document_id,
relationship_type (replies_to|references|attaches_as_proof|supersedes|cited_by),
confidence (ai_detected|user_confirmed|user_created),
notes
```

**`IngestBatch`** — groups documents that arrived together
```
id, source_type (email|scan|manual), received_at,
sender_email, subject, raw_source_path,
case_id (detected), proceeding_id (detected), status
```

**`ActionItem`** — extracted deadlines and court dates
```
id, case_id, proceeding_id, source_document_id,
due_date, description,
action_type (deadline|court_date|response_required|filing_required),
status (open|completed|dismissed), created_at
```

**`Claim`** — an atomic factual assertion made in a document
```
id, case_id, proceeding_id, source_document_id,
claim_text, claim_type (factual|legal|procedural),
status (asserted|contested|refuted|established),
first_made_at, last_updated_at
```

**`ClaimEvidence`** — links documents to claims they support, contest, or refute
```
id, claim_id, document_id,
role (supports|contests|refutes|cites_as_proof),
excerpt (the specific passage), confidence (ai_detected|user_confirmed)
```

**`UserReaction`** — captures the user's strategic reaction during triage
```
id, document_id, user_id,
reaction (lies|true|needs_proof|precedent),
notes (free text), created_at
```
Reactions are first-class strategic context — the AI uses them when answering
case-level questions ("what did I think of the opponent's third motion?").

**`Conversation`** — chat history, scoped to a case or document
```
id, scope_type (case|document), scope_id,
created_at, title (auto-generated from first message)
```

**`ConversationMessage`**
```
id, conversation_id, role (user|assistant),
content, context_document_ids[] (which docs were used as context),
created_at
```

### Extended: `Document`

| Field | Type | Purpose |
|---|---|---|
| `ingest_batch_id` | FK | Which email/scan batch this came from |
| `proceeding_id` | FK | Which court proceeding this belongs to |
| `role` | enum | `cover_letter`, `enclosure`, `standalone` |
| `court_relay` | bool | Court is routing only — not the actual author |
| `attributed_originator` | str | True author even if routed via court |
| `document_type` | enum | `ruling`, `motion`, `statement`, `annex`, `relay`, `correspondence`, ... |
| `significance_tier` | enum | `critical`, `significant`, `informational`, `administrative` |
| `thread_open` | bool | Awaiting response — no follow-up document detected yet |
| `key_passages` | JSON | AI-identified significant excerpts with rationale |
| `cost_delta` | JSON | Financial impact of this document (`{amount, direction, description}`) |

`parent_id` already exists and covers the physical cover letter → enclosure containment.

### Extended: `Case`

| Field | Type | Purpose |
|---|---|---|
| `ai_brief` | JSON | Cumulative AI understanding, updated on each new document |
| `ai_brief_updated_at` | datetime | Staleness tracking |
| `status` | enum | `active`, `dormant`, `closed` |
| `parties` | JSON | Known parties with roles (court, opposing, third parties) |
| `total_cost_exposure` | int | Running total of cost claims across all proceedings (cents) |

---

## UI Architecture

### Navigation architecture

Sanctuary's app chrome follows from the case-first model: **primary navigation surfaces only what's genuinely top-level — everything else is derived, contextual, or searchable.**

#### App shell: thin rail + command palette

The left edge of the app is a **thin icon rail** (~56px), not a wide sidebar. Labels appear on hover; no collapsed/expanded state — the rail is already minimal. The saved real estate goes to the correspondence graph, the AI brief, and the document HUD.

```
┌────┐
│ ●  │ ← brand
├────┤
│ ⌂  │ ← Home
│ ⊞ ●│ ← Triage (badge = pending count)
│ ▸  │ ← Cases
├────┤
│    │
├────┤
│ ⌘K │ ← Command palette
│ ⬆ │ ← Upload
│ 🔔 │ ← Notifications
│ ⚙  │ ← Settings (incl. Gmail config)
│ 🙂 │ ← User menu
└────┘
```

There is **no global top bar**. Per-page top bars (in the triage page, the case dashboard, etc.) own the page-level chrome — proceeding switcher, view mode tabs, page-specific actions. Eliminating the global header gives every pixel of vertical real estate back to content.

#### Three primary destinations

| Destination | Purpose |
|---|---|
| **Home** (`/`) | Cross-case priority view: action items due today/this-week, batches awaiting triage, cases with recent significant activity. The "what needs my attention right now" view you open first thing. |
| **Triage** (`/triage`) | The inbox of documents awaiting user confirmation, grouped by `IngestBatch`. See `docs/triage.md`. |
| **Cases** (`/cases`) | Compact list of cases (status, title, internal ID). Click a case → its dashboard. |

That's the entire primary nav. Everything else is reached contextually or through ⌘K.

#### Command palette (⌘K) is the scale mechanism

With only 3 primary nav items, the palette carries the weight of reaching anything else. It has three always-present sections, filtered simultaneously as you type:

```
⌘K  ________________________________

  Navigate
  > home
  > triage
  > case ADV-024-A
  > document #47

  Search
  > "Müller"     (3 cases, 12 documents, 2 contacts)
  > "Frist"      (5 action items, 8 documents)

  Actions
  > upload document
  > open Gmail settings
  > add new case
  > ask AI (global)
```

The palette replaces what would otherwise be a Contacts page, an Entities page, a cross-case search page, and several buried menu items.

#### Within-case navigation: graph-first

Once inside a case, the **correspondence graph is the primary interface** (see `docs/dashboard.md`). You navigate by clicking nodes in the graph; documents surface in context, never from a list.

The information hierarchy:
```
Rail (global)  →  Cases  →  Case dashboard (graph + brief)  →  Node click  →  Document HUD
                                       ↑                                              │
                                       └──────────── Proceeding switcher ─────────────┘
```

You never open a "document list." You always arrive at a document through the graph, with full case context already present. The one exception is the Timeline view mode on the case dashboard — a flat fallback for cases too early to have relationships detected, or for the occasional chronological scan.

#### What is explicitly *not* in primary nav

Each of these was a top-level destination in an earlier sidebar. Each pulled the user toward a cross-case flat-list mental model — the paradigm Sanctuary is built to escape.

| Removed | Replaced by |
|---|---|
| Master Timeline (cross-case flat list) | Deleted. Timeline exists as a view mode inside each case dashboard. See `docs/specs/09_timeline.md`. |
| Legal Costs (cross-case cost browser) | Case dashboard's Financials view mode; global pending-costs widget on Home; ⌘K aggregates. |
| Contacts (cross-case contact directory) | ⌘K search. A dedicated contacts page implies a file-manager mental model. |
| Entities (cross-case entity browser) | ⌘K search, same reason. |
| Activity Log (cross-case feed) | Notifications panel (rail 🔔) and Home feed. Not a navigation destination. |

The principle: **the primary nav never leads to a flat list.** If what you want is a flat list, you're always going through ⌘K or through a specific case's view mode — which keeps the case-as-object intact.

---

### 1. Triage — strategy session, not data entry

When documents arrive from one email, they enter triage as a **family**:

```
EMAIL  14. Apr  anwalt@kanzlei.de                    5 documents
┌─ BUNDLE A ──────────────────────────────────────────────────┐
│  ▤ Begleitschreiben LG Hamburg  [Court relay]  ← parent     │
│    ↳ ▤ Klageerwiderung Beklagter  [Opposing]                │
│    ↳ ▤ Anlage K1 — Rechnung  [Opposing → proof attach]      │
│  ⚑ Frist 30.04 (aus Begleitschreiben)                       │
└─────────────────────────────────────────────────────────────┘
┌─ BUNDLE B ────────────────────────────────────────────────┐
│  ▤ Begleitschreiben LG Hamburg  [Court relay]  ← parent   │
│    ↳ ▤ Jugendamtsbericht  [Child Services]                 │
└───────────────────────────────────────────────────────────┘
Proceeding: AG Hamburg  Case: ADV-024-A      [confirm & process all →]
```

**Document review layout:** large document view (left) + focused metadata form (right). The form shows only fields with low confidence or missing values — high-confidence fields are pre-confirmed. The document is shown as AI-annotated text with key passages highlighted, not as a raw PDF.

**The Reaction Bar** — at the moment of highest focus, the user captures their strategic read:

```
  🚩 Lies    ✅ True    🔍 Needs Proof    ⚖️ Precedent    [+ note]
```

These reactions are stored as `UserReaction` records and become high-weight context for all future AI queries about this document. "What did I think of the opponent's third motion?" recalls the reaction and any notes made at triage time.

After the reaction, the AI presents:
- **Claims identified** in this document (new assertions, refutations of prior claims)
- **Financial delta** if any cost claims are present
- **Relationship suggestions** — "This appears to respond to the ruling from March 14. Confirm?"

Case and proceeding assignment confirmed at batch level cascade to all children. Cover letter deadlines auto-create `ActionItem` records for the whole bundle.

### 2. Case dashboard — correspondence graph + AI brief

```
ADV-024-A  [Proceeding: AG Hamburg ▾]    [critical] [significant+] [all]
┌─────────────────────────────────────────────────────────────────────┐
│  YOU          COURT           OPPOSING        CHILD SERVICES        │
│   │              │                │                  │              │
│   ●──────────────►               │                  │              │
│   │          ╔═══╧════╗          │                  │              │
│   │          ║ Begl.  ║          │                  │              │
│   │          ║ Klagewi◄──────────●                  │              │
│   │   ⚑      ║ JA-Rpt.◄─────────────────────────────●             │
│   │          ╚═══╤════╝          │                  │              │
│   ●──────────────►               │                  │              │
│              [click to read]                                        │
├─────────────────────────────────────────────────────────────────────┤
│ AI BRIEF                        │ ACTION ITEMS                      │
│                                 │  ⚑ Apr 30  Stellungnahme  [open] │
│ Status: Active — awaiting your  │  · Jun 15  Verhandlungstermin    │
│ response to Jugendamtsbericht.  │                                   │
│ Key risk: Frist April 30.       │ FINANCIAL EXPOSURE                │
│ Cost exposure: 1.690 EUR.       │  Total claims:  1.690 EUR         │
│                                 │  Last delta:   +450 EUR (Apr 02)  │
│ [ask AI ✦]  [refresh brief]    │                                   │
└─────────────────────────────────────────────────────────────────────┘
```

### 3. Correspondence graph — design details

**Nodes:**
- One node per `significant` or `critical` document (`administrative` hidden by default)
- Court relay cover letters collapsed into a bundle node showing their enclosed documents
- `attaches_as_proof` documents shown as small citation badges on the referencing node — not independent nodes
- Node color = true originator; shape = document type; glow = significance tier
- Reaction indicator on node (🚩/✅/🔍/⚖️) if user reacted during triage

**Edges:**
- `replies_to`: solid directional arrow — multiple allowed per node (N:N)
- `references`: dashed arrow
- `attaches_as_proof`: not rendered as edge; shown as icon on node
- `supersedes`: thin gray arrow

**N:N multi-parent reply:**
```
[Ruling] ──────────────────────────────┐
                                        ├──► [Your Response]
[Child Services Report] ───────────────┘
```
Hover highlights which prior documents this is responding to.

**Proceeding scope:** switcher at top. AG/OLG/etc. are separate graphs. Cross-proceeding references shown as grayed edges linking to a collapsed "other proceeding" node.

**Significance filter:** `critical only` / `significant+` (default) / `all`. Expanding `all` reveals administrative relay letters.

### 4. Document HUD — "Director's Cut" reading

When a node is clicked, the document surfaces not as a raw PDF but as a **semantically highlighted view**. See [`docs/specs/04_document_hud.md`](04_document_hud.md) for the full spec; the key interaction model:

- AI-identified key passages highlighted inline (sky-blue token) — the one sentence in a 50-page Schriftsatz that actually shifts something
- Claim annotations inline — each key passage that grounds a claim carries an `⚖` chip; clicking it cross-references the Truth Map
- Pinned margin notes anchored to the passage they annotate (left gutter, amber cards with SVG leader lines)
- Right rail: AI summary, passages spine (scroll-spied), relationships, grounds, action items, cost delta, reaction bar
- Reaction pip in the sticky top bar shows the user's strategic read at a glance; full reaction bar and `+ note` in the right rail
- `[ask about this document ✦]` opens the AI chat drawer scoped to this document

The goal: reading a document should feel like reading a Director's Cut — the AI has already marked the traps and the wins.

### 5. Truth Map — contested claims view

A secondary view on a case (tab or toggle), showing the **factual layer** rather than the structural layer:

```
CONTESTED CLAIMS — ADV-024-A

  · Defendant's whereabouts on 2024-01-10
    ├── Asserted [doc #12, Opposing]  ✅ you confirmed
    ├── Contested [doc #31, Your filing]
    └── Evidence: Anlage K1 [doc #13]  — ⚖️ Precedent tagged

  · Child's primary residence preference
    ├── Asserted [doc #47, Jugendamt]  🔍 Needs Proof
    └── No counter-document yet  → thread open
```

Each claim shows its current status (asserted/contested/refuted/established), the evidence chain, and the user's own reactions from triage. Strength of evidence is visible at a glance — not as a percentage but as the balance of supporting vs. contesting documents.

**Full spec: `docs/specs/06_truth_map.md`**

### 6. Case Clock — temporal context

Below the action items panel, a **Case Clock** section shows:

- Time since last activity per proceeding
- Typical duration ranges for this proceeding type at this court: "AG Hamburg § 1671 proceedings typically reach first hearing 4–8 months after Klageerwiderung. Your filing was March 12 — typical window: July–November 2026."
- Dormancy alert: "This proceeding has been quiet for 6 months — longer than typical. Is something pending?"

Framed always as ranges with rationale, never as point predictions.

### 7. AI Chat — document and case scoped

**Document chat** (in document HUD):
```
✦ Ask about this document
> "What deadline does this set?"
  The document requires a Stellungnahme by April 30, 2026 (§ 91 ZPO).  [doc #47, p.3]
> "Does this contradict what I flagged in March?"
  Yes — you flagged doc #31 as 🚩 Lies regarding residence claim. This report
  partially supports the opposing position. [doc #47, p.7]
```

**Case chat** (in case dashboard):
```
✦ Ask about this case
> "Which opposing statements haven't been responded to yet?"
> "Summarize all cost claims and who bears them"
> "What did I flag as needing proof during triage?"
```

Every answer cites source documents. Conversation history persisted in `Conversation` / `ConversationMessage`. The AI draws on `Case.ai_brief`, user reactions, and semantic retrieval from document embeddings.

**Full spec: `docs/specs/07_case_chat.md`**

### 8. Financial delta — per document and cumulative

Every document that contains cost claims or rulings on costs surfaces a financial delta:

```
NEW  Beschluss Prozesskostenhilfe  Apr 02
  Financial impact: +450 EUR Gerichtsgebühren
  Cumulative exposure ADV-024-A: 1.690 EUR
  Breakdown: [view →]
```

Tracked in `Document.cost_delta` and aggregated in `Case.total_cost_exposure`. No synthetic probability or prediction — just factual cost tracking.

**Full spec: `docs/specs/08_financials.md`**

---

## Implementation Roadmap

**All phases complete as of April 2026.** See the Implementation Status section above for the feature matrix and deviations. Individual phase notes below are preserved as a record of what was planned.

### Phase 1 — Data foundation | Status: Implemented ✅
- Add `Proceeding`, `DocumentRelationship`, `IngestBatch`, `ActionItem` tables
- Add `Claim`, `ClaimEvidence`, `UserReaction` tables
- Add `Conversation`, `ConversationMessage` tables
- Add new `Document` fields: `role`, `court_relay`, `attributed_originator`, `document_type`, `significance_tier`, `thread_open`, `ingest_batch_id`, `proceeding_id`, `key_passages`, `cost_delta`
- Add `Case.ai_brief`, `Case.parties`, `Case.status`, `Case.total_cost_exposure`
- Alembic migrations; update services and repositories

### Phase 2 — Triage redesign | Status: Implemented ✅
- Bundle-aware triage list: group by `ingest_batch_id`, show parent-child tree
- Document HUD layout: AI-highlighted text view (left) + focused metadata form (right)
- Reaction Bar: 🚩 / ✅ / 🔍 / ⚖️ stored as `UserReaction`; free-text note field
- Confidence-aware form: unverified fields highlighted; batch confirm cascades to bundle
- AI presents claims identified in document, financial delta, relationship suggestions
- Action items surface inline from cover letter deadline extraction

### Phase 3 — Email ingest pipeline | Status: Implemented ✅
- EML parser: extract subject, sender, all attachments → create `IngestBatch`
- AI step on cover letter: detect `court_relay`, `attributed_originator` for enclosed docs, `attaches_as_proof` flags, deadlines → `ActionItem` records
- Auto-detect proceeding from court name and file number in cover letter

### Phase 4 — Document intelligence | Status: Implemented ✅
- AI step at ingest: extract `key_passages`, assign `significance_tier`, compute `cost_delta`
- Identify which prior documents this responds to / references → `DocumentRelationship` records
- Extract factual claims → `Claim` records linked to source passages
- Thread-open detection: document with no follow-up after N days flagged

### Phase 5 — Case AI intelligence | Status: Implemented ✅
- On document ingest: update `Case.ai_brief` (existing brief + new document + user reactions)
- Extract delta: significance to case, new action items, cost impact, claim updates
- Case Clock: populate typical duration ranges per proceeding type + court
- Case dashboard: graph + brief panel + action items + financial exposure

### Phase 6 — Truth Map | Status: Implemented ✅
- `Claim` / `ClaimEvidence` view: contested claims per case, evidence chain per claim
- Link user reactions from triage to relevant claims
- Claim status lifecycle (asserted → contested → refuted / established)

### Phase 7 — AI Chat | Status: Implemented ✅
- Document chat: key passages + document content as context, Ollama streaming (SSE)
- Case chat: `ai_brief` + user reactions + semantic retrieval from embeddings
- Every answer cites source documents; conversation persisted
- UI: sliding drawer on document HUD (see [`docs/specs/04_document_hud.md`](04_document_hud.md) §8h) and case dashboard

### Phase 8 — Correspondence graph | Status: Implemented ✅
- Swim-lane SVG renderer (D3.js or custom SVG)
- Proceeding switcher; graph scoped per proceeding
- N:N edges from `DocumentRelationship` table; multi-parent convergence rendering
- Court relay bundles collapsed; `attaches_as_proof` as citation badges on nodes
- Reaction indicators on nodes (from `UserReaction`)
- Significance filter toggle (critical / significant+ / all)
- Click node → document HUD slide-in (see [`docs/specs/04_document_hud.md`](04_document_hud.md))

---

## Key Design Principles

1. **Email is the source of truth** — the ingest batch is the atom of delivery, not the individual document
2. **Court is infrastructure** — relay cover letters collapsed in the graph; true sender always shown
3. **Proceedings scope everything** — documents, graphs, and AI context are scoped per proceeding; cross-proceeding references are visually distinct
4. **Relationships are typed and N:N** — a document can reply to multiple prior docs; proof attachments are citations, not independent graph actors
5. **Significance filters the noise** — 900 letters become ~150 visible nodes at default; everything else accessible but collapsed
6. **Triage is a strategy session** — user reactions are first-class data, not metadata; the AI remembers them
7. **Documents are evidence** — you navigate by graph and claim, not by file list; documents surface in context
8. **AI earns trust incrementally** — regex fills fields first, AI refines, human confirms ambiguous ones only; reactions correct the AI over time
9. **The brief is always current** — every new document triggers a brief update, not a full re-analysis from scratch
10. **Every AI answer is grounded** — all responses cite the source document and passage; no unsourced claims
11. **No magic numbers** — financial deltas are factual; Case Clock shows ranges with rationale; no synthetic probabilities
