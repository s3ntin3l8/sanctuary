# Sanctuary — Timeline View

Companion document to `docs/specs/00_vision.md` §UI. Covers the per-case chronological fallback view mode on the case dashboard. The cross-case Master Timeline was explicitly deleted from the primary nav (vision §UI:380); this spec documents the canonical per-case surface.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

| Layer | Status |
|---|---|
| Per-case Timeline view-mode tab (`view='timeline'`) in `case_dashboard.html:94-98` | ✅ |
| `partials/case_timeline_panel.html` — significance pill + title + originator + relative date | ✅ |
| Document set from `case_dashboard_service.py:158-162` sorted by `issued_date or ingest_date` desc | ✅ |
| Empty state ("No documents yet.") | ✅ |
| Keyboard `l` switches to Timeline (`static/js/dashboard.js:374`) | ✅ |
| Settings default-view selector includes `timeline` (`pages/settings/appearance.html:46-57`) | ✅ |
| Auto-fallback to Timeline when graph has zero edges | ✅ |
| Click row → Document HUD slide-in | ✅ |
| Cross-case Master Timeline removal | ✅ |

### Implementation Deviations

| Feature | Vision §UI / Dashboard §9 | Code | Status |
|---|---|---|---|
| Timeline as view-mode only | "Timeline exists as a view mode inside each case dashboard." | Implemented via `view='timeline'` Alpine state | ✅ Accepted |
| Cross-case Master Timeline | "Deleted" (vision §UI:380) | Removed from API, routes, and templates | ✅ Accepted |
| File path for panel partial | `partials/dashboard/timeline_view.html` | `partials/case_timeline_panel.html` | ✅ Accepted |
| Lightweight — reuses existing queries | "Lightweight; uses existing document repository queries" | Reuses `documents_sorted` — no additional query | ✅ Accepted |
| Auto-switch when no relationships | "View mode auto-switches to Timeline" | Logic in `case_dashboard_service.py` defaults to timeline if edges=0 | ✅ Accepted |
| Click → Document HUD | Implied by `02_dashboard.md §10` | Implemented via HTMX triggers on rows | ✅ Accepted |

---

## The core shift

**Traditional document management:** a chronological filing list is the primary view — you scroll through dates to find the relevant document.

**Sanctuary Timeline:** the correspondence graph is the primary view because relationships between documents reveal case dynamics that a flat list cannot. The Timeline view exists as a **fallback** — for new cases where relationships have not yet been detected, or for the occasional chronological scan needed by the user. It is never a destination; it is always entered through the case dashboard's view-mode tab. Selecting Timeline from the primary nav has been explicitly removed from the design.

The Timeline renders the same document set that the graph uses, filtered by the same top-bar significance filter, sorted by `issued_date` (falling back to `ingest_date`). The single round-trip to `case_dashboard_service.py` serves all four view modes.

---

## Layout overview

```
ADV-024-A  Musterklage GmbH vs. XY   [AG Hamburg ▾]   [critical] [significant+] [all]
┌─ view ─────────────────────────────────────────────────────────────────────────────┐
│  ◯ Graph   ◯ Truth Map   ⬤ Timeline   ◯ Financials                               │
└────────────────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────────────────┐
│  [crit]   Klageerwiderung Beklagter                                  ← clickable  │
│           RA Müller (Opposing)  ·  3 days ago                                      │
├────────────────────────────────────────────────────────────────────────────────────┤
│  [sig]    Stellungnahme zum Schriftsatz vom 01.04                    ← clickable  │
│           RA Schmidt (Own Counsel)  ·  1 week ago                                  │
├────────────────────────────────────────────────────────────────────────────────────┤
│  [sig]    Begleitschreiben LG Hamburg                                ← clickable  │
│           LG Hamburg (Court relay)  ·  2 weeks ago                                │
├────────────────────────────────────────────────────────────────────────────────────┤
│  [—]      Anl. K1 — Stundennachweis                                  ← clickable  │
│           RA Müller (Opposing → proof attach)  ·  2 weeks ago                     │
└────────────────────────────────────────────────────────────────────────────────────┘

                  No documents yet.        ← empty state
```

---

## 1. Data sourcing

Timeline issues **no additional database query**. It reuses `documents_sorted` already computed by `case_dashboard_service.py:158-162`:

```python
documents_sorted = sorted(
    data["documents"],
    key=lambda d: d.issued_date or d.ingest_date,
    reverse=True,
)
```

`data["documents"]` is the significance-filtered document list for the active proceeding. The same list drives the graph's node set — Timeline is literally the flattened, sorted version of the same data. This keeps the "lightweight" promise: the Timeline adds zero server-side cost to the dashboard render.

---

## 2. Row anatomy

Each row in `partials/case_timeline_panel.html` renders:

| Element | Source | Behaviour |
|---|---|---|
| Significance pill | `doc.significance_tier.value` | Color-coded by tier |
| Title | `doc.title or 'Untitled'` | Truncated to one line |
| Originator line | `doc.attributed_originator or doc.sender` | Originator name |
| Date suffix | `doc.issued_date` | Relative time (e.g. "3 days ago") |
| Hover state | CSS | Signals interactivity |

---

## 3. Interaction — click row → Document HUD

All rows are interactive and use HTMX to load the document HUD into the dashboard slot. This ensures a seamless transition from the flat chronological scan to deep semantic reading.

```html
<div hx-get="/document/{{ doc.id }}/hud"
     hx-target="#hud-slot"
     hx-swap="innerHTML"
     class="cursor-pointer hover:bg-surface-container-high">
```

---

## 4. Auto-fallback to Timeline

When a case is first opened and has zero detected document relationships (e.g. only one document present), the dashboard automatically switches to the Timeline view to avoid showing a sparse or disconnected graph. This logic is handled by `CaseDashboardService`.

---

## 5. Success criteria

- `partials/case_timeline_panel.html` is the only timeline rendering surface.
- Chronological list inherits significance filtering from the top bar.
- Rows are clickable and open the document HUD slide-in.
- Empty graphs default to the timeline view for better user orientation.
- Keyboard shortcuts (`l` for timeline, `g` for graph) allow fast switching.

---

## Related docs

- `docs/specs/00_vision.md` — Nav architecture
- `docs/specs/02_dashboard.md` — View mode integration
- `docs/specs/04_document_hud.md` — Reading component
- `docs/specs/06_truth_map.md` — Factual layer sibling
- `docs/specs/08_financials.md` — Cost tracking sibling
