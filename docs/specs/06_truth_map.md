# Sanctuary — Truth Map

Companion document to `docs/specs/00_vision.md` §5. Covers the contested-claims view of a case: the factual layer that sits between the correspondence graph and the strategic brief.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟡 IMPLEMENTED — three known gaps require remediation (badge, deep-link, refute button)

| Layer | Status |
|---|---|
| Schema — `Claim`, `ClaimEvidence`, `UserReaction` (migration `404c6c87d3f1`) | ✅ |
| Repositories — `ClaimRepository`, `ClaimEvidenceRepository`, `UserReactionRepository` | ✅ |
| AI claim extractor with hallucination guards + pipeline gating | ✅ |
| Celery task `extract_claims_task` + `PipelineStage.CLAIMS` | ✅ |
| Service — `ClaimService.get_truth_map` + `transition_status` | ✅ |
| API — `GET /cases/{id}/truthmap?filter=…` + `POST …/claims/{id}/status` | ✅ |
| Dashboard view tab (`view='truth'`) | ✅ |
| `partials/case_view_truthmap.html` + `components/claim_card.html` | ✅ |
| HUD Grounds rail (`partials/hud/_grounds.html`) | ✅ |
| Inline ⚖ chips on passages via `_build_passage_claim_map` | ✅ |
| Top-bar tab open-count badge | ⚠ backend OOB-swaps `#truthmap-badge` but the DOM element is absent from `top_bar.html` |
| HUD "View in Truth Map →" deep link | ⚠ uses `#truthmap` fragment — dashboard tabs are Alpine state, not URL anchors |
| HUD `[✗ refute]` button in Grounds rail | ⚠ POSTs `status=refuted` which always 422s — `refuted` is AI-owned |
| Per-claim user reactions | ❌ reactions are document-scoped only |
| Manual claim creation / edit by user | ❌ AI-only in v1 |
| Filter beyond status (type, originator, proceeding) | ❌ status-only in v1 |

### Implementation Deviations

| Feature | Vision §5 | Code | Status |
|---|---|---|---|
| Reaction surface in Truth Map | "the user's own reactions from triage" | `ClaimService.get_truth_map` batch-loads `UserReaction` per evidence document → emojis on each evidence row | ✅ Accepted |
| Strength-of-evidence display | "balance of supporting vs. contesting documents" | Role glyphs per evidence row (`✓ ⚠ ✕ 📎`) — no aggregate strength bar | Accepted — per-row evidence is more legible than an aggregated score |
| Status lifecycle ownership | asserted → contested → refuted / established | `CONTESTED`/`REFUTED` are AI-owned; only `ESTABLISHED` and back-to-`ASSERTED` are user-owned (`claim_service.py:38-43`) | Accepted — explicit AI/User boundary prevents users from misclassifying AI-detected contest |
| Truth Map location | "secondary view on a case (tab or toggle)" | View-mode tab on case dashboard (`?view=truth`) | Accepted |
| Inline passage claim annotation | "this sentence asserts Claim #12, currently contested" | ⚖ chip on the passage spine via substring match in `_build_passage_claim_map` (`hud_context.py:34-65`) | Accepted — substring match is sufficient for v1; no FK from `ClaimEvidence.excerpt` to a `passage_id` |

---

## The core shift

**Traditional legal archive:** read all documents, mentally track which assertions have been challenged, maintain your own contested-points list.

**Sanctuary Truth Map:** every significant assertion from every document is already extracted, deduplicated, and linked to the documents that support, contest, or refute it. The status lifecycle is maintained by the AI — you only step in to mark something established or reopen it.

The correspondence graph answers *who said what to whom*. The Truth Map answers *what is actually in dispute, and what evidence backs each side*. These are two separate navigation layers, deliberately separated because a claim can span multiple proceedings, multiple originators, and many documents.

---

## Layout overview

