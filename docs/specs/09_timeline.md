# Sanctuary — Timeline View

Companion document to `docs/specs/00_vision.md` §UI. Covers the per-case chronological fallback view mode on the case dashboard. The cross-case Master Timeline was explicitly deleted from the primary nav (vision §UI:380); this spec documents the canonical per-case surface and marks the vestigial cross-case infrastructure for removal.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟡 IMPLEMENTED with vestigial code — three remediation items (row interactivity, auto-fallback, vestigial file deletion)

| Layer | Status |
|---|---|
| Per-case Timeline view-mode tab (`view='timeline'`) in `case_dashboard.html:94-98` | ✅ |
| `partials/case_timeline_panel.html` — significance pill + title + originator + relative date | ✅ |
| Document set from `case_dashboard_service.py:158-162` sorted by `issued_date or ingest_date` desc | ✅ |
| Empty state ("No documents yet.") | ✅ |
| Keyboard `l` switches to Timeline (`static/js/dashboard.js:374`) | ✅ |
| Settings default-view selector includes `timeline` (`pages/settings/appearance.html:46-57`) | ✅ |
| Auto-fallback to Timeline when graph has zero edges (vision §UI: "View mode auto-switches to Timeline.") | ⚠ not implemented — `case_dashboard_service.py` defaults to `graph` regardless of edge count on first visit |
| Click row → Document HUD slide-in | ⚠ rows are non-interactive `<div>`s — no HTMX trigger |
| Cross-case `/timeline` page + `/timeline/data` HTMX endpoint | ❌ vestigial — vision §UI says deleted; not linked from any nav surface |
| `app/api/timeline_api.py` (95 lines) | ❌ vestigial — mounted at both root and `/api/v1` via `app/main.py:389` and `app/api/__init__.py:19` |
| `app/templates/pages/timeline.html` (160 lines) | ❌ vestigial |
| `app/templates/partials/timeline_items.html` (8 lines) | ❌ vestigial HTMX partial |
| `DocumentService.get_documents_paginated` | ❌ vestigial — sole consumer is the orphan `/timeline/data` |
| Period grouping (month headers) | ❌ not in canonical panel; deferred to v2 |
| Per-originator filter chips | ❌ not in canonical panel; significance inherited from top-bar filter |
| Tests for timeline | ❌ `grep -rln 'timeline' tests/` returns no matches |

### Implementation Deviations

| Feature | Vision §UI / Dashboard §9 | Code | Status |
|---|---|---|---|
| Timeline as view-mode only | "Timeline exists as a view mode inside each case dashboard." | `view='timeline'` Alpine state on case dashboard | ✅ Accepted |
| Cross-case Master Timeline | "Deleted" (vision §UI:380) | `app/api/timeline_api.py` registered at root + `/api/v1`; renders `pages/timeline.html` | ⚠ Violation — must delete |
| File path for panel partial | `partials/dashboard/timeline_view.html` (`02_dashboard.md:506`) | `partials/case_timeline_panel.html` | ⚠ Spec drift — fix `02_dashboard.md:506` |
| Lightweight — reuses existing queries | "Lightweight; uses existing document repository queries" (`02_dashboard.md §9`) | Reuses `documents_sorted` from `case_dashboard_service.py:158-162` — no additional query | ✅ Accepted |
| Auto-switch when no relationships | "View mode auto-switches to Timeline" (`02_dashboard.md §12`) | `case_dashboard_service.py` defaults to `graph` for first visit regardless of edge count | ⚠ Known gap |
| Click → Document HUD | Implied by `02_dashboard.md §10` — all document links open the HUD | Rows are plain `<div>`s with no click handler | ⚠ Known gap |

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
| Significance pill | `doc.significance_tier.value` | `bg-error/20 text-error` for `critical`; `bg-secondary/20 text-secondary` for `significant`; `bg-surface-container-highest text-on-surface-variant` for all others |
| Title | `doc.title or 'Untitled'` | Truncated to one line (`truncate`) |
| Originator line | `doc.attributed_originator or doc.sender or 'Unknown sender'` | `text-xs text-on-surface-variant` |
| Date suffix | `doc.issued_date` formatted by `format_relative_time` filter | Suppressed when `issued_date` is null |
| Background on hover | `hover:bg-surface-container-high` (post-remediation) | Signals the row is interactive |

