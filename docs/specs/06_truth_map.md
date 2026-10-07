# Sanctuary — Truth Map

Companion document to `docs/specs/00_vision.md` §5. Covers the contested-claims view of a case: the factual layer that sits between the correspondence graph and the strategic brief.

---

## Implementation Status

**Last Updated:** October 6, 2026
**Status:** 🟢 IMPLEMENTED — `GET /api/v1/cases/{id}/truthmap`, `/api/v1/claims/*` (`app/api/v1/claims.py`) and `frontend/src/features/cases/dashboard/TruthMapTab.tsx`.

| Layer | Status |
|---|---|
| Schema — `Claim`, `ClaimEvidence`, `UserReaction` (migration `404c6c87d3f1`) | ✅ |
| Repositories — `ClaimRepository`, `ClaimEvidenceRepository`, `UserReactionRepository` | ✅ |
| AI claim extractor with hallucination guards + pipeline gating | ✅ |
| Celery task `extract_claims_task` + `PipelineStage.CLAIMS` | ✅ |
| Service — `ClaimService.get_truth_map` + `transition_status` | ✅ |
| API — `GET /api/v1/cases/{id}/truthmap?filter=…` + `PUT /api/v1/claims/{id}/status` (`app/api/v1/claims.py`, schemas in `app/schemas/case_detail.py`) | ✅ |
| Dashboard view tab (`?view=truth`, `frontend/src/features/cases/dashboard/CasePage.tsx`) | ✅ |
| `TruthMapTab` + `ClaimCard` (`frontend/src/features/cases/dashboard/TruthMapTab.tsx`) via `useTruthMap` / `useClaimStatus` (`frontend/src/api/caseDetail.ts`) | ✅ |
| HUD Grounds rail (`Grounds` in `frontend/src/features/documents/DocumentReview.tsx`; `grounds` + `claims_status` on `GET /api/v1/documents/{id}/review`) | ✅ |
| Inline ⚖ chips on passages via `_build_passage_claim_map` → `key_passages[].claim_id` | ✅ |
| Top-bar tab open-count badge (`CaseDetail.open_claim_count` on the Truth map tab, `CasePage.tsx`) | ✅ |
| HUD "View in Truth Map →" deep link | — (not in the SPA) |
| HUD `[✓ confirm]` button in Grounds rail | — (not in the SPA; the rail is read-only, transitions live on the Truth Map claim card) |
| Per-claim user reactions | ❌ reactions are document-scoped only |
| Manual claim creation / edit by user | ❌ AI-only in v1 |
| Filter beyond status (type, originator, proceeding) | ❌ status-only in v1 |

### Implementation Deviations

| Feature | Vision §5 | Code | Status |
|---|---|---|---|
| Reaction surface in Truth Map | "the user's own reactions from triage" | `ClaimService.get_truth_map` batch-loads `UserReaction` per evidence document → emojis on each evidence row | ✅ Accepted |
| Strength-of-evidence display | "balance of supporting vs. contesting documents" | Role glyphs per evidence row (`✓ ⚠ ✕ 📎`) — no aggregate strength bar | Accepted — per-row evidence is more legible than an aggregated score |
| Status lifecycle ownership | asserted → contested → refuted / established | `CONTESTED`/`REFUTED` are AI-owned; only `ESTABLISHED` and back-to-`ASSERTED` are user-owned (`_USER_ALLOWED` in `app/services/claim_service.py`) | Accepted — explicit AI/User boundary prevents users from misclassifying AI-detected contest |
| Truth Map location | "secondary view on a case (tab or toggle)" | View-mode tab on case dashboard (`?view=truth`) | Accepted |
| Inline passage claim annotation | "this sentence asserts Claim #12, currently contested" | ⚖ chip on the passage spine via substring match in `_build_passage_claim_map` (`app/services/hud_context.py`) | Accepted — substring match is sufficient for v1; no FK from `ClaimEvidence.excerpt` to a `passage_id` |

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

The Truth Map panel fills the main canvas area when `?view=truth` is active on the case dashboard. The brief rail (AI Brief, Deadlines, Parties, Financials) remains visible — the Truth Map replaces only the correspondence graph area.

---

## 1. Data model

### `Claim` — `app/models/database.py`

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

### `ClaimEvidence` — `app/models/database.py`

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

