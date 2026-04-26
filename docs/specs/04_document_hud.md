# Sanctuary — Document HUD

Companion to `docs/vision.md` and `docs/dashboard.md`. Covers the single canonical document-reading surface: slide-in overlay on the dashboard, full-screen reader at a dedicated URL, and embedded review pane on the triage page. One template, three contexts, one data flow.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete — all phases through Phase 8)

### Feature Matrix

| Feature | Status | Implementation |
|---------|--------|----------------|
| Three-context template (`_container.html`) | ✅ Implemented | `partials/hud/_container.html` |
| Full-screen reader at `/cases/:id/document/:id` | ✅ Implemented | `pages/document.html`, `api/cases.py` |
| Slide-in overlay (`context=overlay`) | ✅ Implemented | `_container.html` + case dashboard |
| Embedded triage pane (`context=embedded mode=review`) | ✅ Implemented | `_container.html`; triage card click passes `?context=triage` → `mode=review` |
| Sticky top bar with backdrop-blur | ✅ Implemented | `_top_bar.html`, `.hud-top-bar` in `input.css` |
| `n/N` position counter (proceeding-scoped) | ✅ Implemented | `neighbor_doc_ids` returns 4-tuple; `_top_bar.html` renders `doc_position/proceeding_total` |
| Reaction pip in top bar | ✅ Implemented | `_top_bar.html` reads `reactions[0]` |
| `thread_open` amber glow | ✅ Implemented | `_top_bar.html` conditional `border-b-2 border-amber` |
| Scroll-spy (`IntersectionObserver`) | ✅ Implemented | `hud.js:_initScrollSpy()` |
| Key passage `<mark data-passage-id>` with click-to-focus | ✅ Implemented | `render_highlighted` in `app/main.py` |
| URL fragment deep-link `#p=<passage_id>` | ✅ Implemented | `hud.js:_handleFragment()` + `focusPassage()` |
| Left gutter + margin pin cards + leader lines (SVG) | ✅ Implemented | `_body.html`, `_pin_card.html`, `hud.js:_drawLeaders()` |
| Pin collision resolution | ✅ Implemented | `hud.js:_positionPins()` pushes overlapping cards down |
| Passages spine with `[+ pin]` + `[ask AI]` per row | ✅ Implemented | `_passages_spine.html`; `[ask AI]` prefills chat drawer |
| Grounds: inline expand + confirm/refute buttons | ✅ Implemented | `_grounds.html`; POSTs to `claims.py` status endpoint |
| Actions: rel-days + status chip + confirm/dismiss | ✅ Implemented | `_actions.html`; `PATCH /action-item/:id/status` |
| Cost delta: `[promote to cost]` button | ✅ Implemented | `_cost_delta.html`; `POST /document/:id/cost-from-delta` |
| Reaction bar with `+ note` textarea | ✅ Implemented | `_reactions.html`; notes rendered + form for new notes |
| AI chat drawer (Phase 7) | ✅ Implemented | `_chat_drawer.html`, `api/chat.py`; passage-prefill via `hud-prefill-chat` event |
| Keyboard shortcut set (`/ r 1-4 n o f ? ← →` etc.) | ✅ Implemented | `hud.js` keydown listener + `hudReader()` methods |
| Unified reaction route | ✅ Implemented | `POST /document/:id/reaction` — all three contexts |
| `DocumentPin` table + CRUD routes | ✅ Implemented | `repositories/document_pin.py`; `POST /document/:id/pin`, `PATCH/DELETE /pin/:id` |
| Selection-aware `n` key (reads `window.getSelection()`) | ✅ Implemented | `hud.js:createPinAtActive()` |
| Nav data-attrs on embedded branch | ✅ Implemented | `_container.html` embedded div carries `data-prev/next/parent-doc-id` |

### Implementation Deviations

| Feature | Spec | Code | Status |
|---------|------|------|--------|
| Pin/annotation routes | Nested `/document/:id/pin/:pin_id` | Flat `/pin/:pin_id` (globally unique PK) | ✅ Accepted — integration tests and spec §12b updated to reflect flat shape |
| §8h Ask AI | Described as disabled stub in v1; drawer in Phase 7 | Fully implemented with streaming drawer | ✅ Promoted — Phase 7 shipped as part of v1 |
| Passage `id` format | UUIDv4 suggested | `sha1(text|kind)[:12]` (stable, deterministic across renders) | ✅ Accepted — stability holds; passage IDs are consistent |
| `ai_summary` in chat context | Spec: include in `context_builder.py` | Not currently passed to `context_builder` | ⚠ Minor gap — drawer functions without it; non-blocking for v1 |
| Citation `#p=` fragments in chat answers | Spec: AI cites `[DOC:<id>#p=<pid>]` → deep-link scroll | Chat uses `[DOC:<id>]` only; citations link to document root | ⚠ Minor gap — non-blocking for v1 |

---

## 1. The core shift

**Today:** a document opens as a flat file preview. You skim paragraphs, hunt for the paragraph that matters, and the AI's work is buried next to the text.

**Sanctuary HUD:** the AI has already read the document and marked the traps. Key passages are highlighted inline and anchored in a spine on the right. Each passage that grounds a claim carries a ⚖ chip. Your own pinned notes hang in the margin next to the line they annotate. You never open a raw PDF — you open a reading surface that is already an argument.

Reading a document is not a separate activity from working the case. Reactions, claims, cost deltas, actions, and relationships are all in reach within the same frame.

---

## 2. Three contexts, one component

The HUD renders in three contexts with **identical data and identical section order**, differing only in chrome, width, and whether unconfirmed suggestions are editable.