---

## 3. Interaction — click row → Document HUD

**⚠ Known gap.** Currently rows are non-interactive `<div>`s. The vision implies all document links open the Document HUD slide-in (`02_dashboard.md §10`). The fix is a one-liner on the row container in `case_timeline_panel.html`:

```html
<div class="flex items-start gap-3 rounded-lg border border-surface-container-high
            bg-surface-container p-3 text-sm cursor-pointer hover:bg-surface-container-high"
     hx-get="/document/{{ doc.id }}/hud"
     hx-target="#hud-slot"
     hx-swap="innerHTML"
     hx-trigger="click">
```

This mirrors the click pattern used by graph nodes and triage document cards. The `#hud-slot` target already exists in `case_dashboard.html`.

---

## 4. Auto-fallback to Timeline

**⚠ Known gap.** `02_dashboard.md §12` states: "View mode auto-switches to Timeline. Hint at top: 'No relationships detected yet — switch to Timeline or wait for Phase 4 AI extraction.'" This is not implemented.

The fix is in `case_dashboard_service.py`, when computing `active_view` for a first visit (no persisted `UserSettings.dashboard_view[case_id]`):

```python
if not persisted_view:
    has_edges = bool(graph_dict and graph_dict.get("edge_count", 0) > 0)
    active_view = "graph" if has_edges else "timeline"
```

A persisted user choice (from `set_dashboard_view`) always wins; this only affects the very first render of a case.

---

## 5. Significance filter inheritance

The top-bar `[critical] [significant+] [all]` filter applies before `case_dashboard_service.py` builds `documents_sorted`. Because Timeline reuses that sorted list, it inherits the filter with zero additional code — what's displayed in Timeline is always the same document set the graph would show. No per-panel filter chip is needed for significance in v1.

---

## 6. Settings — default view

`pages/settings/appearance.html:46-57` exposes a `Default view` selector that includes `timeline`. It posts to `POST /api/user-settings/dashboard-view` and persists in `UserSettings.settings_json["dashboard_view"]`. When set, every case the user opens lands on Timeline by default, overriding the auto-fallback logic in §4.

---

## 7. Cross-case Master Timeline removal

Vision §UI:380 is unambiguous:

> Master Timeline (cross-case flat list) | Deleted. Timeline exists as a view mode inside each case dashboard.

The following files exist in the codebase but are **not referenced by any nav surface, sidebar link, or template include**. They must be deleted under the pre-release "clean as you go" rule:

| File | Lines | Notes |
|---|---|---|
| `app/api/timeline_api.py` | 95 | Exposes `GET /timeline` + `GET /timeline/data`; registered twice (root + `/api/v1` via `app/main.py:389` and `app/api/__init__.py:19`) |
| `app/templates/pages/timeline.html` | 160 | Full Master Timeline page with filter panel, pagination state |
| `app/templates/partials/timeline_items.html` | 8 | HTMX pagination partial only used by `/timeline/data` |
| `DocumentService.get_documents_paginated` (`app/services/document_service.py:234-254`) | 21 | Cursor-based paginator — sole consumer is the orphan `/timeline/data` |

Removal also requires:
- `app/main.py:373` — drop `timeline_api_router` import
- `app/main.py:389` — drop `app.include_router(timeline_api_router)`
- `app/api/__init__.py:10` — drop `timeline_api` import
- `app/api/__init__.py:19` — drop `api_router.include_router(timeline_api.router)`
- `app/api/__init__.py:28` — drop `"timeline_api_router"` from `__all__`
- `app/api/__init__.py:37` — drop `timeline_api_router = timeline_api.router`

---

## 8. Empty states

| Situation | What renders |
|---|---|
| No documents in case | `<p class="text-sm italic text-on-surface-variant text-center py-8">No documents yet.</p>` |
| Significance filter = `critical` with no critical documents | Same empty-state message (filter applies upstream; panel receives an empty list) |
| Default-view = `timeline` on a case with no documents | Same empty state — no special message needed; the new-case empty state on the main dashboard area (`02_dashboard.md §12`) handles upload CTAs |

---

## 9. Keyboard-first interaction