`RelationshipConfidence` (`app/models/enums.py`) is shared with `DocumentRelationship` — `AI_DETECTED | USER_CONFIRMED | USER_CREATED`.

### `UserReaction` — `app/models/database.py`

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
    FACTUAL = "factual"
    LEGAL = "legal"
    PROCEDURAL = "procedural"


class ClaimStatus(StrEnum):
    ASSERTED = "asserted"  # AI extracted; no known contest
    CONTESTED = "contested"  # AI found evidence contesting it
    REFUTED = "refuted"  # AI found direct refutation
    ESTABLISHED = "established"  # User confirmed as settled


class ClaimEvidenceRole(StrEnum):
    SUPPORTS = "supports"  # ✓
    CONTESTS = "contests"  # ⚠
    REFUTES = "refutes"  # ✕
    CITES_AS_PROOF = "cites_as_proof"  # 📎


class UserReactionType(StrEnum):
    LIES = "lies"  # 🚩
    TRUE = "true"  # ✅
    NEEDS_PROOF = "needs_proof"  # 🔍
    PRECEDENT = "precedent"  # ⚖️
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
| `ASSERTED` or `CONTESTED` → `ESTABLISHED` | User | `PUT /api/v1/claims/{id}/status` | — |
| `ESTABLISHED` or `REFUTED` → `ASSERTED` | User | `PUT /api/v1/claims/{id}/status` | — |
| any → `CONTESTED` or `REFUTED` | **Forbidden to user** | 422 `bad_transition` (`"AI-owned: …"`) | AI set this; revert via your own filing |

Claim outside the caller's cases: 404 (`require_claim_access`). Wrong target for current status: 422 `bad_transition` (`"Cannot transition from X to Y"`).

Source: `_USER_ALLOWED` and `ClaimService.transition_status` in `app/services/claim_service.py`.

---

## 3. AI extraction pipeline

Source: `app/services/intelligence/claim_extractor.py`, `app/tasks/extract_claims.py`.

**Eligibility gate:** only `CRITICAL` and `SIGNIFICANT` documents run through the extractor (`ELIGIBLE_TIERS = {CRITICAL, SIGNIFICANT}` in `claim_extractor.py`). `INFORMATIONAL` and `ADMINISTRATIVE` documents are marked `skipped` with reason `ineligible_tier:<tier>`.

**Pipeline gate:** the Celery task `extract_claims_task(doc_id)` only runs after `pipeline_stages.enrich.status == "completed"` and `doc.ai_summary_created_at` is set. Otherwise the task marks `triage_pending` or `enrich_not_completed`.

**AI prompt** (`CLAIM_EXTRACTOR_SYSTEM` in `app/services/intelligence/prompts.py`):

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

### `ClaimService.get_truth_map(case_id, filter_)` — `app/services/claim_service.py`

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
    evidence: ClaimEvidence
    document: Document
    reactions: list[UserReaction]  # doc-scoped reactions on the evidence document


@dataclass
class ClaimRow:
    claim: Claim
    evidence: list[EvidenceRow]


@dataclass
class ClaimGroup:
    status: ClaimStatus
    claims: list[ClaimRow]


@dataclass
class TruthMapView:
    case_id: str
    filter: TruthMapFilter  # "open" | "established" | "refuted" | "all"
    groups: list[ClaimGroup]
    open_claim_count: int