| Context | Width | Chrome | Mode | Entered via |
|---|---|---|---|---|
| **Slide-in** | 480px overlay on case dashboard | Floating panel, dims dashboard behind | `read` | Click graph node, action item link, chat citation, delta-banner row |
| **Full-screen** | Full viewport at `/cases/:case_id/document/:doc_id` | Sticky top bar, back-to-dashboard button | `read` | Expand from slide-in, deep link, ⌘K search |
| **Embedded (triage)** | Occupies triage right pane | No top bar; queue is the left sidebar | `review` | Click document card in triage queue |

The slide-in is for **scan** (summary + passages cards + chat button; document body is one click away). The full-screen and embedded contexts are for **read** (full document body is the hero, right rail is the spine). The `read`/`review` mode flag toggles the triage-only chrome: metadata review form, confirm/reject on AI suggestions, and cascade-aware reaction submission.

**Rule of one:** there is exactly one Jinja template tree. Orthogonal flags (`context ∈ {overlay, standalone, embedded}`, `mode ∈ {read, review}`) control rendering. No surface diverges on its own.

---

## 3. Full-screen layout (the default reader)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← ADV-024-A · AG Hamburg · 003 F 426/25        [🚩]  [prev ← 12/47 → next]   │ ← sticky top bar (56px)
│ Klageerwiderung Beklagter                                              [⇱][×] │
│ ● Opposing · Statement · 2026-03-12 · ◆◆◆ critical · thread open             │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                    │                         │
│   LEFT GUTTER       DOCUMENT BODY  (60-65%)        │   RIGHT RAIL (30-34%)   │
│   (≈80px)                                          │                         │
│                                                    │   AI SUMMARY            │
│                   # Heading from Docling           │    · Legal …           │
│                                                    │    · Action …          │
│                   Paragraph text flowing here.     │    · Finance …         │
│                   Some sentences are 🔆marked as   │   [approve] [reject]   │
│                   🔆key passages🔆 — clicking      │                         │
│                   scrolls the spine on the right   │   PASSAGES (3·⚖2·📌1)  │
│                   and flashes focus on the mark.   │   ── ruling             │
│                                                    │   ▸ "custody preference"│
│                   Claim-grounded passages carry a  │       ⚖ Claim #12       │
│                   small ⚖ chip.                    │       📌 check K2       │
│                                                    │   ── deadline           │
│   📌 "check       Pinned notes hang in the        │   ▸ "Frist 30.04"       │
│      Anlage K2"   margin and point to the passage. │   ── neutral            │
│                                                    │   ▸ "Kosten trägt…"     │
│                                                    │                         │
│                                                    │   RELATIONSHIPS (3)     │
│                                                    │   ← replies Beschluss   │
│                                                    │     03.14               │
│                                                    │   → references Anlage K1│
│                                                    │   ⊙ superseded-by …     │
│                                                    │                         │
│                                                    │   GROUNDS (2 claims)    │
│                                                    │   ⚖ #12 Defendant …    │
│                                                    │     contests · asserted │
│                                                    │                         │
│                                                    │   ACTIONS (1)           │
│                                                    │   ⚑ Apr 30 Stellungnahme│
│                                                    │                         │
│                                                    │   COST DELTA            │
│                                                    │   +1.240 € Gebühren     │
│                                                    │                         │
│                                                    │   REACT                 │
│                                                    │   🚩 ✅ 🔍 ⚖  [+ note]   │
│                                                    │                         │
│                                                    │   [ Ask about this doc ]│
│                                                    │   (opens chat drawer)   │
└────────────────────────────────────────────────────┴─────────────────────────┘
```

**Regions:**
- **Top bar** (56px, sticky) — breadcrumb, title, originator pill + significance dots + date + thread-open glow, reaction indicator, prev/next (within proceeding, from `neighbor_doc_ids`), open original, expand-to-focus, close.
- **Left gutter** (~80px) — reserved for margin pinned notes. Empty when there are none; no reserved whitespace is wasted because body max-width still holds reader-friendly line length.
- **Body column** — Docling markdown (`Document.content`) rendered with `prose prose-sm` restyled per existing HUD (see §11). Key passages rendered as `<mark data-passage-id>` with click-to-focus; claim-grounded passages carry an inline `⚖` chip.
- **Right rail** (resident, scrolls independently) — Summary, Passages spine, Relationships, Grounds, Actions, Cost Delta, React, Ask AI. Scroll-spy syncs the Passages spine to the currently-visible mark.
- **Chat drawer** (optional, right-side overlay, ~420px) — opens over the right rail; body narrows proportionally. Scoped to this document (`Conversation.scope_type=document`). Out of scope for v1 UI — button present but disabled in v1; phase gate on model/service work (vision Phase 7).

**Reader ergonomics.** Body max line length ≈ 78ch. The 60/65% is a floor; on ≥1600px viewports the rail caps at 420px absolute and body grows. On narrow viewports (<1100px) the rail collapses into a tab bar above the body (Summary / Passages / Rels / Grounds / Actions / Costs) — the HUD never horizontally scrolls.

---

## 4. Slide-in layout (the default overlay)

Width 480px, right-anchored, dims but does not hide the dashboard. No document body visible — the slide-in is **scan-primary**.

```
┌── SLIDE-IN (480px) ──────────────────────────────────────┐
│ ●  DOCUMENT HUD     [prev ←][→ next]      [⇱ open] [×]  │
│                                                           │
│  Klageerwiderung Beklagter                                │
│  ADV-024-A · AG Hamburg · 003 F 426/25                    │
│  ● Opposing · Statement · 2026-03-12                      │
│  ◆◆◆ critical · thread open                              │
├───────────────────────────────────────────────────────────┤
│  AI SUMMARY                           [approve] [reject]  │
│   · Legal significance    …                               │
│   · Required action       Stellungnahme by 30.04          │
│   · Financial impact      +1.240 EUR                      │
├───────────────────────────────────────────────────────────┤
│  KEY PASSAGES (3)  [⚖2 · 📌1]                             │
│                                                           │
│   ▸ ruling     "Das Kind hat sich klar…"                  │
│     ⚖ Claim #12    📌 "check Anlage K2"                   │
│                                                           │
│   ▸ deadline   "Frist zur Stellungnahme…"                 │
│                                                           │
│   ▸ neutral    "Die Kosten trägt…"                        │
│                 +1.240 €                                  │
│                                                           │
│   [ Open full text ⇱ ]                                    │
├───────────────────────────────────────────────────────────┤
│  REACT                                                    │
│   🚩 Lies   ✅ True   🔍 Needs Proof   ⚖ Precedent  [+ ]   │
├───────────────────────────────────────────────────────────┤
│  RELATIONSHIPS (3)                                        │
│   ← replies to Beschluss 03.14                            │
│   → references Anlage K1                                  │
│   ⊙ superseded by Nachtrag                                │
├───────────────────────────────────────────────────────────┤
│  ACTIONS (1)          ⚑ Apr 30 Stellungnahme              │
│  GROUNDS (2 claims)   ⚖ #12 · #47                         │
│  COST DELTA           +1.240 €                            │
├───────────────────────────────────────────────────────────┤
│  [ ✦ Ask about this document ]                            │
│  Esc to close · ← → navigate · ⇱ full text                │
└───────────────────────────────────────────────────────────┘
```

**Behavior:**
- Click a passage card → flashes focus, if the body is off-screen (it is, on the slide-in) it opens full-screen at `/cases/:id/document/:doc_id#p=<passage_id>` with the passage scrolled into view.
- `[Open full text ⇱]` expands to full-screen at the same URL (no fragment). Dashboard stays in browser history one step back.
- `Esc` closes and returns focus to the graph node that opened the HUD.
- `← / →` navigate prev/next within the same proceeding, using `neighbor_doc_ids` (vision requirement §UI.3).
- Sections Actions / Grounds / Cost Delta collapse to one-line summaries; click expands inline.

