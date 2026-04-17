# Sanctuary — Triage Redesign (Phase 2)

Companion document to `docs/vision.md`. Covers the UI design and implementation scope for the bundle-aware Reactive Triage workflow.

---

## The core shift

**Current triage:** flat list of documents → click one → edit metadata fields. Pure data entry.

**Redesigned triage:** bundle-aware queue → one document at a time with full context present → the user's strategic reaction captured as first-class data → case assignment cascades to the whole family.

Triage stops being a filing task and becomes a **strategy session** between the user and the AI.

---

## 1. The queue (left sidebar, narrow)

Documents no longer appear as individual orphans. They group by **`ingest_batch_id`** — one email = one bundle, shown as a family:

```
EMAIL  14. Apr  anwalt@kanzlei.de   [ADV-024-A?]   5 docs ⚠
 ├─ ▤ Begleitschreiben LG Hamburg        [relay]            ✓ ready
 │   ↳ ▤ Klageerwiderung Beklagter       [opposing] ⚠       ⚙ AI processing
 │   ↳ ▤ Anlage K1 — Rechnung            [opposing, proof]  ✓ ready
 └─ ▤ Begleitschreiben LG Hamburg        [relay]            ✓ ready
     ↳ ▤ Jugendamtsbericht               [third party] ⚠    ⚠ OCR failed

EMAIL  12. Apr  anwalt@kanzlei.de   [ADV-031-B]    2 docs ⚠
 └─ ...
```

Parent-child tree shown inline (uses existing `parent_id`).

### Batch header chips

- **Case chip** — `[ADV-024-A]` shows the AI-detected or confirmed case. `?` suffix (`[ADV-024-A?]`) means AI-suggested but unconfirmed; a solid chip means user-confirmed. `[?]` means no detection yet.
- **Proceeding chip** (when multiple exist on the case) — `[AG Hamburg]` or `[OLG Beschwerde]`.
- **⚠** indicates the batch still has unconfirmed metadata on at least one child.

### Per-document role markers

- `[relay]` for court cover letters (`Document.court_relay=True`)
- `[proof]` for documents attached as evidence, not independent actors (`DocumentRelationship.relationship_type='attaches_as_proof'`)
- The `attributed_originator` chip (e.g., `[opposing]`, `[third party]`) so the true sender is visible even when the court routed it

### Per-document pipeline status

Three states, derived from `Document.ingest_status` + `Document.ai_summary_status`:

| Marker | Meaning | Source fields |
|---|---|---|
| `⚙ AI processing` | Docling done, but AI summary / extraction still running | `ingest_status=COMPLETED`, `ai_summary_status in (pending, null)` |
| `✓ ready` | All pipelines done; `key_passages`, `ai_summary`, `cost_delta` populated (or confirmed empty) | `ingest_status=COMPLETED`, `ai_summary_status in (generated, approved)` |
| `⚠ failed` | Ingestion or AI failed — offer retry | `ingest_status=FAILED` OR `ai_summary_status=failed` |

Without this, a user would see an empty Reaction/AI-Extracted block and not know if the AI failed, is still running, or just didn't find anything. The marker makes the system transparent.

Click a batch to expand and work through it.

---

## 2. The review pane (right, wide — document-first layout)

Not two equal columns. The document fills ~60% of the pane; the metadata form is a focused ~40% sidebar.