```

---

## 5. API routes

Source: `app/api/v1/claims.py`; response models in `app/schemas/case_detail.py`; client hooks in `frontend/src/api/caseDetail.ts`.

### `GET /api/v1/cases/{case_id}/truthmap?filter=open|established|refuted|all`

Returns `TruthMapView` JSON: `filter`, `groups[]` (`ClaimGroupView` → `ClaimView` with `allowed_transitions` and its `evidence[]`), `open_claim_count`, `pending_merges`, `pending_evidence`, `pipeline_active_doc_count`, `dedup_job`. Default filter: `open`; `filter` is a `TruthMapFilter` literal, so unknown values are rejected with 422. Fetched by `useTruthMap(caseId, filter)` and rendered by `TruthMapTab`.

### `PUT /api/v1/claims/{claim_id}/status`

Body: `{"status": …}` (`ClaimStatusUpdate`). Calls `ClaimService.transition_status` and returns the updated `ClaimView`; a forbidden transition is 422 `bad_transition`. `useClaimStatus` invalidates both the `truthmap` and the case `detail` queries on success, so the claim card and the top-bar open-count badge refetch together.

Sibling claim routes in the same module: `POST /api/v1/claims/{id}/precedent` (toggle), `DELETE /api/v1/claims/{id}` (dismiss), the merge/evidence proposal confirm/dismiss routes under `/api/v1/claims/proposals/*`, `POST /api/v1/cases/{id}/claims/proposals/merge` (batch) and `POST /api/v1/cases/{id}/claims/find-duplicates`.

---

## 6. Filter chips

```
[Open (7)]  [Established (2)]  [Refuted (1)]  [All]
```

- The active filter is local state in `TruthMapTab` (`useState<TruthMapFilter>`); clicking a chip refetches `GET /api/v1/cases/{id}/truthmap?filter=…` through `useTruthMap`.
- The panel header shows `TruthMapView.open_claim_count` ("N open"); it is always computed regardless of active filter.
- The dashboard **top-bar Truth map tab** shows the same count from `CaseDetail.open_claim_count` (`CasePage.tsx`); both refetch after every status mutation (§5).

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

**Claim card** (`ClaimCard` in `TruthMapTab.tsx`):

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

- **Status buttons** — one `mark …` button per entry in `ClaimView.allowed_transitions` (editors only), each calling `PUT /api/v1/claims/{claim_id}/status` through `useClaimStatus`:
  - If ASSERTED or CONTESTED: `[✓ Mark Established]`
  - If ESTABLISHED or REFUTED: `[↺ Reopen as Asserted]`
- **Evidence rows** — one per `EvidenceRow`, ordered by document `issued_date`:
  - Role glyph: `✓` (supports) · `⚠` (contests) · `✕` (refutes) · `📎` (cites_as_proof)
  - Originator color dot matching `OriginatorType`
  - Document title + relative date of `issued_date`
  - Reaction emojis for all `UserReaction`s on that evidence document: `🚩 ✅ 🔍 ⚖️`
  - Optional `excerpt` rendered in italics dimmed

---

## 8. HUD ↔ Truth Map cross-references

Two surfaces in the Document HUD feed into or link back to the Truth Map.

### Grounds rail — `Grounds` in `frontend/src/features/documents/DocumentReview.tsx`

Shows all `Claim` rows where `source_document_id == doc.id` — the claims *originated* in this document — from `grounds` + `claims_status` on `GET /api/v1/documents/{id}/review` (built by `build_hud_context` in `app/services/hud_context.py`). Each row is claim text + status badge + precedent marker; the rail is read-only — status transitions happen on the Truth Map claim card (§7), so no UI path can request an AI-owned status.

A "View in Truth Map →" deep link from the rail to `/cases/{case_id}?view=truth` is not in the SPA.

### Passage ⚖ chips — `Passages` in `DocumentReview.tsx`

`_build_passage_claim_map()` (`app/services/hud_context.py`) substring-matches `ClaimEvidence.excerpt` text against each `key_passage.text` to derive a `passage_id → claim_id` mapping, exposed as `key_passages[].claim_id` on the review/reader payloads. Matching passages render a `claim #12` chip in the spine (and the section header counts them as `⚖ N`); the reader's `body_html` highlights carry the same mapping via `render_highlighted`. The chip is informational — it does not link to the Truth Map.

This is a substring match, not a FK. If passage text and excerpt diverge after editing, the chip silently disappears — the mismatch is logged but not user-visible.

---

## 9. `UserReaction` propagation from triage

1. **Captured at triage / in the HUD** — `POST /api/v1/documents/{doc_id}/reactions` (`app/api/v1/documents.py`) → `UserReactionRepository.set_reaction(doc_id, reaction, notes)`. One reaction record per `(document_id, reaction_type)` pair (idempotent upsert; posting the same reaction again without `notes` deletes it).

2. **Surfaced in Truth Map** — `ClaimService.get_truth_map` batch-loads reactions for all evidence documents in the case via `UserReactionRepository.get_by_document_ids([…])`. Each `EvidenceRow.reactions` contains all reaction records for that evidence document.

3. **Not claim-scoped** — a `🚩 Lies` reaction on a document applies to the whole document, not specifically to a claim. This means the same reaction may appear on multiple evidence rows if a document provides evidence for several claims.

This design is deliberate for v1: reaction-fragmentation (tagging per claim or per passage) adds UI complexity without clear legal benefit, since the strategic read ("I think this document lies") is document-level. Claim-scoped reactions are a non-goal (see §13).

---

## 10. Claim cards within the case dashboard

`CasePage.tsx` mounts `<TruthMapTab detail={detail} />` in the main canvas when the `?view=truth` search param is active (`view === 'truth'`).

The **Deadlines list remains visible** when Truth Map is active — it lives in the brief rail (`Rail.tsx`) beside the canvas and is not replaced by the view switch. This is intentional: a Frist due in 12 days is always relevant regardless of which view you're reading.

The Truth Map is fetched lazily by `useTruthMap(caseId, filter, enabled)` the first time the tab is shown (filter=`open`); switching filter chips refetches the query — no page reload.

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
| `t` | Switch case dashboard to Truth Map view | ✅ `KEY_TO_VIEW` in `CasePage.tsx` |
| `←` / `→` | Cycle filter chips (Open → Established → Refuted → All → Open) | ❌ to implement |
| `Enter` on a claim card | Open the source document HUD for `claim.source_document_id` | ❌ to implement |
| `Esc` | Return to Graph view (same as global Esc behavior) | ❌ in the SPA `Esc` only closes the chat drawer (`CasePage.tsx`) |

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
   - Verify 4 filter chips visible; default `open` selected; seeded CONTESTED claim visible.
2. Click `mark established` on a CONTESTED claim
   - Claim moves to Established group; CONTESTED group disappears if empty; the "N open" count decrements.
3. Open the document HUD (`/document/{id}`) for a document that has claims in the Grounds rail
   - Verify the rail lists the claims with status badges and offers no status buttons.
4. `?view=truth` in the URL (direct navigate) → Truth Map is active immediately without clicking the tab.
5. Top-bar Truth map tab shows the open-count badge matching the panel's "N open" count.

### Automated coverage

| Test file | What it covers |
|---|---|
| `tests/unit/test_claim_service.py` | `get_truth_map` filters, group order, evidence loading, reactions, open_claim_count, cross-case isolation; `transition_status` allowed/forbidden |
| `tests/integration/test_v1_case_dashboard.py` | Full HTTP on `/api/v1` — `test_truth_map_and_claim_transitions` (GET truthmap shape, `allowed_transitions`, PUT status, 422 `bad_transition` for AI-owned, precedent toggle, dismiss), `test_find_duplicates_starts_a_job`, `test_viewer_share_cannot_mutate` (claim routes are read-only for viewer shares) |
| `frontend/src/features/cases/dashboard/CasePage.test.tsx` | Top-bar Truth map tab renders `open_claim_count` |
| `tests/unit/test_intelligence_claim_extractor.py` | Extractor logic, status transitions, hallucination guards |
| `tests/integration/test_claim_deletion.py` | Cascade delete from `DocumentService.delete_document` → `Claim` → `ClaimEvidence` |
| `tests/unit/test_hud_context.py` | `grounds` aggregation, `claims_status` derivation |

**Not yet covered:** `TruthMapTab` itself (filter chips, claim card buttons, grouping) has no vitest; the Grounds rail and passage `claim_id` chips in `DocumentReview.tsx` have no automated test yet.

---

## 18. Success criteria

- Filter chip change: the `useTruthMap` refetch renders in < 200 ms on localhost.
- Top-bar badge: `CaseDetail.open_claim_count` matches the panel's `TruthMapView.open_claim_count` after every `PUT /api/v1/claims/{id}/status` (the mutation invalidates both queries).
- Status 422 for AI-owned transitions is the only error path reachable from normal UI (the claim card only renders the server's `allowed_transitions`; the Grounds rail has no status buttons).
- Evidence rows are ordered chronologically by document date across all claim cards.
- Extractor skip reasons are readable in the HUD Grounds rail for every ineligible-tier document.
- All seeded claims survive a full `make seed` re-seed without FK constraint errors.

---

*Related: `docs/specs/00_vision.md` §5 — Truth Map north star · `docs/specs/02_dashboard.md` §9 — view mode context · `docs/specs/04_document_hud.md` §8d — Grounds rail*