```
ADV-024-A  Musterklage GmbH vs. XY  [Truth Map active]
┌──────────────────────────────────────────────────────────────────┐
│  [Open (7)]  [Established (2)]  [Refuted (1)]  [All]            │
│                                                                  │
│  ── CONTESTED ──────────────────────────────────────────────  ●  │
│  │                                                               │
│  │  #12  "Defendant arrived at 14:30 on 2024-01-10"            │
│  │       [factual]  [Contested]  ▾ Mark Established             │
│  │                                                               │
│  │  Evidence chain:                                              │
│  │  ✓ supports   #47 Klageerwiderung Beklagter    🚩  14. Jan   │
│  │               "...trat um genau 14:30 Uhr ein..."            │
│  │  ⚠ contests   #82 Stellungnahme Jugendamt     🔍  03. Mär   │
│  │               "...erschien nicht vor 15:30 Uhr..."           │
│  │                                                               │
│  │  #31  "Child primary residence is disputed"    [legal]        │
│  │       [Contested]  ▾ Mark Established                        │
│  │                                                               │
│  ── ASSERTED ───────────────────────────────────────────────  ●  │
│  │                                                               │
│  │  #19  "Monthly costs amount to 1.240 EUR"      [factual]     │
│  │       [Asserted]  ▾ Mark Established                         │
│  │  ✓ supports   #55 Klage                             01. Feb  │
│  │                                                               │
│  ── ESTABLISHED ────────────────────────────────────────────  ●  │
│  │  #7   "AG Hamburg has jurisdiction"            [legal]        │
│  │       [Established]  ↺ Reopen as Asserted                    │
│                                                                  │
│                    ℹ 1 refuted claim  [show refuted]            │
└──────────────────────────────────────────────────────────────────┘
```

The Truth Map panel fills the main canvas area when `view='truth'` is active on the case dashboard. The left-column (AI Brief, Parties, Financials) and the Action Items strip remain visible — the Truth Map replaces only the correspondence graph area.

---

## 1. Data model

### `Claim` — `app/models/database.py:362-398`

```
id                    Integer PK
case_id               String FK→cases.id, NOT NULL
proceeding_id         Integer FK→proceedings.id, nullable
source_document_id    Integer FK→documents.id, NOT NULL

claim_text            Text, NOT NULL
claim_type            ClaimType, default=FACTUAL
status                ClaimStatus, default=ASSERTED
first_made_at         DateTime
last_updated_at       DateTime (auto-onupdate)

indexes: ix_claims_case (case_id)
         ix_claims_case_status (case_id, status)
         ix_claims_proceeding (proceeding_id)
```

### `ClaimEvidence` — `app/models/database.py:401-425`

```
id                    Integer PK
claim_id              Integer FK→claims.id, NOT NULL
document_id           Integer FK→documents.id, NOT NULL

role                  ClaimEvidenceRole, NOT NULL
excerpt               Text, nullable            ← truncated at 500 chars at write time
confidence            RelationshipConfidence, default=AI_DETECTED
ingest_date           DateTime

indexes: ix_claim_evidence_claim (claim_id)
         ix_claim_evidence_document (document_id)
```

`RelationshipConfidence` (`app/models/enums.py:182-187`) is shared with `DocumentRelationship` — `AI_DETECTED | USER_CONFIRMED | USER_CREATED`.

### `UserReaction` — `app/models/database.py:428-450`

```
id                    Integer PK
document_id           Integer FK→documents.id, NOT NULL
user_id               String, default="single_user"
reaction              UserReactionType, NOT NULL
notes                 Text, nullable
ingest_date           DateTime

indexes: ix_user_reactions_document (document_id)
         ix_user_reactions_reaction (reaction)
```

`UserReaction` is **document-scoped**, not claim-scoped or passage-scoped. Multiple reactions of different types can exist per document (one `(document_id, reaction)` pair — idempotent upsert via `UserReactionRepository.set_reaction`).

### Enums — `app/models/enums.py`

```python
class ClaimType(StrEnum):
    FACTUAL   = "factual"
    LEGAL     = "legal"
    PROCEDURAL = "procedural"

class ClaimStatus(StrEnum):
    ASSERTED   = "asserted"    # AI extracted; no known contest
    CONTESTED  = "contested"   # AI found evidence contesting it
    REFUTED    = "refuted"     # AI found direct refutation
    ESTABLISHED = "established" # User confirmed as settled

class ClaimEvidenceRole(StrEnum):
    SUPPORTS       = "supports"        # ✓
    CONTESTS       = "contests"        # ⚠
    REFUTES        = "refutes"         # ✕
    CITES_AS_PROOF = "cites_as_proof"  # 📎

class UserReactionType(StrEnum):
    LIES       = "lies"        # 🚩
    TRUE       = "true"        # ✅
    NEEDS_PROOF = "needs_proof" # 🔍
    PRECEDENT  = "precedent"   # ⚖️
```