```
┌─────────────────────────────────────┬──────────────────────────────────┐
│                                      │  ⚠ NEEDS REVIEW                  │
│   Document HUD                       │  ┌────────────────────────────┐  │
│   (AI-annotated text view)           │  │ Case     [ADV-024-A  ] ✓   │  │
│                                      │  │ Proceed. [AG Hamburg ] ✓   │  │
│   — Key passages highlighted in      │  │ Sender   [___________] ⚠   │  │
│     slate blue (cited from AI        │  │ Date     [2026-03-12 ] ·   │  │
│     summary)                         │  │ Originator [● Opposing] ✓  │  │
│   — Scrollable, searchable           │  │ Role     [enclosure   ] ✓  │  │
│   — Click a highlight to jump        │  └────────────────────────────┘  │
│                                      │                                  │
│                                      │  REACTION                        │
│                                      │  🚩 Lies    ✅ True              │
│                                      │  🔍 Needs   ⚖️ Precedent         │
│                                      │  [ optional note ___________ ]   │
│                                      │                                  │
│                                      │  AI EXTRACTED                    │
│                                      │  · Claim: "Defendant absent on   │
│                                      │    2026-01-10"                   │
│                                      │  · Cost delta: +1.240 EUR        │
│                                      │  · Deadline: 2026-04-30          │
│                                      │                                  │
│                                      │  RELATIONSHIP SUGGESTIONS        │
│                                      │  · replies_to → Beschluss 03/14  │
│                                      │    [confirm] [reject]            │
│                                      │                                  │
│                                      │       [← prev]  [mark reviewed →]│
└──────────────────────────────────────┴──────────────────────────────────┘
```

### Key behaviors

**Confidence-aware form.** Fields with high `extraction_confidence` render pre-confirmed (subtle ✓, no attention drawn). Low/medium or missing values get highlighted and pull the eye — you only work on what needs work. No more scanning 8 fields when 2 matter.

**AI-annotated document view.** Replaces the raw PDF. The Docling markdown is rendered with `key_passages` (from `Document.key_passages`) highlighted in slate blue. Each highlight is clickable and scrolls the form to the corresponding field.

**The Reaction Bar.** Four tags, stored as `UserReaction` rows. Optional free-text note. No default — the user must explicitly tag, or skip. These reactions become high-weight context the AI recalls later when you ask "what did I think of the opponent's third motion?"

**AI-extracted block.** Shows what the AI pulled out of this document — claims (→ `Claim` table), cost delta (→ `Document.cost_delta`), action items (→ `ActionItem`). User can confirm (accept into the case), reject (drop), or edit before saving.

**Relationship suggestions.** The AI proposes links to other documents in the case (`replies_to`, `references`, `attaches_as_proof`). User confirms or rejects; confirmation creates `DocumentRelationship` rows with `confidence=user_confirmed`.

---

## 3. Batch-level confirm — what it does and doesn't do

At the batch header, a single **[confirm & process all →]** button.

### What the cascade does

1. **Sets `case_id` + `proceeding_id`** on all documents in the bundle (via `IngestBatchRepository.assign_case()`)
2. **Creates `ActionItem` records** for any deadlines/court dates extracted from the cover letter, linked to the source document
3. **Transitions batch status** → `completed`
4. **Recomputes `review_reasons`** for each child: any doc whose only blocker was `missing_case_id` now has an empty reasons list, which flips `needs_review=False` automatically

### What the cascade does NOT do

- It does **not** unconditionally clear `needs_review`. Documents with other unresolved reasons (missing sender, missing date, missing parent, etc.) stay in triage even after their case is assigned.
- It does not mark individual AI extractions (claims, relationships) as confirmed. Those require per-document review.

### Two levels of "confirm"

| Action | Effect |
|---|---|
| **Batch cascade** | Assigns case + proceeding, creates action items, batch → `completed`. Each child leaves triage *iff* case assignment was its only blocker. |
| **Per-document "mark reviewed"** | Explicitly clears `needs_review` on that one document. |

### The two paths

- **Fast path** (simple bundle where AI got sender/date/originator right): a single batch cascade clears the whole bundle from triage in one action.
- **Detailed path** (children are missing sender/date etc.): cascade first to set the case + proceeding, then walk each child quickly to fill gaps and mark reviewed.

---

## 4. The flow in practice

