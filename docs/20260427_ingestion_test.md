# Ingestion-test review — 2026-04-27

Fresh-DB re-ingestion of two test emails after Sections 1–6 landed.
Each section: what's happening → why → file:line → recommended fix.
**No code changed until the owner approves a fix scope.**

---

## Evidence base

- `data/ai_debug/runs.jsonl` — 29 entries covering sync, enricher, batch, brief, relationships, claims, entities stages.
- Per-doc logs: `doc_1..5.log`, `batch_1.log`, `case_8372-25.log` (7 026 lines total).
- Code traced to live files; all file:line references verified against the working tree.

Key timing observations from `runs.jsonl`:

| scope | stage | duration | thinking tokens | status |
|---|---|---|---|---|
| doc 2 | sync | 60 s | — | **timeout** |
| doc 2 | sync (retry) | 135 s | 16 373 | ok |
| doc 2 | enricher | 72 s | 16 064 | ok → key_passages: [] |
| doc 4 | enricher | 181 s | **39 868** | ok → 3 passages, all offsets null |
| doc 4 | relationships | 97 s | 22 094 | ok |

---

## 1 — Document preview (gap)

**Observation.** A visible vertical gap appears at the top of the document HUD even when no key passages are pinned and no annotation gutter is needed.

**Cause.** `app/templates/partials/triage_card/_body.html:8-22` renders the pin gutter unconditionally. The container reserves margin/padding whether or not any pins exist for the visible passage range.

**Fix.** Wrap the pin gutter in `{% if doc.key_passages or doc.pins %}` (or Alpine `x-show`), collapsing the parent container's top padding when the gutter is empty. No model change.

---

## 2 — Metadata fields

### 2a — AZ displayed as expanded text input instead of a pill

**Cause.** `triage_metadata_form.html` renders `az_court` the same as title/sender — a free-text input. Per CLAUDE.md, "Aktenzeichen is context, never identity."

**Fix.** Show it as a compact monospace read-only pill (copy-on-click, edit-on-click), matching the `Case.id` pill style in the top bar. New small partial `_az_pill.html` reused in both the metadata form and the doc HUD header.

---

### 2b — Issues count conflates "real problems" with "AI awaiting confirmation"

**Cause.** `app/services/triage_service.py:83` (`needs_review_count`) bundles three categories into one badge: `low_confidence`, `null` (genuine problems), and `pending_confirmation` (AI found a value, user just needs to click ✓). A document with 5 perfectly-extracted fields in `pending_confirmation` state shows "5 issues," which reads as broken.

**Fix.** Split into two badges:
- `Needs review: N` — `low_confidence` + `null`
- `To confirm: M` — `pending_confirmation`

Gate downstream bundle actions on the first badge only.

---

### 2c — Originator dropdown renders but list is empty; Significance Tier / Document Type should stay AI-controlled

**Significance Tier and Document Type — already read-only, just needs to surface as "pre-confirmed".**
The metadata form already renders `significance_tier` and `document_type` as read-only `<dd>` rows (`triage_metadata_form.html:299-305`) — they are AI-controlled and not user-editable. The only gap is that they are not counted under the "pre-confirmed" header at the top of the review pane (`triage_metadata_form.html:170-186`). The fix is to add both fields to `field_confs` with `'high'` confidence so they appear in the `high_fields` summary line — not to introduce edit dropdowns for them.

**Originator — the dropdown is in the template but the option list is empty. Real bug.**
`triage_metadata_form.html:241-247` already renders `originator_type` as a `<select>` populated from `{% for type in OriginatorType %}`. The reason the list shows up empty is that `OriginatorType` is not in the template context.

Trace:
- The metadata form is included from `partials/hud/_rail.html:18-20` only when `mode == 'review'`.
- The HUD container is built by `app/services/hud_context.py:115` `build_hud_context()`. `OriginatorType` is only injected into the context inside the conditional at `hud_context.py:230-238`, which is gated on `cases is not None`.
- `app/api/documents.py:325` is the call site for the triage right pane:
  ```python
  ctx = build_hud_context(db, doc, mode=mode, context="embedded")
  ```
  It does **not** pass `cases=…`, so the conditional skips and `OriginatorType` stays unset → the Jinja `for` loop iterates over an undefined name → empty `<select>`.