---

## 2. Status lifecycle

The AI owns the "contested" and "refuted" states. The user owns "established" and can reopen anything to "asserted".

```
                ┌─────────── AI: CONTESTS evidence ────────────┐
                ▼                                              │
  [ASSERTED] ──────────── AI: REFUTES evidence ──────────► [REFUTED]
      │  ▲                                                     │
      │  └── User: "↺ Reopen as Asserted" ──────────────────── ┘
      │
      └── User: "✓ Mark Established" ──────────────► [ESTABLISHED]
                                                           │
                                                           └── User: "↺ Reopen as Asserted"
```

| Transition | Who | HTTP | Error if violated |
|---|---|---|---|
| `ASSERTED` → `CONTESTED` | AI (on new CONTESTS evidence) | — | — |
| any → `REFUTED` | AI (on REFUTES evidence) | — | — |
| `ASSERTED` or `CONTESTED` → `ESTABLISHED` | User | `POST …/claims/{id}/status` | — |
| `ESTABLISHED` or `REFUTED` → `ASSERTED` | User | `POST …/claims/{id}/status` | — |
| any → `CONTESTED` or `REFUTED` | **Forbidden to user** | 422 `"AI-owned: …"` | AI set this; revert via your own filing |

Cross-case mismatch: 404. Wrong target for current status: 422 `"Cannot transition from X to Y"`.

Source: `app/services/claim_service.py:38-43, 144-161`.

---

## 3. AI extraction pipeline

Source: `app/services/intelligence/claim_extractor.py`, `app/tasks/extract_claims.py`.

**Eligibility gate:** only `CRITICAL` and `SIGNIFICANT` documents run through the extractor (`ELIGIBLE_TIERS = {CRITICAL, SIGNIFICANT}` at `claim_extractor.py:25`). `INFORMATIONAL` and `ADMINISTRATIVE` documents are marked `skipped` with reason `ineligible_tier:<tier>`.

**Pipeline gate:** the Celery task `extract_claims_task(doc_id)` only runs after `pipeline_stages.enrich.status == "completed"` and `doc.ai_summary_created_at` is set. Otherwise the task marks `triage_pending` or `enrich_not_completed`.

**AI prompt** (`app/services/intelligence/prompts.py:67-95`):

```
Input:
  1. Document title, summary, content preview
  2. Up to 20 open existing claims in this case (ASSERTED + CONTESTED)

Output:
  {
    "new_claims":    [{"claim_text", "claim_type", "excerpt"}],
    "evidence_links": [{"claim_id", "role", "excerpt"}]
  }
```

- Each `new_claim` must be atomic — one subject, one predicate. Compound sentences must be split.
- `claim_type` and `role` must be exactly from the whitelists; unknown values are silently dropped (hallucination guard).
- Only `claim_id`s from the provided list are accepted; invented IDs are dropped.

**Post-extraction write path:**
1. New claims: `Claim` row created with `status=ASSERTED`; source document auto-linked as `ClaimEvidence.SUPPORTS`.
2. Evidence links: `ClaimEvidence` row created; if `role=CONTESTS` and target `status=ASSERTED`, claim flipped to `CONTESTED`; if `role=REFUTES`, always set to `REFUTED`.
3. After claim extraction: `_trigger_case_brief(doc_id)` fires — claim context feeds the next brief refresh.

---

## 4. Service layer

### `ClaimService.get_truth_map(case_id, filter_)` — `app/services/claim_service.py:79-142`

- Joinedloads `Claim.evidence` + `ClaimEvidence.document`
- Batch-loads reactions for all evidence documents in one query (avoids N+1)
- Sorts evidence per claim by `issued_date or ingest_date` ascending (chronological evidence chain)
- Filters to requested statuses via `_FILTER_STATUSES`; groups by `_GROUP_ORDER`; skips empty groups
- Always computes `open_claim_count` (ASSERTED + CONTESTED) regardless of active filter

Returns `TruthMapView(case_id, filter, groups: list[ClaimGroup], open_claim_count: int)`.