---

## 5. Embedded layout (triage right pane)

Same body + right-rail split as full-screen (§3), hosted inside the triage page's right pane instead of the full viewport. The triage page owns the left queue sidebar (see `docs/triage.md` §1).

Three differences from full-screen `read` mode:

1. **Top bar is suppressed.** The triage page's page-header anchors identity; the HUD's bar would double up.
2. **Metadata review section is inserted** between Summary and Passages spine in the right rail. This renders `app/templates/partials/triage_metadata_form.html` (the existing confidence-aware form with H/M/L chips, field-level confidence borders, and tab-cycle). The form is only rendered when `mode=review`.
3. **AI suggestions are editable.** Relationships, Grounds, Actions, and Cost Delta each expose per-row `[confirm]` / `[reject]` buttons when `mode=review` and the underlying row has `confidence=ai_detected`. In `read` mode the same rows render as static chips.

`mode=review` has no effect on the body column — Docling markdown renders the same way in both modes.

---

## 6. Top bar (full-screen only)

```
← ADV-024-A · AG Hamburg · 003 F 426/25    [🚩]  [12/47  ← →]  [o] [⇱] [×]
Klageerwiderung Beklagter
● Opposing · Statement · 2026-03-12 · ◆◆◆ critical · thread open
```

**Left — identity (breadcrumb):**
- Back-arrow → returns to `/cases/:case_id` with the graph node re-selected (history state preserves scroll + active proceeding).
- Internal `Case.id` · `Proceeding.court_name` · `Proceeding.az_court` (clickable, opens the case dashboard switched to that proceeding).
- Title (`Document.title`) on line 2 as `text-lg font-black tracking-tighter`.
- Meta row: originator pill (`attributed_originator` or `originator_type` with color from `app/constants.py::ORIGINATOR_COLORS`), significance dots (3/2/1/0 from `components/significance_indicator.html`), `received_date` (`font-mono text-[9px]`), `thread_open` glow.

**Center — navigation:**
- Reaction indicator (current `UserReaction` emoji) if present, in originator-own/opposing/amber/primary depending on reaction type.
- `n/N` counter + prev/next buttons using `neighbor_doc_ids` (proceeding-scoped). Arrow keys mirror.
- `[o]` opens the original file in a new tab (served by a new `/document/:id/original` route — see §12).
- `[⇱]` toggles focus mode (hide the right rail; body stretches full-width, margin notes remain). Re-press or `Esc` restores.
- `[×]` closes → back to dashboard.

**Sticky behavior:** top bar is `position: sticky; top: 0; z-20`, backdrop-blurred, `bg-surface-container-low/80` to match `partials/page_header.html`.

---

## 7. Body column — the reading surface

### 7a. Rendering

`Document.content` is Docling markdown stored in the DB. It is rendered via the existing `render_highlighted` Jinja filter (`app/main.py:361-385`) — **extended** in this spec to:

- Emit stable per-passage IDs: `<mark id="p-{passage_id}" data-passage-id="{passage_id}" data-kind="{kind}" class="hud-mark hud-mark--{kind}">`. The `passage_id` is the stable key from `Document.key_passages[i]`; see §12.
- Drop the `title=` tooltip for rationale; rationale surfaces in the spine card instead.
- Append an inline `⚖` chip when the passage has ≥1 `ClaimEvidence` row (`evidence.excerpt` matches passage text by substring + `document_id`).
- Restyle: `bg-[color:var(--color-key-passage-bg)] text-[color:var(--color-key-passage-fg)] rounded px-0.5 ring-1 ring-[color:var(--color-key-passage-ring)]`. See §11 for the new token.