**Fix.** Two clean options, both small:
1. (Preferred) Move the `OriginatorType` injection out of the `if cases is not None:` block in `hud_context.py:230-238` and always set it. It's a static enum, no reason to gate it on `cases`. This also fixes any other surface that needs `OriginatorType` without paying for the cases query.
2. (Alternative) Pass `cases=cases_query()` from `documents.py:325` whenever `mode == 'review'`. More verbose, requires the case list query the rail doesn't otherwise need.
---

### 2d — Attachments get `received_date = now()` instead of inheriting the email timestamp

**Cause.** `app/services/ingestion/batch_orchestrator.py:283`:
```python
received_date=datetime.now(UTC),
```
Body docs (line 236-237) correctly inherit `parsed.get("received_date")`; attachments don't.

**Fix.** One-liner:
```python
received_date=parsed.get("received_date") or datetime.now(UTC),
```
All attachments in a batch should share the email's actual received timestamp.

---

### 2e — No "Attached to email …" hint in the metadata panel

**Observation.** Nothing in the per-doc metadata surface shows which batch/email the document came from. Users switch to the case graph to infer parentage.

**Fix.** Add one line at the top of the metadata panel: `Attached to: <subject> · <received_date>` (link to the triage bundle anchor). Pulls from `doc.ingest_batch.subject` / `received_date`.

---

## 3 — Pipeline

### 3a — Duplicate retry buttons

**Cause.** Two retry affordances appear: stage-level in `_review_actions.html:4-30` and global in `_pipeline_stepper.html:85-100`. Depending on the card state, both render simultaneously.

**Fix.** Keep stage-level retry on the stepper only (it lives where the visual stage failure is). Drop the duplicate from `_review_actions.html`; that partial is for metadata-review actions only.

---

### 3b — No "retry all failed in this batch" affordance

**Observation.** When several docs in a batch fail the same stage, the user must click retry per-doc.

**Fix.** Add a "Retry all failed" button to `triage_bundle.html` (rendered only when `bundle.failed_count > 0`). POST to a new route `/api/triage/bundle/{batch_id}/retry-failed` that iterates the docs and invokes the existing per-doc retry path. No new task class.

---

### 3c — Retry button on cross-doc stages is per-doc, not per-batch

**Cause.** BATCH_ANALYSIS and RELATIONSHIPS are batch-level stages, but their retry buttons appear on each card individually. Clicking retry on one doc doesn't rerun the batch-level pass.

**Fix.** Detect cross-doc stages in the stepper and route their retry through the batch endpoint above. Optionally render these stages on the bundle header row rather than each card.

---

## 4 — Passages

### 4a — Pin button appears non-functional

**Cause.** `app/templates/partials/triage_card/_passages_spine.html:90-96` uses `@click="$dispatch('pin-passage', …)"`. The listener is defined inside the doc HUD wrapper, which may not be mounted when the spine first renders. Race condition: the button fires, no handler catches it.

**Fix.** Move the handler to a top-level Alpine store (`Alpine.store('hud', { pin(passage) { … } })`) so it's available regardless of mount order. Follows the existing `Alpine.store('triage', …)` pattern.

---

### 4b — Scrolling instability on passage selection

**Cause.** `scrollIntoView({block:'center'})` fires on every passage activation, even when the user is already viewing it, causing jarring jumps.

**Fix.** Only scroll when the activated passage is outside the visible viewport. Use `IntersectionObserver` instead of unconditional `scrollIntoView`.

---

### 4c — AI computes offsets instead of the server (biggest UX quality issue)