### Dataclasses

```python
@dataclass
class EvidenceRow:
    evidence:  ClaimEvidence
    document:  Document
    reactions: list[UserReaction]   # doc-scoped reactions on the evidence document

@dataclass
class ClaimRow:
    claim:    Claim
    evidence: list[EvidenceRow]

@dataclass
class ClaimGroup:
    status: ClaimStatus
    claims: list[ClaimRow]

@dataclass
class TruthMapView:
    case_id:          str
    filter:           TruthMapFilter    # "open" | "established" | "refuted" | "all"
    groups:           list[ClaimGroup]
    open_claim_count: int
```

---

## 5. API routes

Source: `app/api/claims.py`.

### `GET /cases/{case_id}/truthmap?filter=open|established|refuted|all`

Returns `partials/case_view_truthmap.html` partial. HTMX swap target: `#truthmap-panel` (outerHTML). Default filter: `open`. Invalid filter strings collapse to `open`.

Context: `truth_map`, `case`, `originator_colors`, `ClaimStatus`, `ClaimEvidenceRole`, `UserReactionType`.

### `POST /cases/{case_id}/claims/{claim_id}/status`

Body: `status=established|asserted` (form-encoded). Calls `ClaimService.transition_status`. On success:
1. Re-renders `components/claim_card.html` for the updated row (`hx-target="#claim-card-{id}"`)
2. **HTMX OOB swap** of `<span id="truthmap-badge">` with the updated `open_claim_count`

Both are returned in the same response body.

---

## 6. Filter chips

```
[Open (7)]  [Established (2)]  [Refuted (1)]  [All]
```

- Active filter chip styled differently; other chips use `hx-get` to swap the panel.
- `open` chip shows `open_claim_count` badge; badge is always computed regardless of active filter.
- **⚠ Known gap:** the `open_claim_count` badge only appears inside the panel header. The dashboard **top-bar Truth tab** does not yet show this count. Remediation: add `<span id="truthmap-badge">{{ truth_map.open_claim_count }}</span>` next to the `Truth` tab in `partials/dashboard/top_bar.html:80-92`; the OOB swap already targets this ID from `claims.py:104-108`.

Filter semantics:

| Filter | Statuses included | Default? |
|---|---|---|
| `open` | CONTESTED + ASSERTED | ✅ |
| `established` | ESTABLISHED only | — |
| `refuted` | REFUTED only | — |
| `all` | All four | — |

---

## 7. Group order and claim card anatomy

**Groups render in urgency-first order:** CONTESTED → ASSERTED → ESTABLISHED → REFUTED.

**Status color tokens:**

| Status | Color |
|---|---|
| CONTESTED | `bg-amber` / amber text |
| ASSERTED | `bg-outline-variant` / secondary text |
| ESTABLISHED | `bg-originator-own` / own-color text |
| REFUTED | `bg-error` / error text |

**Claim card** (`components/claim_card.html`):

```
┌────────────────────────────────────────────────────────────────┐
│  #12  "Defendant arrived at 14:30 on 2024-01-10"    [factual]  │
│       ● CONTESTED    ▾ Mark Established                        │
│                                                                │
│  ✓  doc #47  ●  Klageerwiderung Beklagter  🚩  14. Jan 2026   │
│              "...trat um genau 14:30 Uhr ein..."               │
│                                                                │
│  ⚠  doc #82  ●  Stellungnahme Jugendamt   🔍  03. Mär 2026    │
│              "...erschien nicht vor 15:30 Uhr..."              │
└────────────────────────────────────────────────────────────────┘
```

- **Status chip** — Alpine dropdown (`x-data`) showing user-allowed transitions:
  - If ASSERTED or CONTESTED: `[✓ Mark Established]`
  - If ESTABLISHED or REFUTED: `[↺ Reopen as Asserted]`
  - HTMX POST to `/cases/{case_id}/claims/{claim_id}/status`
- **Evidence rows** — one per `EvidenceRow`, ordered by document `issued_date`:
  - Role glyph: `✓` (supports) · `⚠` (contests) · `✕` (refutes) · `📎` (cites_as_proof)
  - Originator color dot matching `OriginatorType`
  - Document title + relative date of `issued_date`
  - Reaction emojis for all `UserReaction`s on that evidence document: `🚩 ✅ 🔍 ⚖️`
  - Optional `excerpt` rendered in italics dimmed

