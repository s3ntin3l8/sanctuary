# Sanctuary — Case Dashboard

Companion document to `docs/vision.md` and `docs/triage.md`. Covers the primary post-triage view: the case dashboard as command center.

---

## The core shift

**Traditional legal DMS:** open the case → see a list of files → pick one → read it.

**Sanctuary case dashboard:** open the case → see **what the case is right now** (brief, graph, open actions, exposure) → only reach documents by clicking into the graph when you need evidence.

The dashboard is the answer to *"where does this case stand, and what do I need to do about it?"* — one screen, no navigation required for 90% of the work.

You never return to the dashboard to "find a document." You return to the dashboard to **see the situation**. Documents surface in context, never from a list.

---

## Layout overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ADV-024-A  Musterklage GmbH vs. XY        ● Active  · AG Hamburg · 12 days   │ ← top bar
│ [Proceeding: AG Hamburg ▾]  [Graph|Truth Map|Timeline|Financials]  [+ doc] [✦]│
├──────────────────────────────────────────────────────────────────────────────┤
│ ⚡ 3 new documents since your last visit — 1 action added  [review →] [x]    │ ← delta banner (when applicable)
├──────────────────┬───────────────────────────────────────────────────────────┤
│                  │  CORRESPONDENCE MAP          [critical] [significant+] [all]│
│ AI BRIEF         │                                                             │
│                  │   YOU      COURT       OPPOSING    CHILD SERVICES          │
│ Status: Active — │    │         │            │              │                 │
│ awaiting your    │    ●─────────►           │              │                 │
│ Stellungnahme to │    │     ╔═══╧════╗      │              │                 │
│ Jugendamtsberi.. │    │     ║ Begl.  ║      │              │                 │
│                  │    │ ⚑   ║ Klagewi◄──────●              │                 │
│ Key risks:       │    │     ║ JA-Rpt.◄──────────────────── ●                 │
│ · Frist Apr 30   │    │     ╚═══╤════╝      │              │                 │
│ · §91 costs      │    ●─────────►           │              │                 │
│                  │                                                             │
│ Open threads: 1  │    ℹ 2 administrative documents hidden                     │
│                  │                                                             │
│ [refresh brief]  │                                                             │
│                  │                                                             │
│ PARTIES          │                                                             │
│ ● Court          │                                                             │
│ ● Opposing       │                                                             │
│ ● Jugendamt      │                                                             │
├──────────────────┼───────────────────────────────────────────────────────────┤
│ FINANCIALS       │ ACTION ITEMS                        [open] [completed]     │
│ Total:  1.690 €  │  ⚑  Apr 30 (12d)  Stellungnahme     Beschluss 02.04       │
│ +450 € (Apr 02)  │  ·   Jun 15 (58d)  Verhandlungs-    [court date]          │
│ [breakdown →]    │      termin AG Hamburg                                     │
│                  │  ◦   typical: hearing follows Klageerwiderung by 4–8 mo    │
│                  │      → window Jul–Nov 2026 (Case Clock)                    │
└──────────────────┴───────────────────────────────────────────────────────────┘
                                                                 [✦ Ask AI]  ← floating
```

Three zones: **left column** (brief + parties + financials), **main area** (graph + action items), **overlays** (delta banner, document HUD, chat).

---

## 1. Top bar

Anchors the dashboard — always visible, never scrolls away.

```
ADV-024-A  Musterklage GmbH vs. XY        ● Active  · AG Hamburg · 12 days
[Proceeding: AG Hamburg ▾]  [Graph|Truth Map|Timeline|Financials]  [+ doc] [✦]
```

### Left group — identity
- **Internal ID** (`Case.id`): `ADV-024-A` — the lead identifier, always first
- **Title** (`Case.title`): `Musterklage GmbH vs. XY`
- **Status dot**: color-coded from `Case.status` (active/dormant/closed); `dormant` is amber, `closed` is muted
- **Active proceeding name**: `AG Hamburg` — mirrors the proceeding switcher
- **Tempo hint**: `12 days` since last activity — computed from the most recent document's `ingest_date`. Goes red past Case Clock dormancy threshold.

### Middle group — controls
- **Proceeding switcher** dropdown — lists all `Proceeding` rows for this case with their court level + Az. Keyboard shortcut: `Cmd+P` (or `Ctrl+P` on Linux).
- **View mode tabs** — `Graph` (default) · `Truth Map` · `Timeline` · `Financials`. Remembered per-user in `UserSettings.settings_json["dashboard_default_view"]`.

### Right group — actions
- `[+ doc]` — upload new document (opens upload modal; arrives as a new `IngestBatch` of source_type `manual`)
- `[✦]` — toggle the Case AI Chat slide-in

---

## 2. AI Brief panel (left column, top)

The plain-language **living memo** describing where the case stands. Not a document summary — a *case* summary, accumulated.

```
AI BRIEF                          [refresh brief]