**Cause.** `app/services/intelligence/prompts.py:40-42` asks the model:
```
start_offset and end_offset are zero-based character positions in the document text.
If you cannot determine precise offsets, omit them (do not guess)
```
Local 9B models cannot reliably count characters. They burn their thinking budget trying, sometimes time out (doc 2: 60 s timeout), sometimes give up and return an empty `key_passages: []` (doc 2 retry), and sometimes return `null` offsets for every passage (doc 4, 39 868 thinking tokens).

`_repair_passage_offsets` (`document_enricher.py:67-123`) already does a better job locally using exact substring search + normalized (whitespace/quote-collapsed) fallback. It just needs a fuzzy third pass.

**Fix.** Drop `start_offset`/`end_offset` from the enricher prompt and schema entirely. The AI returns only `text`. Server-side, promote `_repair_passage_offsets` to the canonical source, adding a third pass: `difflib.SequenceMatcher` ratio ≥ 0.85. Expected wins:
- ~5–15k fewer thinking tokens per doc.
- No more timeouts on passage extraction.
- Offsets computed deterministically — no more "null offset" passages.

---

## 5 — Grounds / Claims

### 5a — No highlighting in the doc HUD even when a claim cites a passage

**Cause.** `app/services/hud_context.py:34-65` matches passages to claims via strict string equality on the `text` field. AI-extracted claim quotes and AI-extracted passage texts rarely match exactly (whitespace, paraphrase, partial sentence).

**Fix.** Replace equality check with the same normalized+fuzzy strategy as §4c. Ideally: prompt the claims stage to reference the passage `id` (which already exists as `KeyPassage.id`, set in `document_enricher.py:167-172`), so the match is O(1) instead of O(n·fuzzy).

---

### 5b — Only "confirm" affordance, no "reject" / "dispute"

**Cause.** `app/services/claim_service.py:40-41` only permits ASSERTED → ESTABLISHED. There's no transition for disputes, contradictions, or "needs proof."

**Fix.** Add `ClaimStatus.DISPUTED` and `ClaimStatus.NEEDS_PROOF` transitions. Third button in the claims panel ("🚩 Lies" / "🔍 Needs Proof") that sets the status. This surfaces the existing `UserReaction` plumbing (🚩/✅/🔍/⚖️) at the claim granularity.

---

### 5c — Draft (auto-created, unconfirmed) cases leak into dashboard, case graph, and case overview

**Observation.** When triage auto-creates a draft case from an email, the user sees that case in the dashboard list, in the case graph, and in the case overview — alongside ratified cases — even though they have not yet confirmed it. The "View in Truth Map" link visible from the triage card is one symptom; the broader issue is that drafts are not visually segregated anywhere they appear.

**Cause.**
- `app/services/case_service.py:306` (`get_all_cases_directory`):
  ```python
  all_cases = self.case_repo.get_all_sorted_by_date(include_drafts=True)
  ```
  Drafts are explicitly included in the directory.
- The case dashboard / graph / overview routes all read from this directory (or directly query `Case` without filtering on `is_draft`).
- `app/templates/partials/triage_card/_grounds.html:58-63` renders the truth-map link unconditionally.
- The draft-case banner that does exist (`triage_metadata_form.html:109-132`) is only shown inside the triage HUD, not in case-level views.

**Fix.** Treat draft cases as a distinct visual class everywhere they surface. Smallest change set:
1. **Dashboard / case directory** — keep drafts in the listing but render them in a separate "Pending confirmation" group at the top, with the existing confirm/reject buttons inline. Drafts should not be miscounted in `stats_by_status`.
2. **Case graph / overview** — when a user opens a draft case, render a banner identical to the one in `triage_metadata_form.html:109-132` (Confirm / Reject) above the canonical view. Don't hide the case — that's worse UX — just make it clear the user is in confirmation mode.
3. **Truth-map link** — gate `_grounds.html:58-63` with `{% if not case.is_draft and not is_triage %}`. Inside a draft, the truth map is empty by construction; the link leaks the user out of the confirmation flow.

This is bigger than the original "one-line gate" fix — it's a small UX cleanup pass for draft visibility, but each of the three steps is independent and any subset can land first.
---

## 6 — Document enricher / AI offset analysis

