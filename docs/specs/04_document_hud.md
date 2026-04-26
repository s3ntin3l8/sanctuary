# Sanctuary — Document HUD

Companion to [`docs/specs/00_vision.md`](00_vision.md) and [`docs/specs/02_dashboard.md`](02_dashboard.md). Covers the single canonical document-reading surface: slide-in overlay on the dashboard, full-screen reader at a dedicated URL, and embedded review pane on the triage page. One template, three contexts, one data flow.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

### Feature Matrix

| Feature | Status | Implementation |
|---------|--------|----------------|
| Three-context template (`_container.html`) | ✅ Implemented | `partials/hud/_container.html` |
| Full-screen reader at `/cases/:id/document/:id` | ✅ Implemented | `pages/document.html`, `api/cases.py` |
| Slide-in overlay (`context=overlay`) | ✅ Implemented | `_container.html` + case dashboard |
| Embedded triage pane (`context=embedded mode=review`) | ✅ Implemented | `_container.html` |
| Sticky top bar with backdrop-blur | ✅ Implemented | `_top_bar.html` |
| `n/N` position counter (proceeding-scoped) | ✅ Implemented | `neighbor_doc_ids` returns 4-tuple |
| Reaction pip in top bar | ✅ Implemented | `_top_bar.html` |
| `thread_open` amber glow | ✅ Implemented | `_top_bar.html` |
| Scroll-spy (`IntersectionObserver`) | ✅ Implemented | `hud.js` |
| Key passage `<mark data-passage-id>` | ✅ Implemented | `render_highlighted` |
| URL fragment deep-link `#p=<passage_id>` | ✅ Implemented | `hud.js` |
| Left gutter + margin pin cards | ✅ Implemented | `_body.html`, `_pin_card.html` |
| Pin collision resolution | ✅ Implemented | `hud.js` |
| Passages spine with `[+ pin]` | ✅ Implemented | `_passages_spine.html` |
| Grounds: inline expand + confirm/refute | ✅ Implemented | `_grounds.html` |
| Actions: status chip + confirm/dismiss | ✅ Implemented | `_actions.html` |
| Cost delta: `[promote to cost]` button | ✅ Implemented | `_cost_delta.html` |
| AI chat drawer (Phase 7) | ✅ Implemented | `_chat_drawer.html` |
| Keyboard shortcut set (`/ r 1-4 n o f ? ← →`) | ✅ Implemented | `hud.js` |

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
│   ▸ ruling     "Das Kind hat sich klar für die Mutter …"  │
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

---

## 5. Embedded layout (triage right pane)

Occupies triage right pane. Mode is set to `review`, which inserts the metadata review form and enables confirm/reject buttons on AI suggestions.

---

## 6. Top bar (full-screen only)

Includes breadcrumb, title, originator meta, and navigation controls (`n/N` counter, prev/next, open original, focus mode, close).

---

## 7. Body column — the reading surface

### 7a. Rendering
`Document.content` rendered with inline highlights for key passages. Claim-grounded passages carry a small ⚖ chip.

### 7b. Margin pinned notes (left gutter)
Pinned notes render as ~220px-wide cards in the left gutter, aligned to anchor passages with SVG leader lines.

---

## 8. Right rail — section order

1. **AI Summary:** Legal Significance / Required Action / Financial Impact.
2. **Passages Spine:** Scroll-spy list grouped by kind.
3. **Relationships:** Incoming and outgoing edges with confirm/reject in review mode.
4. **Grounds:** Claims grounded in this document.
5. **Actions:** Deadlines and court dates sourced from this document.
6. **Cost Delta:** Financial signals with promotion to the ledger.
7. **React:** Four-reaction bar (🚩/✅/🔍/⚖).
8. **Ask AI:** Scoped document chat drawer.

---

## 9. Navigation axes

- **Proceeding prev/next:** Siblings in same proceeding by date.
- **Parent / children:** Russian-doll cover-letter → enclosure relationship.
- **Bundle siblings:** Documents arriving in the same ingest batch.

---

## 10. Keyboard Shortcuts

| Key | Action |
|---|---|
| `Esc` | Close HUD / exit focus mode |
| `← / →` | Prev / next doc in proceeding |
| `[` / `]` | Parent / first child |
| `{ / }` | Prev / next bundle sibling |
| `n` | Add pin at selection or active passage |
| `r` | Focus reaction bar |
| `/` | Focus Ask AI input |
| `o` | Open original file in new tab |
| `?` | Show shortcut cheat-sheet |

---

## Related docs

- `docs/specs/00_vision.md` — North star
- `docs/specs/01_triage.md` — Intake flow
- `docs/specs/02_dashboard.md` — Dashboard integration
- `docs/specs/07_case_chat.md` — AI Chat spec
