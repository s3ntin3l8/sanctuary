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

### Primary navigation paradigm

The **correspondence graph is the primary interface** for a case — not a document list. You navigate by clicking nodes in the graph; documents surface in context, never from a list.

The information hierarchy:
```
All cases (sidebar) → Case graph + AI brief → Node click → Document HUD
```

You never open a "document list." You always arrive at a document through the graph, with full case context already present.

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

When a node is clicked, the document surfaces not as a raw PDF but as a **semantically highlighted view**:

- AI-identified key passages rendered in slate blue — the one sentence in a 50-page Schriftsatz that actually shifts something
- Supporting context visible but visually dimmed
- Claim annotations inline — "this sentence asserts Claim #12, currently contested"
- User reaction and notes visible at top
- Source citations for AI summary visible and clickable
- `[ask about this document ✦]` always accessible

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

### 8. Financial delta — per document and cumulative

Every document that contains cost claims or rulings on costs surfaces a financial delta:

```
NEW  Beschluss Prozesskostenhilfe  Apr 02
  Financial impact: +450 EUR Gerichtsgebühren
  Cumulative exposure ADV-024-A: 1.690 EUR
  Breakdown: [view →]
```

Tracked in `Document.cost_delta` and aggregated in `Case.total_cost_exposure`. No synthetic probability or prediction — just factual cost tracking.

---

## Implementation Roadmap

### Phase 1 — Data foundation
- Add `Proceeding`, `DocumentRelationship`, `IngestBatch`, `ActionItem` tables
- Add `Claim`, `ClaimEvidence`, `UserReaction` tables
- Add `Conversation`, `ConversationMessage` tables
- Add new `Document` fields: `role`, `court_relay`, `attributed_originator`, `document_type`, `significance_tier`, `thread_open`, `ingest_batch_id`, `proceeding_id`, `key_passages`, `cost_delta`
- Add `Case.ai_brief`, `Case.parties`, `Case.status`, `Case.total_cost_exposure`
- Alembic migrations; update services and repositories

### Phase 2 — Triage redesign
- Bundle-aware triage list: group by `ingest_batch_id`, show parent-child tree
- Document HUD layout: AI-highlighted text view (left) + focused metadata form (right)
- Reaction Bar: 🚩 / ✅ / 🔍 / ⚖️ stored as `UserReaction`; free-text note field
- Confidence-aware form: unverified fields highlighted; batch confirm cascades to bundle
- AI presents claims identified in document, financial delta, relationship suggestions
- Action items surface inline from cover letter deadline extraction

### Phase 3 — Email ingest pipeline
- EML parser: extract subject, sender, all attachments → create `IngestBatch`
- AI step on cover letter: detect `court_relay`, `attributed_originator` for enclosed docs, `attaches_as_proof` flags, deadlines → `ActionItem` records
- Auto-detect proceeding from court name and file number in cover letter

### Phase 4 — Document intelligence
- AI step at ingest: extract `key_passages`, assign `significance_tier`, compute `cost_delta`
- Identify which prior documents this responds to / references → `DocumentRelationship` records
- Extract factual claims → `Claim` records linked to source passages
- Thread-open detection: document with no follow-up after N days flagged

### Phase 5 — Case AI intelligence
- On document ingest: update `Case.ai_brief` (existing brief + new document + user reactions)
- Extract delta: significance to case, new action items, cost impact, claim updates
- Case Clock: populate typical duration ranges per proceeding type + court
- Case dashboard: graph + brief panel + action items + financial exposure

### Phase 6 — Truth Map
- `Claim` / `ClaimEvidence` view: contested claims per case, evidence chain per claim
- Link user reactions from triage to relevant claims
- Claim status lifecycle (asserted → contested → refuted / established)

### Phase 7 — AI Chat
- Document chat: key passages + document content as context, Ollama streaming
- Case chat: `ai_brief` + user reactions + semantic retrieval from embeddings
- Every answer cites source documents; conversation persisted
- UI: sliding panel on document HUD and case dashboard

### Phase 8 — Correspondence graph
- Swim-lane SVG renderer (D3.js or custom SVG)
- Proceeding switcher; graph scoped per proceeding
- N:N edges from `DocumentRelationship` table; multi-parent convergence rendering
- Court relay bundles collapsed; `attaches_as_proof` as citation badges on nodes
- Reaction indicators on nodes (from `UserReaction`)
- Significance filter toggle (critical / significant+ / all)
- Click node → document HUD slide-in

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