Full log evidence for the key passages problem:

- **Doc 2 (timeout + empty result):** First enricher attempt timed out at 60 076 ms (`runs.jsonl` line 4). Retry at 135 s succeeded but thinking trace (`doc_2.log:264-404`, ~800 tokens) shows the model agonizing over offsets, giving up, returning `key_passages: []`.

- **Doc 3 (offset mismatch):** Passage 2 had AI-supplied offsets that didn't match. Repair pass 1 (exact substring) failed because the AI included `\n\n` not present in `doc.content`. Pass 2 (normalized) rescued it.

- **Doc 4 (offset loop):** 39 868 thinking tokens — the run's largest. Thinking trace (`doc_4.log:233-690`, 190 lines) shows an infinite "Wait, one more check on Offsets" loop. Final response: 3 passages, `start_offset: null` on all three.

The enricher itself correctly handles null offsets — no data is lost. The problem is the wasted inference time and the empty-passages outcome for doc 2.

**Root cause:** see §4c. The fix is the same — remove the offset ask from the prompt.

---

## 7 — Case graph

**Observation.** Documents 3-7 from the same batch are not visually grouped under the cover/relay letter in the case graph — they render as siblings on the proceeding lane.

**Cause.** `app/services/case_graph_service.py:121-123`:
```python
def _is_bundle_header(doc) -> bool:
    return bool(doc.court_relay) and doc.role == DocumentRole.COVER_LETTER
```
The test batch's court letter has `court_relay=True` but `role=NORMAL` (not flagged as COVER_LETTER by the batch analyzer for the single-relay shape). Result: `_is_bundle_header` returns False for every doc; the bundle outline never renders; no `parent_groups` clustering.

Secondary issue: even when a header is found, the bundle outline x-coordinate is hardcoded to `court_lane_idx` (line 322, 336). A non-court bundle renders in the wrong column.