---

## 8. HUD ↔ Truth Map cross-references

Two surfaces in the Document HUD feed into or link back to the Truth Map.

### Grounds rail — `partials/hud/_grounds.html`

Shows all `Claim` rows where `source_document_id == doc.id` — the claims *originated* in this document.

**⚠ Known gap — refute button:** the Grounds rail currently shows a `[✗ refute]` button alongside `[✓ confirm]` for unfinished claims (`_grounds.html:42-62`). This POSTs `status=refuted`, which always returns 422 because `REFUTED` is AI-owned. The button must be removed; leave only `[✓ Mark Established]` for ASSERTED/CONTESTED claims.

**⚠ Known gap — deep link:** the "View in Truth Map →" link currently navigates to `/cases/{case_id}#truthmap` (`_grounds.html:67`). The dashboard uses Alpine `view` state — there is no DOM element at `#truthmap`, so the link lands on the graph view unchanged. Fix: change the link to `/cases/{case_id}?view=truth#claim-{claim.id}`. On dashboard load, `dashboard.js` should read the `?view` query param to set Alpine `view` and scroll `#claim-card-{id}` into viewport.

### Passage ⚖ chips — `partials/hud/_passages_spine.html:34-78`

`_build_passage_claim_map()` (`hud_context.py:34-65`) substring-matches `ClaimEvidence.excerpt` text against each `key_passage.text` to derive a `passage_id → claim_id` mapping. Matching passages render a `⚖ #12` chip inline. Clicking the chip navigates to the claim card in the Truth Map via the corrected deep-link above.

This is a substring match, not a FK. If passage text and excerpt diverge after editing, the chip silently disappears — the mismatch is logged but not user-visible.

---

## 9. `UserReaction` propagation from triage

1. **Captured at triage** — `TriageService.toggle_reaction(doc_id, reaction, notes)` → `UserReactionRepository.set_reaction(doc_id, reaction, notes)`. One reaction record per `(document_id, reaction_type)` pair (idempotent upsert; toggling the same reaction again deletes it).

2. **Surfaced in Truth Map** — `ClaimService.get_truth_map` batch-loads reactions for all evidence documents in the case via `UserReactionRepository.get_by_document_ids([…])`. Each `EvidenceRow.reactions` contains all reaction records for that evidence document.

3. **Not claim-scoped** — a `🚩 Lies` reaction on a document applies to the whole document, not specifically to a claim. This means the same reaction may appear on multiple evidence rows if a document provides evidence for several claims.

This design is deliberate for v1: reaction-fragmentation (tagging per claim or per passage) adds UI complexity without clear legal benefit, since the strategic read ("I think this document lies") is document-level. Claim-scoped reactions are a non-goal (see §13).

---

## 10. Claim cards within the case dashboard

The Truth Map panel is mounted at `pages/case_dashboard.html:88-92`:

```html
<div x-show="view === 'truth'" x-cloak class="h-full overflow-auto custom-scrollbar p-4">
  {% include "partials/case_view_truthmap.html" %}
</div>
```

The **Action Items strip remains visible** when Truth Map is active — it lives in a separate bottom zone and is not replaced by the view-mode tab switch. This is intentional: a Frist due in 12 days is always relevant regardless of which view you're reading.

The case dashboard builds the initial `truth_map` (filter=`open`) in `case_dashboard_service.py:138-196` at page load. Switching filter chips is a client-side HTMX swap — no full page reload.

---

## 11. Empty states

| Situation | What renders |
|---|---|
| Document under triage (not yet confirmed) | Grounds rail: "Claims can only be extracted after the document is confirmed in triage." (`claims_status = pending_triage`) |
| Document eligible but enrichment not yet complete | Grounds rail: "Claim extraction pending — the document is still being enriched." (`claims_status = pending`) |
| Extractor ran; tier is INFORMATIONAL or ADMINISTRATIVE | Grounds rail: "Claims are not extracted for informational or administrative documents." (`claims_status = skipped`) |
| Extractor ran; no claims found | Grounds rail: "No claims identified in this document." (`claims_status = ran`) |
| Truth Map panel, filter=`open`, no open claims | "No contested or asserted claims — all claims are established or refuted." |
| Truth Map panel, filter=`established`, none established | "No claims have been marked established yet." |
| Truth Map panel, filter=`refuted`, none refuted | "No claims have been refuted." |
| Claim row with zero evidence | Should not occur — source doc is always auto-linked as `SUPPORTS`. If it does, render a warning chip: "⚠ No evidence linked — report this." |