```
1. Open /triage
2. Queue shows 3 unconfirmed batches (left sidebar)
3. Click batch → first document auto-loads in review pane
4. Scan the AI-highlighted text (~5 seconds)
5. Glance at the form — only 2 fields need attention (sender, date)
6. Fix them
7. Tap 🚩 Lies or ✅ True (optional but quick)
8. Confirm 1-2 AI extractions (claims, relationships)
9. → next doc in bundle (keyboard shortcut) OR [confirm & process all]
10. Batch clears from queue
```

**Target:** a 5-doc bundle goes from "30 minutes of clicking through forms" to "3 minutes of focused review."

---

## 5. Proceedings — what are they?

A **Proceeding** is a court-level stage within a case. One case can have multiple proceedings as it escalates through the system:

```
Case ADV-024-A "Custody dispute"
├── Proceeding #1  AG Hamburg    court_level=ag   az_court="003 F 426/25"
├── Proceeding #2  OLG Hamburg   court_level=olg  az_court="12 UF 89/25"
└── Proceeding #3  BGH           court_level=bgh  az_court="XII ZB 123/26"
```

The initial case *is* its first proceeding. When a case escalates (Beschwerde to OLG, Revision to BGH), a new Proceeding row is created under the same Case. Documents arriving during each stage are scoped to that proceeding.

In triage, the AI usually detects the proceeding from the Aktenzeichen in the cover letter — e.g., `003 F 426/25` pins the document to the AG Hamburg proceeding. The user confirms per batch, and the `proceeding_id` cascades to all children.

This scoping matters downstream: the correspondence graph (Phase 7) is rendered per proceeding by default, with cross-proceeding references shown as grayed edges.

---

## 5a. Case IDs: which one is the "lead"?

A single case carries multiple identifiers in the real world:

| ID | Example | Scope | Stability |
|---|---|---|---|
| **Internal ID** (`Case.id`) | `ADV-024-A` | Your counsel | Permanent, stable across all courts |
| **Court Az** (`Proceeding.az_court`) | `003 F 426/25` | Per court level | Changes on escalation |
| **External refs** | Jugendamt ref, opposing counsel ref | Per third party | Varies |

### The lead is the internal ID

`Case.id` is the lead identifier **everywhere**: sidebar, breadcrumb, URLs, chat, cross-references, reports. Rationale:

- It's yours. Your matter stays named `ADV-024-A` whether it's pending at AG, on Beschwerde at OLG, or dormant.
- Court Az numbers are context-specific — there is no single "the court ID" for a case that has moved through three courts.
- The internal ID is stable, addressable, human-readable.

### Where the court Az belongs

On the **Proceeding**, not the Case. Each court level gets its own `Proceeding.az_court`. A document's "court reference" is `document.proceeding.az_court`, not a Case-level field.

### `Case.court_id` — removed

The legacy `court_id` column on `Case` (from before Proceedings existed) has been dropped. Per-court Aktenzeichen now lives exclusively on `Proceeding.az_court`. See migration `cc7bed04fc19_drop_case_court_id`.

### Display rules

| Surface | Primary | Secondary | Format |
|---|---|---|---|
| Sidebar / breadcrumb | Internal ID | — | `ADV-024-A` |
| Triage batch header | Internal ID | Proceeding name | `[ADV-024-A?] · AG Hamburg` |
| Document HUD | Internal ID | Proceeding + Az | `ADV-024-A · AG Hamburg · 003 F 426/25` |
| Case list row | Internal ID + title | Active proceeding | `ADV-024-A — Custody dispute · AG Hamburg` |
| URLs | Internal ID | — | `/cases/ADV-024-A` |
| AI chat answers | Internal ID | Az when quoting court docs | "The ruling [ADV-024-A, AG 003 F 426/25] sets a deadline of…" |

The Az is shown but never lead — it's context for the proceeding, not the identity of the case.

---

## 5b. What the AI assigns at ingest

Phase 4 (document intelligence) populates these fields. Phase 2 triage shows them when present, renders empty blocks with `⚙ AI processing` status when still running.

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

