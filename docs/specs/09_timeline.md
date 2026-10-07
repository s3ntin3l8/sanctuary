# Sanctuary — Timeline View

Companion document to `docs/specs/00_vision.md` §UI. Covers the per-case chronological fallback view mode on the case dashboard. The cross-case Master Timeline was explicitly deleted from the primary nav (vision §UI:380); this spec documents the canonical per-case surface.

---

## Implementation Status

**Last Updated:** October 6, 2026
**Status:** 🟢 IMPLEMENTED — `GET /api/v1/cases/{id}/timeline` (`CaseTimelineService`) and `frontend/src/features/cases/dashboard/TimelineTab.tsx`.

| Layer | Status |
|---|---|
| Per-case Timeline view-mode tab (`?view=timeline`, labelled "Calendar", `frontend/src/features/cases/dashboard/CasePage.tsx`) | ✅ |
| `TimelineTab.tsx` — date, actor dot, kind icon, title, critical / overdue / amount / ⚖ claim-count markers (`useCaseTimeline` in `frontend/src/api/caseDetail.ts`) | ✅ |
| Event stream from `CaseTimelineService.build_payload` (`app/services/case_timeline_service.py`) via `GET /api/v1/cases/{id}/timeline`, sorted by date ascending | ✅ |
| Empty state ("No documents yet.") | — (not in the SPA; an empty case renders an empty list behind the `0 / 0` counter) |
| Keyboard `l` switches to Timeline (`KEY_TO_VIEW` in `CasePage.tsx`) | ✅ |
| Settings default-view selector includes `timeline` | — (not in the SPA) |
| Auto-fallback to Timeline when graph has zero edges | — (not in the SPA; `CasePage.tsx` defaults to `graph`) |
| Click row → Document HUD (`navigate('/document/{id}')` → `frontend/src/features/documents/DocumentPage.tsx`) | ✅ |
| Cross-case Master Timeline removal | ✅ |

### Implementation Deviations

| Feature | Vision §UI / Dashboard §9 | Code | Status |
|---|---|---|---|
| Timeline as view-mode only | "Timeline exists as a view mode inside each case dashboard." | Implemented via the `?view=timeline` search param in `CasePage.tsx` | ✅ Accepted |
| Cross-case Master Timeline | "Deleted" (vision §UI:380) | Removed from the API and the SPA routes | ✅ Accepted |
| Lightweight — reuses existing queries | "Lightweight; uses existing document repository queries" | `CaseTimelineService.build_payload` runs its own aggregation over documents, action items, costs and proceedings behind a separate `GET /api/v1/cases/{id}/timeline` request (see §1) | Deviates — separate query |
| Auto-switch when no relationships | "View mode auto-switches to Timeline" | Not implemented — `CasePage.tsx` defaults to `graph` regardless of edge count | ❌ Not implemented |
| Click → Document HUD | Implied by `02_dashboard.md §10` | Document-backed rows are buttons that `navigate('/document/{id}')` (`TimelineTab.tsx`) | ✅ Accepted |

---

## The core shift

**Traditional document management:** a chronological filing list is the primary view — you scroll through dates to find the relevant document.

**Sanctuary Timeline:** the correspondence graph is the primary view because relationships between documents reveal case dynamics that a flat list cannot. The Timeline view exists as a **fallback** — for new cases where relationships have not yet been detected, or for the occasional chronological scan needed by the user. It is never a destination; it is always entered through the case dashboard's view-mode tab. Selecting Timeline from the primary nav has been explicitly removed from the design.

The Timeline renders every dated event in the case — documents (`issued_date`, falling back to `ingest_date`), action items, legal costs and proceeding milestones — from its own `GET /api/v1/cases/{id}/timeline` request, with client-side actor / kind / future chips rather than the graph's significance filter.

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

`GET /api/v1/cases/{case_id}/timeline` (`app/api/v1/case_detail.py`) calls `CaseTimelineService.build_payload(case_id)` (`app/services/case_timeline_service.py`), which merges four sources into one `TimelineEvent` list sorted ascending by date and returns it as `TimelineView` (`app/schemas/case_detail.py`):

| Source | Event kinds | Date |
|---|---|---|
| `Document` | filing / order / statement / report / relay / payment (from `DocumentType`, falling back to the actor lane) | `issued_date or ingest_date` |
| `ActionItem` | hearing (court dates) / deadline | `due_date` |
| `LegalCost` | payment (debit / credit) | `paid_at or due_at` |
| `Proceeding` | milestone (opened / closed) | `started_at` / `ended_at` |

Court relays are emitted as a court event plus one row per substantive child document. The payload also carries `month_buckets` (per-month totals for the ribbon) and a `quiet_gap_days` marker on events preceded by ≥14 days of silence in the same month. The SPA fetches it once per case through `useCaseTimeline` (`frontend/src/api/caseDetail.ts`); the actor, kind and future chips filter client-side in `TimelineTab.tsx`.

---

## 2. Row anatomy

Each row in `TimelineTab.tsx` renders one `TimelineEventView`:

| Element | Source | Behaviour |
|---|---|---|
| Date | `event.date` | Short date (`formatShortDate`) |
| Actor dot | `event.actor` | Originator color (`ORIGINATOR_COLOR`) |
| Kind icon | `event.kind` | `KIND_ICON` lookup |
| Title | `event.title` | Truncated to one line |
| Markers | `sig === 'critical'` ⚑ · `is_overdue` · `amount_eur` / `direction` · `claim_count` ⚖ · `note` | Shown only when present |
| Month header / today line / quiet gap | `month_buckets`, `today`, `quiet_gap_days` | Sticky month heading, dashed "today" divider, "· N quiet days ·" |
| Hover state | CSS | Document-backed rows only |

---

## 3. Interaction — click row → Document HUD

Rows with a `source_document_id` render as buttons that `navigate('/document/{id}')` — the full Document HUD (`frontend/src/features/documents/DocumentPage.tsx`, `GET /api/v1/documents/{id}/reader`). This keeps the transition from the flat chronological scan to deep semantic reading one click. Rows without a source document (proceeding milestones, costs not tied to a document) are plain, non-interactive rows.

---

## 4. Auto-fallback to Timeline

Design intent: when a case is first opened and has zero detected document relationships (e.g. only one document present), the dashboard should switch to the Timeline view to avoid showing a sparse or disconnected graph.

**Not implemented in the SPA.** `CasePage.tsx` defaults to `graph` whenever `?view` is absent, regardless of `GraphView.edge_count`.

---

## 5. Success criteria

- `TimelineTab.tsx` is the only timeline rendering surface.
- Chronological list is filterable client-side by actor, kind and future/past chips.
- Document-backed rows are clickable and open the Document HUD (`/document/{id}`).
- Empty graphs default to the timeline view for better user orientation — not in the SPA (see §4).
- Keyboard shortcuts (`l` for timeline, `g` for graph) allow fast switching.

**Automated coverage:** `tests/unit/test_case_timeline_service.py` (ordering, actor derivation, overdue flag, quiet gaps, kind mapping) and `tests/integration/test_v1_case_dashboard.py::test_graph_and_timeline` (`GET /api/v1/cases/{id}/timeline` shape). `TimelineTab` has no vitest yet.

---

## Related docs

- `docs/specs/00_vision.md` — Nav architecture
- `docs/specs/02_dashboard.md` — View mode integration
- `docs/specs/04_document_hud.md` — Reading component
- `docs/specs/06_truth_map.md` — Factual layer sibling
- `docs/specs/08_financials.md` — Cost tracking sibling