Typography uses the same `prose prose-sm max-w-none` block already present in `document_triage.html:107-116` — unchanged.

### 7b. Interactions

| Event | Effect |
|---|---|
| Click a `<mark>` | Flashes a 600ms focus ring; sets the active row in the Passages spine; URL fragment updated to `#p=<passage_id>` (pushState, not reloading) |
| Hover a `<mark>` | Corresponding spine row gets `bg-surface-container-highest` highlight |
| Scroll | Scroll-spy updates the active spine row based on `IntersectionObserver` on each `<mark>` |
| Click a `⚖` chip | Slide-in opens over the right rail showing the full claim + other evidence; secondary link to Truth Map filtered to that claim |
| Shift-click a `<mark>` | Opens "ask AI about this passage" inline in the chat drawer |

Passage focus ring is a single animation:
```css
@keyframes passage-focus {
  0% { box-shadow: 0 0 0 3px var(--color-primary); }
  100% { box-shadow: 0 0 0 0 transparent; }
}
```

### 7c. Margin pinned notes (left gutter)

Pinned notes render as ~220px-wide "sticky" cards in the left gutter, horizontally aligned to their anchor passage. When the gutter can't fit (narrow viewport), notes collapse to a small `📌` hint next to the passage that opens the note in a popover.

```
  ╭─────────────────╮
  │ 📌 check Anlage │────────────  Some sentences are 🔆marked as
  │    K2 before    │              🔆key passages🔆 — clicking
  │    Apr 28       │              scrolls the spine …
  │     — you, today│
  ╰─────────────────╯
```

Card chrome: `bg-amber-50/80 dark:bg-amber-500/10 border-l-2 border-amber-500 px-3 py-2 text-[11px] text-on-surface font-medium`. Leader line (2px, `bg-amber-500/40`) points from card to passage using absolute-positioned SVG.

Pins can be added on any `<mark>` via (a) the `[+ pin]` affordance inside the spine row on hover, or (b) selecting body text and pressing `n`. Anchor persists to `DocumentAnnotation` (§12).

---

## 8. Right rail — section order and rendering

Vertical rhythm is the existing triage sidebar rhythm: each section is `px-6 py-4` with `border-b border-outline-variant/10`. Eyebrow heads use `text-[10px] font-bold text-primary uppercase tracking-widest mb-2` with a leading `material-symbols-outlined text-sm` icon.

### 8a. AI Summary

Renders `doc.ai_summary` via `summary_bullets_from_ai_summary()` (already in `app/services/case_dashboard_service.py:232-255`). Three bullets: Legal Significance / Required Action / Financial Impact. Chip colors `legal=primary`, `action=amber-500`, `finance=originator-own`. Approve/Reject controls render when `ai_summary_status=generated`; spinner/Retry on `pending`/`failed`. This is the single canonical renderer — removes the three duplicates noted in vision.md's inventory.

### 8b. Passages spine (`PASSAGES`)

A scroll-spy list built from `doc.key_passages`. One row per passage:

```
── ruling                                     ← group heading (kind)
▸ "Das Kind hat sich klar für die Mutter …"  ← excerpt (2-line clamp)
   Rationale: Zeigt klares Kindeswille.       ← rationale (dimmed)
   ⚖ Claim #12    📌 "check Anlage K2"        ← claim + note chips
```

- Grouped by `key_passage.kind` (`ruling`, `holding`, `deadline`, `finding`, `concession`, `neutral`).
- Active row (scroll-spied) gets `bg-surface-container-high/50 border-l-2 border-primary`.
- Hover reveals `[+ pin]` and `[ask AI]` micro-actions.
- Click scrolls body to the `<mark>` and flashes its focus ring.
- Count header shows totals: `PASSAGES (3 · ⚖2 · 📌1)`.

In **slide-in context** this section is rendered as full cards (not just spine rows) because there's no body column to anchor. Each card shows excerpt + rationale + claim/pin chips inline.

### 8c. Relationships

Unified renderer replacing the two divergent existing ones. For each `DocumentRelationship` row where this doc is `from_document` or `to_document`:

| Direction | Icon | Text | Example |
|---|---|---|---|
| outgoing `replies_to` | `←` | replies to `{target.title}` | ← replies to Beschluss 03.14 |
| outgoing `references` | `→` | references `{target.title}` | → references Anlage K1 |
| outgoing `attaches_as_proof` | `⫸` | attaches `{target.title}` as proof | ⫸ attaches Anlage K1 |
| outgoing `supersedes` | `⊃` | supersedes `{target.title}` | ⊃ supersedes v1 Entwurf |
| incoming reverse | same arrow flipped | is replied to by / is referenced by | |

In `read` mode rows are clickable buttons → HTMX-swap the HUD with the target doc. In `review` mode rows with `confidence=ai_detected` render `[confirm]`/`[reject]` inline — confirming upgrades to `user_confirmed`; rejecting deletes the row. Already-confirmed rows render as plain clickable (identical to `read` mode).

Empty state: `No relationships detected` (italic) in both modes. In `read` mode, a subtle `[+ link]` opens a modal to manually create one (phase 8+; stub button for v1).

### 8d. Grounds — claims grounded here

Shows `Claim` rows where `source_document_id = doc.id`. One row per claim:

```
⚖ #12  Defendant's whereabouts on 2026-01-10
        contested · asserted 03.10 · last update 03.31
        ── evidence → Anlage K1 (supports) · doc #31 (contests)
```