---

## 12. Keyboard-first interaction

| Key | Action | Implemented |
|---|---|---|
| `t` | Switch case dashboard to Truth Map view | ✅ `dashboard.js:375` |
| `←` / `→` | Cycle filter chips (Open → Established → Refuted → All → Open) | ❌ to implement |
| `Enter` on a claim card | Open the source document HUD for `claim.source_document_id` | ❌ to implement |
| `Esc` | Return to Graph view (same as global Esc behavior) | ✅ (global) |

---

## 13. Data sources map

| Truth Map zone | Primary source | Populated by |
|---|---|---|
| Filter chips + group headers | `ClaimService.get_truth_map` → `TruthMapView.groups` | Phase 6 (service) |
| Claim text + type | `Claim.claim_text`, `Claim.claim_type` | Phase 4 (AI extractor) |
| Claim status chip | `Claim.status` | AI + user transitions |
| Evidence rows | `ClaimEvidence` via `EvidenceRow` | Phase 4 (AI extractor) |
| Evidence role glyphs | `ClaimEvidence.role` | Phase 4 |
| Evidence document originator dot | `Document.attributed_originator` + `OriginatorType` | Phase 4 |
| Evidence date | `Document.issued_date` or `Document.ingest_date` | Phase 3/4 |
| Evidence excerpt | `ClaimEvidence.excerpt` | Phase 4 |
| Reaction emojis | `UserReaction` via `UserReactionRepository.get_by_document_ids` | Phase 2 (triage) |
| Open-count badge | `TruthMapView.open_claim_count` | Phase 6 (service) |
| HUD Grounds claims | `hud_context.py:build_hud_context` `grounds` | Phase 6 |
| HUD ⚖ passage chips | `hud_context.py:_build_passage_claim_map` | Phase 6 |

---

## 14. Files that will change

### Modified

| File | Change |
|---|---|
| `app/templates/partials/dashboard/top_bar.html:80-92` | Add `<span id="truthmap-badge">{{ truth_map.open_claim_count if truth_map.open_claim_count else '' }}</span>` next to the Truth tab; only show when count > 0 |
| `app/services/case_dashboard_service.py:138-196` | Ensure `truth_map.open_claim_count` is available in top-bar context (it already is via `truth_map` in the full context dict) |
| `app/templates/partials/hud/_grounds.html:42-62` | Remove `[✗ refute]` button; keep `[✓ Mark Established]` only for ASSERTED/CONTESTED claims |
| `app/templates/partials/hud/_grounds.html:67` | Change href from `#truthmap` to `?view=truth#claim-{{ claim.id }}` |
| `static/js/dashboard.js` | On DOMContentLoaded, read `?view` query param → set Alpine `view` data; read `#claim-{id}` hash → `scrollIntoView` after panel renders |
| `docs/specs/00_vision.md` | §5 add link: "see `06_truth_map.md` for the full spec" |
| `docs/specs/02_dashboard.md` | §9 Truth Map sub-section: link to `06_truth_map.md` |

### Deleted

None.

---

## 15. Phase progression

| Phase | What the Truth Map gains |
|---|---|
| Phase 1 | Schema (`Claim`, `ClaimEvidence`, `UserReaction`) laid down |
| Phase 2 | `UserReaction` captured at triage; reactions flow forward |
| Phase 4 | AI claim extractor runs at ingest; evidence links created; status auto-transitions |
| Phase 6 ← current | Truth Map view tab, claim cards, filter chips, status transitions, HUD Grounds rail, passage ⚖ chips |
| Phase 7 | AI Chat can answer "what did I flag as 🔍 Needs Proof?" citing `Claim` + `UserReaction` records |
| v2 | Per-claim or per-passage reactions; manual claim creation; cross-proceeding claim rollup |

---

## 16. Non-goals (v1)