### Significance tiers — default behavior in triage

In the triage queue, documents sort by significance within each batch — `critical` first, `administrative` last. The user's eye lands on the high-impact document first; pure court relays can be confirmed and dismissed quickly at the end.

---

## 6. Data model — what this UI rides on

All tables already exist from Phase 1 migrations. Nothing new to migrate for the UI itself.

| Concern | Where it lives |
|---|---|
| Batch grouping | `IngestBatch` + `Document.ingest_batch_id` |
| Family tree | existing `parent_id` |
| Proceeding assignment | `Document.proceeding_id` + `Proceeding` |
| True originator | `Document.attributed_originator`, `Document.court_relay` |
| Role (cover/enclosure/standalone) | `Document.role` |
| AI-highlighted passages | `Document.key_passages` |
| Cost delta | `Document.cost_delta` |
| Reactions | `UserReaction` table |
| Extracted claims | `Claim` + `ClaimEvidence` |
| Proposed links | `DocumentRelationship` (with `confidence=ai_detected`) |
| Deadlines from cover letters | `ActionItem` |

---

## 7. Files that will change

| File | Change |
|---|---|
| `app/api/triage.py` | New bundle-grouped query; batch-level resolve endpoint |
| `app/services/triage_service.py` *(new)* | Bundle aggregation, cascade assignment logic |
| `app/templates/pages/triage.html` | Tree-based queue, document-first review layout |
| `app/templates/partials/triage_batch.html` *(new)* | Bundle row with parent-child tree |
| `app/templates/partials/triage_metadata_form.html` | Confidence-aware rendering, new fields (proceeding, role, attributed_originator) |
| `app/templates/partials/reaction_bar.html` *(new)* | 🚩/✅/🔍/⚖️ component |
| `app/templates/partials/document_hud.html` *(new)* | AI-annotated text view with key_passages highlighting |
| `app/repositories/user_reaction.py` *(new)* | Reaction CRUD |
| `app/static/js/triage.js` *(new)* | Keyboard shortcuts (→ next, ← prev, 1-4 reactions, Enter confirm) |

---

## 8. Explicit non-goals for Phase 2

- **No AI-populated claims or key_passages yet.** Those need the AI extraction pipeline (Phase 4). Phase 2 ships the *UI* and stores reactions; AI-extracted content renders empty until Phase 4 fills it.
- **No email ingest yet.** Batches are created manually (via `manual` source type) or through a seed script. Real email parsing lands in Phase 3.
- **No correspondence graph.** Still Phase 7.

Phase 2 is the UI skeleton plus the Reactive Triage feature. The rest of the intelligence fills in behind it as later phases land.

---

## 9. Keyboard-first interaction

The review pane is designed for fast keyboard-only operation:

| Key | Action |
|---|---|
| `→` | Next document in bundle |
| `←` | Previous document |
| `1` | 🚩 Lies |
| `2` | ✅ True |
| `3` | 🔍 Needs Proof |
| `4` | ⚖️ Precedent |
| `Tab` | Cycle through unconfirmed form fields |
| `Enter` | Save and advance to next document |
| `Ctrl+Enter` | Confirm & process entire batch |
| `Esc` | Dismiss overlay / cancel edit |

Mouse input remains fully supported; the keyboard shortcuts are for the power path.

---

## 10. Success criteria

Phase 2 is done when:

- A batch of 5 documents can be triaged end-to-end in under 5 minutes without leaving the keyboard
- Confirming at batch level correctly cascades case + proceeding assignment to all children
- `UserReaction` records are persisted and visible in the document detail view after the fact
- Cover letter deadlines automatically create `ActionItem` records for the whole bundle
- Confidence-low fields are visually distinct from confidence-high fields — the user's eye is drawn only to what needs attention
- The document HUD renders `key_passages` highlighted (even if the list is empty pre-Phase 4)