**Fix.**
1. Loosen `_is_bundle_header`: `bool(doc.court_relay) and doc.role in (COVER_LETTER, NORMAL)`. Or: batch analyzer explicitly sets a `BUNDLE_HEADER` role on the relay doc.
2. Compute bundle x-coordinate from the header doc's `attributed_originator` lane rather than the hardcoded `court_lane_idx`.
3. Ensure `batch_analyzer` writes `parent_id` on attachments even for the single-relay shape (currently skipped when there's no separate cover-letter doc — the court letter is the relay).

---

## 8 — General: parent/child only visible after manual refresh

**Observation.** After BATCH_ANALYSIS completes and `parent_id` is set, the triage card grouping (parent_groups) does not update until the user manually refreshes the page.

**Cause.** Per-card polling re-renders the single card but not the bundle wrapper that holds `parent_groups`. The full bundle re-render is triggered by `reload-bundle-{batch_id}` (`triage.py:702-704`, consumed by `triage_bundle.html:23`), but that trigger only fires when `n_done == n_total` across all pipeline stages — which may be tens of minutes after BATCH_ANALYSIS finishes.

**Fix.** Fire `reload-bundle-{batch_id}` a second time specifically when BATCH_ANALYSIS reaches a terminal state for all docs in the batch. This is the semantically correct cue: "parent_id is now set." The all-stages-done emit stays as a final consolidation pass.

Implementation: in the per-doc OOB pipeline-aggregate renderer, track `n_done_batch_analysis` separately from `n_done` and emit the reload when it hits `n_total`.

---

## 9 — Confirm-case modal: Proceeding marked optional, defaults to "None"

**Observation.** In the bundle-confirm modal (existing-case branch), the Proceeding select is labelled "(optional)", defaults to `<option value="">None</option>`, and does not pre-select the auto-detected proceeding for the chosen case. This is wrong: every case has at least one auto-created proceeding (Section 4 Fix B in the plan), so "None" never makes sense.

**Cause.**
- `app/templates/partials/triage_bundle_confirm_modal.html:124-140`:
  ```jinja
  <label … >Proceeding <span … >(optional)</span></label>
  <select name="proceeding_id" … >
      <option value="">None</option>
      {% for p in (proceedings or []) %}
      <option value="{{ p.id }}" data-case-id="{{ p.case_id }}"
              x-show="!bundleConfirm.suggested_case_id || bundleConfirm.suggested_case_id === '{{ p.case_id }}'">
          {{ p.court_name }}{% if p.az_court %} — {{ p.az_court }}{% endif %}
      </option>
      {% endfor %}
  </select>
  ```
  No `selected` attribute is set on any option; the placeholder "None" wins by default.
- The server (`app/api/triage.py:201, 234-241, 282`) accepts `proceeding_id` as `Form(None)`. When None, `confirm_bundle` is called without a proceeding override — so docs keep whatever `proceeding_id` they already had (or `None` for new cases).

**Why "None" is wrong.** Every case under our model owns ≥ 1 proceeding by construction:
- `get_or_create_case_from_reference` (`case_service.py:47`) creates a Proceeding alongside any new Case.
- The auto-triage path and `/cases/create-from-triage` both go through that helper.
- `proceeding_analyzer` may upgrade or split proceedings later, but the case never has zero.

So the modal should always be assigning the docs to a real proceeding. "None" only currently works because the cascade is silently inheriting an existing `doc.proceeding_id`, which masks the bug.

**Fix.**
1. Drop the "(optional)" label and the `<option value="">None</option>` placeholder.
2. Filter the `proceedings` list server-side to those belonging to the suggested/selected case (so the user is only choosing among that case's proceedings — typically just one).
3. Pre-select the canonical proceeding for the chosen case. Heuristic: prefer the proceeding matching the bundle's `attributed_originator` court letterhead → otherwise the case's primary proceeding (most recent / highest court level / first one). Pass it as `bundleConfirm.suggested_proceeding_id` from the server and Alpine-bind via `:value`.
4. **Create-new mode (`isNewCase=true`)** — keep the proceeding select hidden (it already is, line 125 `x-show="!isNewCase"`). The server's `get_or_create_case_from_reference` call at `triage.py:222-227` auto-creates the proceeding using `infer_court_level(court_name)`. No client-side selection needed; the comment at template line 124 already documents this. Just drop "(optional)" from the label.
5. Server-side: make `proceeding_id` required when `case_id` points at an existing case (raise 422 if missing). Keep it optional only for the create-new branch where the helper handles creation.

This converts the modal from "you can leave docs unassigned to any proceeding" (which silently breaks downstream graph and timeline rendering) to "you must place the docs under a proceeding — and we've already picked the right one for you."

---

## Recommended fix sequencing

Ordered smallest blast-radius → largest; each block is mechanically independent:

| # | Fix | Files | Scope |
|---|---|---|---|
| 1 | Attachment `received_date` inheritance | `batch_orchestrator.py:283` | one-liner |
| 2 | Always inject `OriginatorType` into HUD context (fix empty originator dropdown) | `hud_context.py:230-238` | one-liner |
| 3 | needs_review_count split + add `significance_tier`/`document_type` to pre-confirmed line | `triage_service.py`, `triage_metadata_form.html` | small |
| 4 | Drop offset ask + fuzzy fallback in `_repair_passage_offsets` | `prompts.py`, `document_enricher.py` | medium |
| 5 | Emit `reload-bundle` on BATCH_ANALYSIS terminal | `triage.py` pipeline aggregate route | small |
| 6 | Case graph bundle grouping + x-coord fix | `case_graph_service.py` | medium |
| 7 | Confirm-case modal: drop "optional", pre-select proceeding, require it for existing cases | `triage_bundle_confirm_modal.html`, `triage.py` | small |
| 8 | Draft-case visibility cleanup (dashboard grouping, banner, truth-map gate) | `case_service.py`, dashboard templates, `_grounds.html` | medium |
| 9 | Claim DISPUTED/NEEDS_PROOF transitions + UI | `claim_service.py`, claims template | medium-large |

Approve à la carte — any subset can land as its own PR.