- Status chip: `asserted/contested/refuted/established` with existing `claim_card.html` color scheme.
- Click expands inline to show all `ClaimEvidence` for the claim; each evidence row shows role + target doc title + excerpt.
- "View in Truth Map →" link at the bottom, prefiltered to `source=this.doc.id`.
- In `review` mode, newly-extracted AI claims (whose creating `ClaimEvidence` has `confidence=ai_detected`) render with `[confirm]`/`[reject]`; rejecting drops both claim and its evidence row.

Empty state: `No claims grounded in this document yet` + faint `(AI extraction runs at ingest — Phase 4)` when status=pending.

### 8e. Actions — action items sourced here

`ActionItem` rows where `source_document_id = doc.id`. One row each:

```
⚑ Apr 30 (10d)   Stellungnahme zu Beschluss 02.04      [deadline]
·  Jun 15 (55d)   Verhandlungstermin AG Hamburg         [court date]
```

Urgency icon + due date + rel days + title + action_type chip. Clicking navigates to the case dashboard's action items section with this row highlighted. In `review` mode, AI-proposed action items (phase 4+) can be `[confirm]`/`[reject]`.

### 8f. Cost Delta

Renders `doc.cost_delta` with a large mono amount colored by direction (+debit red, -credit green), description, and `trending_up/trending_down` icon. Fallback: if no `cost_delta` but `doc.cost_candidates` exist (regex extraction from Phase 2), surface candidates with `[promote to cost]` buttons in `review` mode, static list in `read` mode.

### 8g. React

Four-reaction bar (🚩/✅/🔍/⚖) using the existing `components/reaction_button.html` macro. Single POST path: `POST/DELETE /document/:doc_id/reaction` (consolidate — the current `/triage/document/:id/reaction` path moves to `/document/:id/reaction` so all three contexts share it; triage context still fine since route is not triage-coupled). `+ note` reveals a textarea; `⌘↵/^↵` saves the note to the most-recent reaction. Reactions are doc-scoped in v1; passage-scoped reactions are v2 (see §16).

### 8h. Ask AI

v1: disabled stub button with tooltip `Document chat arrives with Phase 7`.

v2 (Phase 7): button opens a right-side drawer (~420px) using the existing `.slide-in-right` pattern, scoped to `Conversation.scope_type='document'`, with the Docling body + `ai_summary` + `key_passages` + reactions as context. Answers cite passage IDs that scroll the body.

---

## 9. Navigation axes

The HUD exposes **three navigation vectors** from the top bar and keyboard:

| Axis | Source | Default keys | Chrome |
|---|---|---|---|
| Proceeding prev/next | `neighbor_doc_ids(doc)` — siblings in same proceeding by `issued_date nullslast, id` | `← / →` | Top bar `n/N` counter + arrow buttons |
| Parent / children | `Document.parent_id` / `children` | `[ / ]` | Breadcrumb shows parent; `[` goes up; `]` enters first child |
| Bundle siblings | `Document.ingest_batch_id` → `IngestBatch.documents` | `{ / }` | Not rendered visually in v1; keyboard-only. Phase 4+ may surface a collapsed chip |

Proceeding prev/next is the primary axis (matches vision.md §UI.3). The `[ / ]` axis matters for the Russian-doll cover-letter → enclosure relationship. Bundle `{ / }` lets power users move through a 5-doc email family quickly.