Status: Active — awaiting your Stellungnahme to
the Jugendamtsbericht from 28.03.

Key risks:
 · Frist Apr 30 (12 days)
 · §91 costs — opposing side is seeking 1.240 €

Open threads: 1 (Jugendamt report awaiting your response)

Recent development: Beschluss PKH granted 02.04.
Updated 4 hours ago.
```

### Structure

Backed by `Case.ai_brief` JSON:
```json
{
  "status_line": "Active — awaiting your Stellungnahme to ...",
  "key_risks": ["Frist Apr 30 (12 days)", "§91 costs — 1.240 €"],
  "open_threads": [
    {"thread": "Jugendamt report", "description": "awaiting your response"}
  ],
  "recent_development": "Beschluss PKH granted 02.04",
  "schema_version": 1
}
```

### How it's generated and kept current

- **On each new document ingested into the case**, a background task runs:
  `prompt = existing_ai_brief + new_document + user_reactions_on_new_doc → updated ai_brief`
- **Incremental, not rebuilt from scratch** — the brief evolves with the case rather than being regenerated from all 900 documents each time.
- **Timestamp shown as staleness hint** — `Case.ai_brief_updated_at` drives "Updated X hours ago".
- **[refresh brief]** forces a full re-analysis from all documents — expensive; used sparingly, e.g., after a major cleanup or a prompt update.
- Per-user override: a toggle in UserSettings can auto-refresh on every visit if desired (default: off; refresh is ingest-driven).

### What it's *not*
- Not a document summary — the per-document summary lives in `Document.ai_summary`
- Not a substitute for the Truth Map — claims-level detail is a separate view
- Not a prediction — no "you'll win 60%"; only factual observations and extracted risks

---

## 3. Correspondence graph (main canvas)

The primary navigation surface — everything the user does starts here.

See `docs/vision.md` §UI.3 for the full visual grammar (nodes, edges, N:N convergence, proceeding scope, significance filter). This section adds **interaction behavior** specific to the dashboard.

### Default state

- Graph scope: **current proceeding only**
- Significance filter: `significant+` (critical + significant visible; informational collapsed; administrative hidden)
- Layout: swim-lane vertical time axis
- Auto-fit zoom to show all visible nodes on first render

### Interactions

| Action | Effect |
|---|---|
| Click a node | Opens Document HUD as right-side slide-in (dashboard stays behind, dimmed) |
| Double-click a node | Full-screen document HUD (dashboard fully hidden) |
| Hover a node | Highlights all its incoming + outgoing edges; fades others |
| Click an edge | Pans/zooms to show both endpoints |
| Hover a bundle node (collapsed relay) | Expands inline to show enclosed documents |
| Right-click a node | Context menu: `View`, `Change proceeding`, `Add reaction`, `Copy link to doc` |
| Ctrl+scroll / pinch | Zoom |
| Drag empty area | Pan |
| `f` (keyboard) | Fit to screen |
| `c` (keyboard) | Center on critical nodes |

### Hidden-tier indicator

When the significance filter hides nodes, a small strip at the bottom of the graph states what's hidden:

```
ℹ 2 administrative documents hidden  [show all]
```

Clicking `[show all]` temporarily switches to filter=`all`; state persists only for this session.

### Thread-open indicator

Nodes where `Document.thread_open=True` (waiting for a response from the other side) render with a subtle amber glow. Helps spot "stuck" threads at a glance.

### Cross-proceeding references

When a document in the current proceeding references a document in another proceeding, a grayed edge points off-canvas to a small collapsed-proceeding node:

```
[Beschwerdeschrift OLG] ─ ─ ─ ─► [→ AG Hamburg · Beschluss 02.04]
```

Clicking that node switches the proceeding.

---

## 4. Action Items panel (bottom right)

Extracted deadlines, court dates, response-required, filing-required actions — all case-wide by default (not scoped to the current proceeding), because a Frist at AG matters even while you're viewing OLG.

```
ACTION ITEMS                                         [open] [completed] [all]

 ⚑  Apr 30 (12d)     Stellungnahme zu Beschluss vom 02.04      [open doc]
 ·   Jun 15 (58d)     Verhandlungstermin AG Hamburg             [court date]
 ◦   typical: hearing follows Klageerwiderung by 4–8 months
                 → window Jul–Nov 2026  (Case Clock)