- **No manual claim creation or editing.** Claims are AI-extracted. If a claim is wrong, the user marks it Established or reopens it — they cannot write claim text. Rationale: freeform text bypasses the AI's atomicity and deduplication; the first version proves the extracted claims are useful before adding authoring.
- **No per-claim or per-passage reactions.** Reactions are document-scoped. A document that "lies" is marked at document level; the individual claim it supports is implicitly cast as suspect via its evidence row.
- **No FK from `ClaimEvidence.excerpt` to a `passage_id`.** The substring match in `_build_passage_claim_map` is sufficient for v1. v2 can add a `passage_id` FK once passage stability is proven.
- **No aggregate strength bar.** The vision §5 mentions "balance of supporting vs. contesting." Implemented as individual role glyphs — the user reads the chain, not a metric.
- **No cross-case claim rollup.** Truth Map is per-case only. Cross-case claim similarity detection is a v2 research feature.
- **No claim-confidence scoring.** `ClaimEvidence.confidence` tracks AI_DETECTED vs. USER_CONFIRMED provenance. No percentage score is derived.
- **No filter beyond status.** Filter by claim type (factual/legal/procedural), by originator, or by proceeding is out of scope for v1.

---

## 17. Verification

### Manual test steps

1. `make seed && make run` → navigate to `/cases/<seeded-case>?view=truth`
   - Verify 4 filter chips visible; default `Open` selected; seeded CONTESTED claim visible.
2. Click `[▾ Mark Established]` on a CONTESTED claim
   - Claim moves to Established group; CONTESTED group disappears if empty; `Open (N)` badge decrements.
3. Open document HUD for a document that has claims in the Grounds rail
   - Verify `[✗ refute]` button is **not** present (after patch).
   - Verify `[✓ Mark Established]` is present for ASSERTED/CONTESTED claims.
4. Click "View in Truth Map →" from HUD Grounds
   - Dashboard switches to Truth Map tab; target claim card is scrolled into viewport.
5. `?view=truth` in the URL (direct navigate) → Truth Map is active immediately without clicking the tab.
6. Top-bar Truth tab shows open-count badge matching the panel's `Open (N)` chip.

### Automated coverage

| Test file | What it covers |
|---|---|
| `tests/unit/test_claim_service.py` | `get_truth_map` filters, group order, evidence loading, reactions, open_claim_count, cross-case isolation; `transition_status` allowed/forbidden |
| `tests/integration/test_truthmap_route.py` | Full HTTP — GET filter variants, 404, POST status transitions, 422 for AI-owned, cross-case 404; "Truth Map" in dashboard HTML |
| `tests/unit/test_intelligence_claim_extractor.py` | Extractor logic, status transitions, hallucination guards |
| `tests/integration/test_claim_deletion.py` | Cascade delete from `DocumentService.delete_document` → `Claim` → `ClaimEvidence` |
| `tests/unit/test_hud_context.py` | `grounds` aggregation, `claims_status` derivation |

**Add after remediation:**
- Integration test: `#truthmap-badge` present in `case_dashboard.html` response HTML with correct count.
- Integration test: `[✗ refute]` button absent from `_grounds.html` response after patch.
- Integration test: `?view=truth#claim-{id}` link present in `_grounds.html` "View in Truth Map" anchor.

---

## 18. Success criteria

- Filter chip swap: panel re-renders in < 200 ms on localhost (HTMX outerHTML swap of pre-built HTML).
- Top-bar badge: count matches `open_claim_count` from the panel after every `POST .../status` transition (OOB swap keeps them in sync).
- Status 422 for AI-owned transitions is the only error path reachable from normal UI (refute button removed; no other UI path sends REFUTED).
- "View in Truth Map →" from HUD: lands on Truth Map tab with the target claim card visible in viewport without manual scrolling.
- Evidence rows are ordered chronologically by document date across all claim cards.
- Extractor skip reasons are readable in the HUD Grounds rail for every ineligible-tier document.
- All seeded claims survive a full `make seed` re-seed without FK constraint errors.

---

*Related: `docs/specs/00_vision.md` §5 — Truth Map north star · `docs/specs/02_dashboard.md` §9 — view mode context · `docs/specs/04_document_hud.md` §8d — Grounds rail*