| Key | Action | Source |
|---|---|---|
| `l` | Switch to Timeline view | `static/js/dashboard.js:374` — already wired |
| `Esc` | Close Document HUD if open | `static/js/dashboard.js` — already wired |
| `Enter` (focused row) | Open Document HUD for focused row | Post-remediation: requires `tabindex="0"` on rows + keydown handler |
| `↑` / `↓` | Navigate between rows | Post-remediation: same tabindex approach; standard browser focus traversal |
| `g` | Switch to Graph view | `static/js/dashboard.js:374` |
| `t` | Switch to Truth Map view | `static/js/dashboard.js:374` |
| `$` | Switch to Financials view | `static/js/dashboard.js:374` |

---

## 10. Data sources map

| Dashboard zone | Source | Table / Field | Phase |
|---|---|---|---|
| Row list | `case_dashboard_service.documents_sorted` | `Document` filtered by `proceeding_id` + significance tier | Phase 1 (schema) + Phase 5 (dashboard shell) |
| Significance pill | `Document.significance_tier` | `Document.significance_tier` | Phase 4 (AI tier assignment) |
| Originator label | `Document.attributed_originator or sender` | `Document.attributed_originator`, `Document.sender` | Phase 3/4 (relay detection) |
| Relative date | `Document.issued_date` | `Document.issued_date` | Phase 1 |
| Date fallback | `Document.ingest_date` | `Document.ingest_date` | Phase 1 |
| Active proceeding scope | `UserSettings.settings_json["active_proceeding"][case_id]` | `UserSettings` | Phase 5 |
| Significance filter | `UserSettings.settings_json["significance_filter"][case_id]` | `UserSettings` | Phase 5 |
| Default view setting | `UserSettings.settings_json["dashboard_view"]` | `UserSettings` | Phase 5 |

---

## 11. Files that will change

### Modified

| File | Change |
|---|---|
| `app/templates/partials/case_timeline_panel.html` | Add `hx-get`, `hx-target="#hud-slot"`, `hx-swap="innerHTML"`, `cursor-pointer`, `hover:bg-surface-container-high` to row container; add `tabindex="0"` for keyboard nav |
| `app/services/case_dashboard_service.py` | When no persisted `dashboard_view[case_id]` and `graph_dict["edge_count"] == 0`, default `active_view = 'timeline'` |
| `app/main.py` | Remove `timeline_api_router` import (line 373) and `app.include_router(timeline_api_router)` (line 389) |
| `app/api/__init__.py` | Remove `timeline_api` import, router registration, `__all__` entry, and export alias (lines 10, 19, 28, 37) |
| `docs/specs/00_vision.md` | Under §UI nav table, add footnote on "Master Timeline … Deleted" row referencing `09_timeline.md` for the per-case canonical surface |
| `docs/specs/02_dashboard.md:355-356` | Add "See `docs/specs/09_timeline.md`" link in §9 Timeline view mode sub-section |
| `docs/specs/02_dashboard.md:506` | Replace `partials/dashboard/timeline_view.html` → `partials/case_timeline_panel.html` (file already exists; remove from the "New" table or correct path) |

### Deleted

| File | Reason |
|---|---|
| `app/api/timeline_api.py` | Orphan cross-case Master Timeline route — vision §UI says deleted |
| `app/templates/pages/timeline.html` | Orphan Master Timeline page |
| `app/templates/partials/timeline_items.html` | Orphan HTMX pagination partial |
| `DocumentService.get_documents_paginated` in `app/services/document_service.py:234-254` | Sole consumer is the deleted `/timeline/data` endpoint |

### New

| File | Purpose |
|---|---|
| `tests/integration/test_case_timeline_view.py` | Coverage: rows render in `issued_date` desc order; HUD opens on row click; auto-fallback when graph edge count = 0; cross-case `/timeline` returns 404 after deletion |

---

## 12. Phase progression map

| Phase | What lights up on the Timeline view |
|---|---|
| **Phase 1** (schema) | `Document`, `issued_date`, `ingest_date`, `sender` available |
| **Phase 3/4** (doc intelligence) | `attributed_originator` populated; `significance_tier` assigned; significance pill meaningful |
| **Phase 5** (case dashboard shell) | Timeline view tab wired; keyboard shortcut `l`; settings default-view |
| **Phase 6+** (remediation) | Row click → HUD; auto-fallback to Timeline on empty graph; vestigial cross-case code deleted |