Cross-proceeding references open the target doc's HUD at that proceeding's scope (switching the dashboard's active proceeding when returning).

---

## 10. Passage-level interactions — summary

Everything the spec buys at the passage level:

| Interaction | Trigger | Data backing |
|---|---|---|
| Focus + spine sync | Click `<mark>` | Stable `passage_id` on `key_passages[i]` (§12) |
| Ask AI about passage | Shift-click `<mark>` or spine `[ask AI]` | Chat drawer with passage context (Phase 7) |
| Claim anchor badge | Inline `⚖` chip | `ClaimEvidence.excerpt` matched to passage, or future `ClaimEvidence.passage_id` FK |
| Pin a note | `n` on a selected `<mark>`, or spine `[+ pin]` | `DocumentPin` table (§12) |
| Scroll-to-anchor | URL fragment `#p=<passage_id>` | Same stable `passage_id` |

Out of scope v1: passage-scoped reactions (would require `UserReaction.passage_id` — v2); passage-scoped AI re-prompt (v2); passage quote-to-clipboard with cite string (trivial; could land opportunistically).

---

## 11. Visual grammar

All colors are semantic tokens from `static/input.css`; the HUD inherits the "Quiet Sanctuary" dark-slate aesthetic and works unmodified in light mode.

**Tokens already in place** (reuse verbatim):
- Originators: `bg-originator-{court|opposing|own|third|unknown}/{10,15,20,30,40}` utilities (safelist at `static/input.css:4`)
- Surface: `bg-surface-container-low`, `bg-surface-container-high/50`, `border-outline-variant/10`
- Reactions: `bg-originator-opposing/15` (🚩), `bg-originator-own/15` (✅), `bg-amber-500/15` (🔍), `bg-primary/15` (⚖)
- Significance: `text-error` (critical) / `text-amber-500` (significant) / `text-primary` (informational) / `text-outline` (administrative), mapped via `components/significance_indicator.html`

**New tokens to add** (spec calls for these — implementation note, not deferred):
```css
/* static/input.css — light */
--color-key-passage-bg:   #e0f2fe;   /* sky-100 */
--color-key-passage-fg:   #075985;   /* sky-800 */
--color-key-passage-ring: #7dd3fc;   /* sky-300 */
--color-warning:          #b45309;   /* amber-700 — closes an existing gap; referenced but undefined today */
--color-warning-container:#fef3c7;   /* amber-100 */

/* dark */
.dark {
  --color-key-passage-bg:   rgb(2 132 199 / 0.20);
  --color-key-passage-fg:   #bae6fd;
  --color-key-passage-ring: rgb(56 189 248 / 0.35);
  --color-warning:          #fbbf24;
  --color-warning-container:rgb(251 191 36 / 0.15);
}
```

Plus define `.slide-in-right` in `static/input.css` (the `@keyframes slideRight` already exists at `static/input.css:252-255` but no class binds it):
```css
.slide-in-right { animation: slideRight .24s cubic-bezier(.2,.7,.2,1) both; }
```

**Typography.** Same micro-scale as the triage HUD: `text-[8-10px]` uppercase eyebrows, `text-[11px]` body micro, `text-sm` primary body, `text-lg` HUD title, `prose prose-sm` for Docling body (unchanged).

**Icons.** Material Symbols Outlined, sized by adjacent Tailwind class. Canonical glyphs:
- Summary → `smart_toy`
- Passages → `format_quote`
- Relationships → `account_tree`
- Grounds → `balance`
- Actions → `event`
- Cost Delta → `euro` / `trending_up` / `trending_down`
- React → `bolt`
- Ask AI → `auto_awesome` (or `sparkles` inline SVG used today)
- Pin → `push_pin`

---

## 12. Data additions

Three data-model changes land with the HUD. None break existing schemas.

### 12a. `Document.key_passages` gains a stable `id`

Today each passage is `{text, rationale, span, kind?, page?}`. Add a required `id: str` (UUIDv4 or content-hash of `text+span`) generated when passages are persisted. This is what `<mark id="p-{id}">`, scroll-spy, and URL fragments anchor on. Population path: `app/services/intelligence/document_enricher.py` (Phase 4 already writes `key_passages`) assigns an `id` at write time. Existing rows without `id` are migrated in the service at read time — a passage without `id` gets one generated on first HUD render (persisted back).

No DB migration required — `key_passages` is a JSON column.

### 12b. `DocumentPin` table (margin annotations)

The table exists as `document_pins` in the DB. Canonical model name: `DocumentPin` (see `app/models/database.py` and CLAUDE.md). The spec originally called this `DocumentAnnotation` — that name was never used in code.

```
DocumentPin:
  id              PK
  document_id     FK Document (indexed; ORM cascade delete-orphan via Document.pins)
  passage_id      str(12), NOT NULL  -- stable sha1 key from key_passages[i].id
  user_id         str, default "single_user"
  note            Text, nullable     -- free-form annotation text
  ingest_date     datetime
  updated_at      datetime, onupdate
```

Repository: `app/repositories/document_pin.py`. Routes: `POST /document/:id/pin`, `PATCH /document/:id/pin/:pin_id`, `DELETE /document/:id/pin/:pin_id`.

The `kind enum(note, pin)` and nullable `passage_id` proposed in the original spec are deferred until a second annotation type (inline body note vs. margin sticky) ships — the UI today only exercises passage-anchored margin pins (YAGNI). `Document.pins` relationship is live with `cascade="all, delete-orphan"`.

ORM cascade (`Document.pins`, `Document.reactions`, `Document.claim_evidence`) is set via SQLAlchemy `relationship(cascade="all, delete-orphan")`. Note: `PRAGMA foreign_keys=ON` is not enabled in the engine configuration, so DB-level `ON DELETE CASCADE` on the FK columns is not active — the ORM relationships and explicit pre-delete cleanup in `document_service.delete_document` ensure child rows are removed.

### 12c. Consolidate reaction route

`POST/DELETE /triage/document/:id/reaction` moves to `POST/DELETE /document/:id/reaction`. The implementation is unchanged; only the path moves. All three HUD contexts use the same endpoint. Triage page call sites updated. (The second path in `case_dashboard_hud.html` — `/api/reactions/toggle` — is currently vestigial and may never have existed; deleted in this change.)

**Not required for v1:** `AiSummaryStatus` enum conversion (nice-to-have, out of HUD scope); `Document.reactions`/`claims`/`outgoing_relationships` ORM relationships (added opportunistically when the HUD context-builder lands).

---

## 13. Keyboard shortcuts

Contextual; bound only when the HUD surface has focus. Input fields guard.

| Key | Action (read mode) | Action (review mode override) |
|---|---|---|
| `Esc` | Close slide-in / exit focus mode / return to dashboard | Same |
| `←` / `→` | Prev / next doc in proceeding | Same |
| `[` / `]` | Parent / first child | Same |
| `{` / `}` | Prev / next bundle sibling | Same |
| `↑` / `↓` | Previous / next passage in spine (scroll into view) | Same |
| `Enter` (on spine row) | Scroll body, flash focus | Same |
| `n` | Add pin at current selection (if inside a `<mark>`) or at active spine row | Same |
| `r` | Focus React bar; numbers 1-4 fire | `1-4` fire directly (triage convention preserved) |
| `/` | Focus Ask-AI input (Phase 7) | Same |
| `o` | Open original file in new tab | Same |
| `⇱` / `f` | Toggle focus mode (hide right rail) | n/a (triage pane is embedded) |
| `?` | Show shortcut cheat-sheet | Same |
| `⌘↵` / `Ctrl+↵` | Save note / confirm primary affordance in focused section | Confirm metadata form / Confirm bundle |

Cheat-sheet modal reuses the `partials/home/shortcuts_modal.html` pattern; the HUD has its own version listing the above. Shortcuts are registered via a **new** lightweight Alpine store `Alpine.store('shortcuts', {...})` (see §14) — this is the first central registry; triage and dashboard later migrate to it.

---

## 14. Architecture — files

### New

| File | Purpose |
|---|---|
| `app/templates/pages/document.html` | Full-screen page at `/cases/:case_id/document/:doc_id` |
| `app/templates/partials/hud/_container.html` | Orchestrator; dispatches context/mode to inner partials |
| `app/templates/partials/hud/_top_bar.html` | Sticky top bar (full-screen only) |
| `app/templates/partials/hud/_body.html` | Docling body with inline marks + left gutter for pins |
| `app/templates/partials/hud/_rail.html` | Right-rail container |
| `app/templates/partials/hud/_summary.html` | AI Summary section |
| `app/templates/partials/hud/_passages_spine.html` | Passages spine (read) / passage cards (slide-in) |
| `app/templates/partials/hud/_relationships.html` | Unified relationship renderer |
| `app/templates/partials/hud/_grounds.html` | Claims grounded in this document |
| `app/templates/partials/hud/_actions.html` | Action items sourced from this document |
| `app/templates/partials/hud/_cost_delta.html` | Cost delta panel |
| `app/templates/partials/hud/_reactions.html` | Reaction bar (replaces `triage_reaction_bar.html`) |
| `app/templates/partials/hud/_ask_ai.html` | Ask-AI affordance (stub in v1) |
| `app/templates/partials/hud/_pin_card.html` | Single margin-pin callout (was `_margin_pin.html` in the original spec draft) |
| `app/templates/partials/hud/_shortcuts.html` | Keyboard cheat-sheet modal |
| `app/services/hud_context.py` | `build_hud_context(db, doc, *, mode, context, cases)` — aggregates reactions, rels, grounds, actions, neighbors, pins; when `cases` provided (embedded/triage context), also adds `OriginatorType` and `is_draft_case` |
| `app/repositories/document_pin.py` | CRUD for `DocumentPin` margin annotations |
| `static/js/hud.js` | Scroll-spy, focus mode, shortcut registry, pin-editor Alpine component |

### Modified

| File | Change |
|---|---|
| `app/api/cases.py` | Keep `GET /cases/:case_id/document/:doc_id/hud` (now returns `_container.html` with `context=overlay`); add `GET /cases/:case_id/document/:doc_id` for full-screen page |
| `app/api/documents.py` | Remove `?context=triage|activity|default` forking; unify to `GET /document/:id/hud` (returns `_container.html`); add `POST/DELETE /document/:id/reaction` (moved from triage); add `GET /document/:id/original`; add `POST/PATCH/DELETE /document/:id/annotation` |
| `app/api/triage.py` | Triage page embeds the HUD via `context=embedded mode=review`; retire the old `/document/:id?context=triage` path |
| `app/services/case_dashboard_service.py` | Keep `summary_bullets_from_ai_summary`, `key_passages_for_template`, `neighbor_doc_ids`; move into `hud_context.py` or re-export |
| `app/main.py::render_highlighted` | Extended to emit `id`, `data-passage-id`, `data-kind`, claim-chip suffix; drop `title=` tooltip |
| `app/services/intelligence/document_enricher.py` | Assign stable `id` on each `key_passages[i]` at write time |
| `static/input.css` | Add `--color-key-passage-*`, `--color-warning`, `--color-warning-container` tokens; define `.slide-in-right` |
| `app/templates/pages/triage.html` | Right pane uses `partials/hud/_container.html` with `context=embedded`, `mode=review`; retire the separate pane swap target |
| `app/models/database.py` | Added `Document.pins`, `Document.reactions`, `Document.claim_evidence` relationships (`cascade="all, delete-orphan"`); `DocumentPin`, `UserReaction`, `ClaimEvidence` updated to use `back_populates` |

### Deleted (clean as you go)

| File | Replaced by |
|---|---|
| `app/templates/partials/document_triage.html` | `partials/hud/_container.html` with `context=embedded mode=review` |
| `app/templates/partials/dashboard/case_dashboard_hud.html` | `partials/hud/_container.html` with `context=overlay mode=read` |
| `app/templates/partials/document_detail.html` | Unused today — deleted outright |
| `app/templates/partials/document_activity.html` | Activity feed starts linking to full-screen HUD; partial deleted |
| `app/templates/partials/triage_reaction_bar.html` | `partials/hud/_reactions.html` |
| `app/templates/partials/triage_financial_delta.html` | `partials/hud/_cost_delta.html` |

`app/templates/partials/triage_metadata_form.html` **stays** — it's surfaced inside the HUD rail only when `mode=review`, so it remains a discrete partial, unchanged.

---

## 15. Phase progression

**Status (April 2026): all phases complete.** The HUD was built in stages; each adds capability without breaking earlier ones.

| Phase | Status | What lights up |
|---|---|---|
| **Spec lands** | ✅ Done | `docs/document_hud.md` merged. |
| **Phase 2½** (UI consolidation) | ✅ Implemented | `partials/hud/*` shipped. `/cases/:id/document/:id/hud` returns new container in `context=overlay`. `/cases/:id/document/:id` serves full-screen. Triage pane uses `context=embedded mode=review` (card click passes `?context=triage`; route maps to `mode=review`). Old partials deleted. `DocumentPin` margin annotations, leader lines, collision resolution. Scroll-spy. Unified reaction path. Claim anchors. Bundle `{`/`}` nav. Keyboard shortcut set. Top bar with `n/N` counter, reaction pip, thread-open amber glow. |
| **Phase 4** (doc intelligence) | ✅ Implemented | Key passages auto-populate with stable `sha1(text\|kind)[:12]` IDs. Grounds section filled from `Claim` rows. Cost Delta from `doc.cost_delta`. AI relationship suggestions with review-mode confirm/reject. |
| **Phase 5** (case AI brief) | ✅ Implemented | Summary section with approve/reject; Grounds claim-status transitions. |
| **Phase 6** (Truth Map) | ✅ Implemented | Grounds `⚖ chip` → Truth Map deep link. Inline expand + confirm/refute buttons within HUD grounds section. |
| **Phase 7** (chat) | ✅ Implemented | Ask AI drawer active. Passage-prefill via `[ask AI]` spine button and `hud-prefill-chat` event. `+ note` on reactions. Actions confirm/dismiss. Cost delta `[promote to cost]`. |
| **Phase 8+** (graph polish) | ✅ Implemented | HUD reachable from every graph node. Nav data-attrs on embedded branch enable `← → [` keys in triage pane. |

Sections render empty states gracefully when underlying data is absent.

---

## 16. Non-goals for v1

- **No passage-scoped reactions.** Today's reactions are document-scoped. A passage-scoped model would be a richer truth-map but it's a separable v2.
- **No manual link creation.** `[+ link]` button in Relationships section is a v2. AI-detected edges with confirm/reject is sufficient for the strategy-session loop.
- **No PDF viewer.** Original file link (`[o]`) opens raw file in a new tab; in-app PDF rendering is explicitly out of scope.
- **No document edit.** The HUD is read-only for content. Metadata edit is only in `review` mode via the existing triage form.
- **No bulk actions.** Single-doc surface. Bulk operations remain in the case dashboard / triage list.
- **No export/print.** Separate report tooling.
- **No diff view** between documents (supersedes relationships). Phase 9+ concept.

---

## 17. Success criteria

The HUD is done when:

- **One template.** `partials/document_triage.html`, `partials/dashboard/case_dashboard_hud.html`, `partials/document_detail.html`, `partials/document_activity.html` are deleted; `partials/hud/_container.html` drives all three contexts.
- **Identical section order** across slide-in / full-screen / embedded (chrome differs; section sequence does not).
- **Scroll-spy correctness.** Scrolling the full-screen body updates the active passage spine row with ≤16ms lag (one frame).
- **Click-to-focus.** Clicking any `<mark>` updates URL fragment, scrolls body, flashes focus ring, activates spine row — end-to-end in one keyframe loop.
- **Pinned notes.** Creating a pin via `n` persists to `DocumentPin` and renders as a margin callout aligned to the passage. Deleting clears the callout.
- **Unified reaction path.** All three contexts POST to the same `/document/:id/reaction` endpoint; no `/api/reactions/toggle` or `/triage/.../reaction` remains.
- **Deep linkability.** `/cases/ADV-024-A/document/47#p=abc123` opens the HUD, loads the page, scrolls to passage `abc123`, and flashes focus — without needing the user to navigate from the dashboard first.
- **Tokens.** `--color-key-passage-*`, `--color-warning`, `.slide-in-right` defined in `static/input.css`; no template references undefined tokens.
- **Empty states.** A freshly-ingested doc (no `key_passages`, no `ai_summary`, no claims) still renders the HUD without errors — each section shows its empty state.
- **Keyboard parity.** Every interactive element reachable via keyboard; shortcuts cheat-sheet (`?`) lists them all.
- **Triage parity.** Triage flow (confirm metadata → confirm bundle → advance) works identically on the new embedded HUD as on the old triage right pane.
- **Light + dark mode.** Every rendered surface is themed via semantic tokens; no hard-coded hex in new templates other than server-provided stripe colors.

---

## 18. Verification

End-to-end manual test once implemented:

1. `make run` and `make seed`; visit `/cases/ADV-024-A`.
2. Click a graph node → slide-in opens; passages listed as cards; scrolling doesn't shift the dashboard.
3. Press `[⇱]` → full-screen at `/cases/ADV-024-A/document/:id`. URL shareable.
4. Scroll the body → right-rail spine's active row moves in step.
5. Click a passage in the spine → body scrolls to that `<mark>`, ring flashes, URL fragment updates.
6. Press `n` with a passage selected → pin input appears; save → callout renders in the left gutter aligned to the passage.
7. Press `←` / `→` → moves between docs in the proceeding; `[` goes to parent; `]` to first child.
8. Press `r` then `1` → 🚩 Lies reaction persisted; icon appears in top bar.
9. Visit `/triage`, open a bundle → right pane is the same HUD shape, metadata review form inserted between Summary and Passages; relationships show confirm/reject.
10. Press `o` in full-screen → original file opens in a new tab.
11. Light-mode toggle (`⌘D`) → no visual regressions; all tokens swap.
12. Delete a passage's claim evidence → ⚖ chip disappears on next render.
13. `make test` — new tests cover: reaction consolidation route, `DocumentPin` CRUD, `hud_context` aggregation, `neighbor_doc_ids` continuity across empty `issued_date`, cascade-delete of pins/reactions/claim_evidence on document removal.

Automated coverage:
- Route tests: `GET /cases/:id/document/:id` returns 200 and includes `partials/hud/_container`.
- Repository tests: `DocumentPin` create/list/update/delete; cascade delete on document removal.
- Service tests: `hud_context.build_hud_context(...)` assembles reactions, rels in/out, prev/next, grounds, actions; bundle nav returns correct prev/next for the middle doc of a 3-doc bundle.
- Template tests: snapshot rendering for `read` and `review` modes given a fixture document.

---

*Related: [`docs/vision.md`](vision.md) — overall product vision and phase roadmap · [`docs/dashboard.md`](dashboard.md) — case dashboard HUD and graph spec · [`docs/triage.md`](triage.md) — triage inbox and bundle review flow*