```

### Row anatomy

- **Urgency icon**: `⚑` critical (overdue or <7 days); `·` near (7–30 days); ` ` far (>30 days)
- **Due date** + **relative days** — `Apr 30 (12d)`
- **Title** + **source link** — clicking opens the source document HUD
- **Action type badge**: `[deadline]` / `[court date]` / `[response required]` / `[filing required]`

### Filters

Tabs: `[open]` (default) · `[completed]` · `[all]`. Completed items cross out but remain visible in `[all]`.

### Case Clock hints

Below the concrete action items, the **Case Clock** inserts anticipated events based on court-specific tempo — always framed as ranges, never point predictions:

> **typical**: hearing follows Klageerwiderung by 4–8 months → window Jul–Nov 2026

Shown only when the AI has high-confidence temporal signals from prior proceedings at this court level. Phase 5 work; empty until then.

### Dormancy alert

If the current proceeding has been silent longer than typical, an alert rows appears:

> ⚠ This proceeding has been quiet 6 months — longer than typical. Is something pending outside the system?

Clickable to dismiss or to open a note.

---

## 5. Financial Exposure panel (left column, bottom)

```
FINANCIALS
Total:  1.690 €
+450 € (Apr 02)
[breakdown →]
```

### Content

- **Total** — `Case.total_cost_exposure` in cents, formatted as EUR
- **Last delta** — most recent `Document.cost_delta` with document date link
- **[breakdown →]** — switches the main area into the Financials view mode (see §10)

### Updated on ingest

Every document with a `cost_delta` JSON triggers:
1. Create `LegalCost` row (if not already)
2. Recompute `Case.total_cost_exposure`
3. Update the dashboard panel in place

No synthetic probability, no predicted cost exposure — only factual amounts from documents.

---

## 6. Parties strip (left column, middle)

```
PARTIES
● Court         (LG Hamburg)
● Opposing      (Anwaltskanzlei Müller)
● Jugendamt     (Bez. Hamburg-Nord)
● You           (Mandant)
```

Shown as colored dots matching `OriginatorType` (court=blue, opposing=red, own=green, third_party=amber, unknown=neutral) + the party name.

### Source

`Case.parties` JSON, populated/updated by the AI as documents arrive:

```json
{
  "court": {"name": "LG Hamburg", "color": "court"},
  "opposing": {"name": "Anwaltskanzlei Müller", "color": "opposing"},
  "third_parties": [
    {"name": "Jugendamt Bez. Hamburg-Nord", "role": "child services"}
  ]
}
```

Clicking a party filters the graph to show only documents authored by that party (toggle; click again to clear).

---

## 7. New-document delta banner

When new documents have landed in the case since the user last viewed it, a banner appears at the top:

```
⚡ 3 new documents since your last visit — 1 action added  [review →] [dismiss]
```

### Behavior

- **`[review →]`** — opens a modal listing each new document with its AI summary, the new `ActionItem` records, cost delta, and any claim changes. Each row clickable to open the document HUD.
- **`[dismiss]`** — updates the user's "last viewed" timestamp for this case; banner disappears.
- **Auto-dismiss** — if the user interacts with any new-document node in the graph, banner dismisses itself.

### How we track "since last visit"

Store per-user per-case timestamp in `UserSettings.settings_json`:
```json
{
  "last_viewed_cases": {
    "ADV-024-A": "2026-04-14T18:32:00Z"
  }
}
```

On dashboard load:
```
new_docs = documents.filter(ingest_date > last_viewed_cases.get(case_id, 0))
```

When multi-user lands later, this moves to a dedicated table.

---

## 8. Proceeding switcher behavior

Switching proceeding changes exactly three things:

| Changes with proceeding | Stays case-level |
|---|---|
| Correspondence graph (nodes + edges) | AI Brief (case-wide) |
| Hidden-tier counter below graph | Action Items (all proceedings) |
| Thread-open indicators | Financial exposure |
| Case Clock typical-range hints | Parties strip |
| Tempo hint in top bar | Delta banner |

Rationale: the user is reasoning about the **case** as a whole; the proceeding toggle is a graph-focus tool, not a page-wide filter. A Frist at AG matters when you're viewing OLG.

The active proceeding is persisted in `UserSettings.settings_json["active_proceeding"][case_id]` so returning to a case respects the last-used view.

---

## 9. View modes

Top bar tabs switch the **main area** (graph) to alternate visualizations. Left column and action items stay put.

### Graph (default, Phase 8)
Swim-lane correspondence graph — described above.

### Truth Map (Phase 6)
Claim-centric view. Main area shows contested factual/legal assertions grouped by status (asserted / contested / refuted / established). Each claim expands to show the evidence chain (documents supporting vs. contesting) and any user reactions from triage. See `docs/vision.md` §UI.5.

### Timeline (Phase 2+, always available)
Flat chronological list of documents in the current proceeding — the fallback when the graph isn't yet populated (e.g., early cases with no relationships detected). Lightweight; uses existing document repository queries.

### Financials (Phase 1, always available)
Tabular breakdown from `LegalCost` rows: per-category totals, paid vs. outstanding, §91 ZPO reimbursable, cost delta by document. Extends the existing `/costs` page scoped to this case. Includes a per-proceeding split.

### Mode persistence

Remembered per-user per-case in `UserSettings`. Default on first visit: Graph if any relationships exist, else Timeline.

---

## 10. Document HUD slide-in

Clicking a graph node (or a document link in Action Items, Delta banner, Brief citations, or Truth Map evidence) opens the same **Document HUD** component used in triage — semantically highlighted reading view with key_passages, claim annotations, reaction bar, AI summary with citations, and `[ask about this document ✦]`.

See `docs/triage.md` §2 for HUD structure.

### Integration with dashboard

- Slide-in from right, dimming (but not hiding) the dashboard
- `Esc` closes; dashboard stays on the same scroll position
- The clicked node in the graph stays highlighted while the HUD is open
- Keyboard: `→` in HUD navigates to the next node in document-date order; `←` previous

### Differences from triage HUD

- No batch-cascade controls (nothing to confirm; case is already assigned)
- Reaction bar remains available (users tag reactions at any time, not just at triage)
- Shows existing incoming + outgoing `DocumentRelationship` edges in a small side panel

---

## 11. Case AI Chat

Floating button `[✦ Ask AI]` at bottom-right corner. Clicking opens a slide-in from the right (separate from the document HUD — they don't overlap; document HUD slightly narrower to make room if both open).

### Scope and context

- **Scope**: case (all proceedings). Optional toggle: "Limit to current proceeding".
- **Context assembly**:
  - `Case.ai_brief` as system context
  - All `UserReaction` rows for documents in this case (high-weight — AI recalls your tags)
  - Semantic retrieval from document embeddings for query-relevant chunks
  - Recent `ActionItem` and `Claim` records
- **Every answer cites source documents** — clickable passage references open the document HUD at that passage.

### Conversation persistence

Each chat thread is a `Conversation` row with `scope_type='case'`, `scope_id=case.id`. Multiple conversations per case supported — user can return to past threads via a history dropdown at the top of the chat panel.

### Example queries

- "Which opposing statements haven't been responded to yet?"
- "What did I flag as 🚩 Lies during triage?"
- "Summarize all cost claims across all proceedings."
- "What's our argument on the custody question?"

---

## 12. Empty states

| Situation | What the dashboard shows |
|---|---|
| **New case, no documents** | Brief: "No documents yet. Upload the first to begin intelligence gathering." Graph empty. Action items empty. Single CTA: `[+ add document]`. |
| **Only one document so far** | Single node in the graph; brief synthesized from just that doc; no action items unless AI found deadlines. |
| **All documents are administrative** | Graph shows "2 administrative documents — show all?" hint; user can toggle significance filter to see them. |
| **AI brief not yet generated** | Brief panel shows shimmer + "AI is analyzing your case — first brief in ~30s". Status updates via htmx poll every 5s. |
| **AI brief generation failed** | Red banner on brief: "AI analysis failed: [error]. [retry]". Rest of dashboard still functional. |
| **No relationships detected yet** | Graph renders as disconnected nodes (no edges). View mode auto-switches to Timeline. Hint at top: "No relationships detected yet — switch to Timeline or wait for Phase 4 AI extraction." |
| **Case closed** | Status dot muted; "Closed on [date]" shown in top bar; delta banner suppressed; all panels read-only. |

---

## 13. Keyboard-first interaction

Dashboard is designed to work without a mouse for common navigation.

| Key | Action |
|---|---|
| `g` | Switch to Graph view |
| `t` | Switch to Truth Map view |
| `l` | Switch to Timeline view (L for List/Linear) |
| `$` | Switch to Financials view |
| `Cmd+P` / `Ctrl+P` | Open Proceeding switcher |
| `1`..`9` | Jump to proceeding N |
| `/` | Focus Case AI Chat input |
| `+` | Open upload modal |
| `a` | Jump to Action Items panel |
| `r` | Refresh AI brief |
| `f` | Fit graph to screen |
| `c` | Center graph on critical nodes |
| Arrow keys (in graph) | Navigate between nodes |
| `Enter` (in graph) | Open Document HUD for selected node |
| `Esc` | Close overlay (HUD, chat, delta modal) |

Shortcuts are shown in a cheat-sheet accessible via `?`.

---

## 14. Data sources map

What populates which zone, from which Phase 1 table.

| Dashboard zone | Primary source | Populated by phase |
|---|---|---|
| Top bar identity | `Case.id`, `Case.title`, `Case.status` | Phase 1 (existing) |
| Active proceeding chip | `Proceeding` rows + UserSettings active | Phase 1 |
| Tempo hint | `Document.ingest_date` (MAX over case) | Phase 1 (existing) |
| AI Brief | `Case.ai_brief` (JSON) | Phase 5 |
| Key risks / open threads | derived from `Case.ai_brief` | Phase 5 |
| Parties strip | `Case.parties` (JSON) | Phase 5 |
| Correspondence graph nodes | `Document` + `Document.significance_tier` | Phase 4 (AI) + Phase 8 (renderer) |
| Graph edges | `DocumentRelationship` | Phase 4 (detection) + Phase 7 (render) |
| Node colors | `Document.attributed_originator` + `OriginatorType` | Phase 4 |
| Node reactions | `UserReaction` | Phase 2 |
| Court relay collapse | `Document.court_relay` + `Document.role` | Phase 3/4 |
| Thread-open glow | `Document.thread_open` | Phase 4 |
| Action Items | `ActionItem` rows, scoped to case | Phase 1/3 (creation) + Phase 5 UI |
| Case Clock ranges | derived from historical `ActionItem` patterns | Phase 5 |
| Dormancy alert | comparison of silent time vs. typical | Phase 5 |
| Financial exposure total | `Case.total_cost_exposure` | Phase 4 (update trigger) |
| Cost delta rows | `Document.cost_delta` | Phase 4 |
| Delta banner | compare `Document.ingest_date` vs. `UserSettings.last_viewed_cases[case_id]` | Phase 5 |
| Truth Map (tab) | `Claim` + `ClaimEvidence` + `UserReaction` | Phase 6 |
| Case Chat | `Conversation` + `ConversationMessage` + AI context | Phase 7 |
| Document HUD slide-in | `Document` + `Document.key_passages` + reactions | Phase 2 (component) + Phase 4 (content) |

---

## 15. Files that will change

Dashboard implementation spans Phase 5 (shell + brief + actions + financials) and Phase 7 (graph renderer). Truth Map tab arrives in Phase 6; chat in Phase 7.

### New
| File | Purpose |
|---|---|
| `app/api/dashboard_case.py` | Case-scoped dashboard route (`GET /cases/<id>`) — returns aggregated context |
| `app/services/case_dashboard_service.py` | Aggregates brief, proceedings, action items, financials, parties, delta for rendering |
| `app/services/ai_brief.py` | Brief generation + incremental update logic |
| `app/services/case_clock.py` | Dormancy detection + typical-range estimation |
| `app/templates/pages/case_dashboard.html` | Replaces `case_stream.html` as primary case view |
| `app/templates/partials/dashboard/ai_brief.html` | Left-column brief panel |
| `app/templates/partials/dashboard/parties.html` | Parties strip |
| `app/templates/partials/dashboard/financials.html` | Cost summary panel |
| `app/templates/partials/dashboard/action_items.html` | Open/completed list + Case Clock hints |
| `app/templates/partials/dashboard/delta_banner.html` | New-documents notification |
| `app/templates/partials/dashboard/proceeding_switcher.html` | Dropdown + active-proceeding badge |
| `app/templates/partials/dashboard/view_mode_tabs.html` | Graph/Truth Map/Timeline/Financials toggle |
| `app/templates/partials/dashboard/correspondence_graph.html` | SVG renderer (Phase 8) |
| `app/templates/partials/dashboard/truth_map.html` | Claims view (Phase 6) |
| `app/templates/partials/dashboard/timeline_view.html` | Flat chronological fallback |
| `app/templates/partials/dashboard/ai_chat.html` | Case-scoped chat panel |
| `static/js/dashboard.js` | Keyboard shortcuts, view-mode persistence, graph interactions |
| `static/js/graph_renderer.js` | SVG graph interactions (zoom/pan/hover/click) |
| `app/repositories/conversation.py` | Conversation + message CRUD (new) |

### Modified
| File | Change |
|---|---|
| `app/api/cases.py` | Redirect `/cases/<id>` to new dashboard route; keep list route |
| `app/services/case_service.py` | Extend `get_case_with_summary` to include brief, parties, delta context |
| `app/models/database.py` | (no new columns needed for Phase 5 — already laid down in Phase 1) |
| `app/services/ingestion/service.py` | Hook: on new-document ingest, trigger incremental brief update task |
| `app/tasks/ai_summaries.py` | Add `update_case_brief_task(case_id, new_doc_id)` |

---

## 16. Phase progression map

Dashboard comes alive gradually as phases land. Nothing is a hard dependency — the dashboard renders meaningfully at each stage.

| Phase | What lights up on the dashboard |
|---|---|
| **After Phase 2** (triage) | Top bar, Timeline view, basic Action Items (from ActionItem table), Financial exposure (from LegalCost), Parties strip (manual for now), Document HUD on click. No graph, no brief, no claims. |
| **After Phase 3** (email ingest) | Delta banner starts firing (real ingest timestamps); batches populate metadata faster. |
| **After Phase 4** (document intelligence) | Graph nodes and edges appear (significance_tier + DocumentRelationship); thread-open glows; court_relay bundles collapse; attributed_originator drives node colors. |
| **After Phase 5** (case AI brief) | AI Brief panel populates; Case Clock hints appear in Action Items; parties auto-detected; delta banner shows impact summaries. |
| **After Phase 6** (Truth Map) | Truth Map tab works; claim annotations appear inline in document HUD. |
| **After Phase 7** (chat + graph polish) | Case AI Chat available; graph renderer fully interactive (zoom/pan/hover). |

---

## 17. Non-goals for this dashboard

- **No per-document list view.** Timeline is as close as we get — a flat, dated sequence. Never a sortable table.
- **No synthetic probabilities.** No "you'll win 60%"; no "30% risk"; no estimated settlement value. Only factual observations and extracted risks from the documents.
- **No raw PDF viewer on this screen.** Documents open as HUDs; if the user insists on the raw PDF they click a separate "open original" link inside the HUD.
- **No notifications widget.** Notifications are a global header feature, not dashboard content. Dashboard shows the case-specific delta banner only.
- **No editing of AI brief.** The brief is derived, not authored. To influence it, the user tags reactions or adds manual claims (Truth Map).
- **No bulk actions.** This is a single-case view; bulk operations live in the case directory.
- **No exports from the dashboard itself.** Export/print comes from a separate report view (Phase 8+).

---

## 18. Success criteria

The dashboard is done when:

- **Time-to-orient**: opening a case gets the user to "I know what's going on and what's next" in under 10 seconds, without clicking anything
- **Navigation**: the user can reach any specific document in the case in ≤2 clicks (click node → open HUD) without ever seeing a file list
- **Brief staleness**: brief is always ≤1 minute stale relative to the latest ingested document (background update lag)
- **Delta fidelity**: new-document banner correctly shows 100% of documents added since last visit, and dismissing it advances the timestamp atomically
- **Significance filter effectiveness**: default `significant+` view for a case with 900 documents renders ≤200 nodes (≥80% noise reduction)
- **Keyboard navigation**: every interactive element reachable without a mouse; power users can triage → review → dismiss entirely keyboard-driven
- **Multi-proceeding handling**: switching proceedings updates graph + hidden tier + tempo within 200ms; never loses scroll position on other panels
- **Empty states**: fresh case (no docs) renders without errors or empty-rectangle ugliness; one-doc case renders a meaningful brief
- **Case Clock integrity**: all tempo hints are ranges with rationale; no point predictions; dormancy alerts fire correctly for proceedings silent longer than typical
- **AI chat grounding**: every answer cites source documents; clicking citations opens the HUD at the cited passage

---

## Related docs

- `docs/vision.md` — north-star architecture, data model, design principles
- `docs/triage.md` — Phase 2 intake flow; dashboard is the destination after triage completes