---

## 13. Non-goals

- **No cross-case Timeline** — deleted by design; ⌘K and Home feed are the cross-case aggregation surfaces.
- **No period grouping in v1** — "April 2026" month headers deferred; case-scoped lists are short enough that headers add noise before there are 50+ documents.
- **No per-originator filter chips in Timeline** — the top-bar significance filter is shared; originator filtering is done in the graph where the originator-colour encoding is more useful.
- **No infinite-scroll / pagination** — case-scoped document sets are bounded; the Financials and Truth Map views handle their own pagination requirements independently.
- **No CSV / print export** — out of scope for v1.
- **No "all proceedings" aggregate** — Timeline is scoped to the active proceeding, consistent with all other view modes.

---

## 14. Verification

### Manual

1. `make seed && make run` → open `/cases/<seeded-case>?view=timeline` → rows render in `issued_date` descending order; significance pills coloured correctly (`critical` = red/20, `significant` = secondary/20, other = surface-highest); relative dates render.
2. Click a row → Document HUD slides in from the right. *(Post-remediation: `case_timeline_panel.html` with HTMX trigger.)*
3. Open a brand-new case with zero imported documents → check that `DocumentRelationship` count = 0 → dashboard renders Timeline by default without needing the user to click. *(Post-remediation: auto-fallback in `case_dashboard_service.py`.)*
4. Visit `/timeline` directly → 404 Not Found. Same for `/api/v1/timeline` and `/timeline/data`. *(Post-deletion.)*
5. Settings → Appearance → set Default view to `timeline` → reload `/cases/<id>` for a case with graph edges → opens on Timeline view despite graph being available.
6. Top-bar filter set to `[critical]` → Timeline shows only critical-tier rows (inherited from significance filter; no additional code).
7. Press `l` on any dashboard view → switches to Timeline; `g` returns to Graph.

### Automated

`tests/integration/test_case_timeline_view.py`:

- `test_timeline_rows_ordered_by_issued_date` — seed 3 documents with different `issued_date`s; assert rendered rows in descending order.
- `test_timeline_row_click_opens_hud` — GET the dashboard; assert each row has `hx-get="/document/{id}/hud"` attribute.
- `test_timeline_autofallback_when_no_edges` — create a case with documents but no `DocumentRelationship` rows; assert `active_view == 'timeline'` in the template context.
- `test_cross_case_timeline_returns_404` — `GET /timeline` → 404; `GET /api/v1/timeline` → 404. *(Post-deletion.)*

```
pytest tests/integration/test_case_timeline_view.py -v   # all green
grep -rln "timeline" tests/                               # returns the new test file only
```

---

## 15. Success criteria

- `partials/case_timeline_panel.html` is the **only** timeline rendering surface in the codebase.
- Vestigial files (`timeline_api.py`, `pages/timeline.html`, `partials/timeline_items.html`) deleted; `GET /timeline` returns 404 in dev.
- `DocumentService.get_documents_paginated` removed; `grep -rn 'get_documents_paginated' app/` returns no results.
- Timeline rows are clickable; clicking opens the Document HUD without a full page reload.
- First-visit cases with zero graph edges land on Timeline automatically.
- Spec drift in `02_dashboard.md:506` resolved: `partials/dashboard/timeline_view.html` reference corrected to `partials/case_timeline_panel.html`.
- All existing `pytest` tests pass; new `test_case_timeline_view.py` suite passes.
- Row significance pills, originator labels, and relative dates render correctly for all significance tiers.

---

## Related docs

- `docs/specs/00_vision.md` — §UI navigation table (Master Timeline deletion rationale)
- `docs/specs/02_dashboard.md` — §9 View modes (Timeline sub-section); §12 Empty states; §13 Keyboard shortcuts
- `docs/specs/04_document_hud.md` — Document HUD component opened by row click
- `docs/specs/06_truth_map.md` — sibling view mode: contested-claims surface
- `docs/specs/08_financials.md` — sibling view mode: statutory cost tracking
