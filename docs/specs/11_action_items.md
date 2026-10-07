# Sanctuary — Action Items & Case Clock

Companion document to `docs/specs/00_vision.md` §6 and `docs/specs/02_dashboard.md` §6. Covers the full Action Items lifecycle — extraction at ingest, panel display, status transitions, notification badges, dormancy alerting — and the Case Clock "typical duration" signal layer.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

| Layer | Status |
|---|---|
| `ActionItem` model + repo (`repositories/action_item.py`, 13 methods) | ✅ |
| `ActionItemType` enum — `deadline`, `court_date`, `response_required`, `filing_required` | ✅ |
| `ActionItemStatus` enum — `open`, `completed`, `dismissed` | ✅ |
| Frist extraction at ingest (AI analysis, `batch_analyzer.py`) | ✅ |
| Court-date extraction at enrichment (Phase 4) | ✅ |
| Deadlines panel — `frontend/src/features/cases/dashboard/Rail.tsx` (open items from `CaseDetail.action_items`, mine/all addressee toggle, 6-item cap) | ✅ |
| Open / completed / all tabs and Next Deadline sub-section | — (not in the SPA; `Rail.tsx` lists open items only) |
| Source-document link per item (item title opens the inline Review panel for the source document) | ✅ |
| `PATCH /api/v1/action-items/{item_id}` — complete / reopen / dismiss (`useCaseActionStatus` in `frontend/src/api/caseDetail.ts`) | ✅ |
| Notification count: overdue deadlines + upcoming (7d) + hearings (30d) | — (not in the SPA; see issue #186) |
| Dormancy alert (`_compute_dormancy_alert` in `app/services/case_service.py`, 90-day threshold) | ✅ |
| Keyboard `a` scrolls to action items panel | — (not in the SPA) |
| Case Clock signals (`_get_case_clock_signals` in `app/services/signals.py`) | ❌ returns `[]` — placeholder only |
| Manual action item creation UI | ❌ AI-ingest-only in v1 |
| Per-item edit (title, due date, description) | ❌ status-only PATCH |
| Action items in case-chat context | ✅ — `build_case_chat_prompt` includes top 10 open items |
| Action items in ⌘K results | — (not in the SPA; see issue #186) |

### Implementation Deviations

| Feature | Vision §6 / Dashboard §6 | Code | Status |
|---|---|---|---|
| Case-wide (not proceeding-scoped) | "All case-wide by default (not scoped to the current proceeding)" | `get_by_case(case_id)` — no proceeding filter | ✅ |
| Frist extraction | "Deadlines, court dates, response-required, filing-required" | `ActionItemType` has all four; AI writes them on ingest | ✅ |
| Source document link | "Click an item → opens source HUD" | `Rail.tsx` renders the title as a button calling `onOpenDoc(source_document_id)` when non-null | ✅ |
| Case Clock signal | "Typical duration ranges — AG 9 mo, OLG 12 mo" | `_get_case_clock_signals` placeholder returns `[]` — not yet fed by real data | ❌ — non-goal for v1 |
| Manual creation | Not specified (AI-only) | No form or POST route for manual creation | ❌ — non-goal for v1 |

---

## The core shift

**Traditional task management:** a sticky-note pad or calendar reminder, disconnected from the documents that created the obligation. The user must manually transfer dates from letters to reminders, and must remember which document the deadline came from.

**Sanctuary Action Items:** every deadline and court date is a first-class record linked to the source document that created it. The AI extracts Fristen from cover letters during ingest, and court dates from body text during enrichment. The user does not enter dates — they triage the incoming documents and the obligations appear. Clicking an action item opens the source document at the passage that established the deadline, preserving the chain of evidence.

---

## Layout overview

```
ADV-024-A  Musterklage GmbH vs. XY
┌──────────────────────────────────────────────────┐
│  DEADLINES  4                          [mine|all]  │
├──────────────────────────────────────────────────┤
│  in 3 days   24.04.2026                  ✓  ×     │
│  Klageerwiderung einreichen ↗                     │
│  in 9 days   30.04.2026                  ✓  ×     │
│  Stellungnahme Kostenantrag ↗                     │
│  in 2 weeks  05.05.2026                  ✓  ×     │
│  Anhörung AG Hamburg                              │
│  2 days ago  19.04.2026  ← overdue       ✓  ×     │
│  Antwort auf Schriftsatz RA Müller ↗              │
└──────────────────────────────────────────────────┘
```

---

## 1. Data model

```
ActionItem
  id                 INTEGER PK
  case_id            TEXT     FK → cases.id, NOT NULL, indexed
  proceeding_id      INTEGER  FK → proceedings.id, nullable, indexed
  source_document_id INTEGER  FK → documents.id, nullable, indexed
  title              TEXT     NOT NULL
  description        TEXT     nullable
  due_date           DATETIME NOT NULL, indexed
  action_type        ENUM(ActionItemType)   NOT NULL  default DEADLINE
  status             ENUM(ActionItemStatus) NOT NULL  default OPEN
  location           TEXT     nullable  (for court_date entries: "AG Hamburg, Saal 12")
  ingest_date        DATETIME NOT NULL  default now()

  INDEX ix_action_items_case_due(case_id, due_date)
  INDEX ix_action_items_due_status(due_date, status)
  INDEX ix_action_items_proceeding(proceeding_id)
```

**`ActionItemType` values:**

| Value | Meaning | Badge color |
|---|---|---|
| `deadline` | Frist — must respond or file by date | Error (red) |
| `court_date` | Verhandlungstermin / Anhörung | Tertiary |
| `response_required` | Stellungnahme erwartet | Tertiary |
| `filing_required` | Schriftsatz einzureichen | Tertiary |

**`ActionItemStatus` values:** `open` → `completed` / `dismissed`. Both transitions are reversible via `mark_open`.

---

## 2. Sources of action items

Action items are created exclusively by the AI pipeline; there is no manual creation UI in v1.

| Source | Trigger | Extractor |
|---|---|---|
| Cover-letter Frist | Ingest, `batch_analyzer.py` — AI reads the letter body for explicit Fristen | `ActionItemType.DEADLINE` |
| Court hearing date | Enrichment (`document_enricher.py`) — AI reads the full document for `Verhandlungstermin` and `Anhörung` dates | `ActionItemType.COURT_DATE` |
| Response-required notice | Enrichment — AI detects explicit "please respond by" or "Stellungnahme erwartet" language | `ActionItemType.RESPONSE_REQUIRED` |
| Filing-required notice | Enrichment — AI detects "Schriftsatz einzureichen" obligations | `ActionItemType.FILING_REQUIRED` |

`source_document_id` is always set when the AI creates the item during a document's processing. Items created before a proceeding is assigned inherit `proceeding_id=NULL` and are case-wide.

---

## 3. Status lifecycle

| Transition | Route | Who triggers |
|---|---|---|
| `open` → `completed` | `PATCH /api/v1/action-items/{id}` (`{status: "completed"}`) | User (✓ in `Rail.tsx`) |
| `open` → `dismissed` | `PATCH /api/v1/action-items/{id}` (`{status: "dismissed"}`) | User (× in `Rail.tsx`) |
| `completed` → `open` | `PATCH /api/v1/action-items/{id}` (`{status: "open"}`) | API only — no reopen control in the SPA |
| `dismissed` → `open` | `PATCH /api/v1/action-items/{id}` (`{status: "open"}`) | API only — no reopen control in the SPA |
| Created | `ActionItemRepository.create_action_item()` | AI pipeline only |

Returns the updated `CaseActionItem`; `useCaseActionStatus` invalidates the case, cases and home queries, so the Deadlines panel refetches without a reload.

---

## 4. Panel

`Rail.tsx` renders the Deadlines section in the dashboard's right rail, below the Brief, from `CaseDetail.action_items` (`GET /api/v1/cases/{case_id}`).

**Filter:** open items only; a `mine | all` toggle (local state, no round-trip) hides items addressed to someone other than the user.

**Item row:**
- Relative due date (`formatDueRelative`, red when `is_overdue`) + absolute date
- Addressee chip when the item is not addressed to the user
- ✓ / × buttons (`can_edit` only) → `useCaseActionStatus`
- Title — a button that opens the inline Review panel (`?view=review`, `DocumentReview.tsx`) for the source document when `source_document_id` is set, plain text otherwise; the full-screen HUD is one step further via "Open HUD"

**6-item cap:** the rail shows the six soonest open items; the type badge, completed/all tabs and the Next Deadline sub-section of the pre-migration panel are not in the SPA.

---

## 5. Notification badges

The notifications feed planned in issue #186 builds the counts for the sidebar badge and notifications panel — the pre-migration builder was removed with the legacy shell and nothing in the SPA renders these yet:

| Category | Query | Window |
|---|---|---|
| Overdue deadlines | All types, `status=open`, `due_date < now` | Unbounded past |
| Upcoming deadlines | All types, `status=open`, `due_date` within 7 days | Next 7 days |
| Upcoming hearings | `action_type=court_date`, `status=open`, `due_date` within 30 days | Next 30 days |
| Pending triage documents | `needs_review=True` | — |
| Overdue costs | `LegalCost` with `status=open` past `due_date` | Unbounded past |

Total badge count = sum of all five (capped at 5 per category = 25 max before the limit matters in practice).

---

## 6. Dormancy alert

`_compute_dormancy_alert(case, db)` in `app/services/case_service.py` scans all `ACTIVE` proceedings of a case and returns a warning string if any proceeding has had no document activity for more than 90 days (`DORMANCY_DAYS = 90`).

**Logic:**
1. For each active proceeding, find `max(Document.ingest_date)` for that `proceeding_id`.
2. Fall back to `proceeding.started_at` or `proceeding.ingest_date` if no documents.
3. `days_silent = (now - last_activity).days`
4. Return `"{court_name} ({az_court}) has had no activity for {N} days."` for the most-silent proceeding exceeding the threshold; otherwise `None`.

The alert string is injected into the AI Brief context and surfaces in the case dashboard's brief panel. It is not a separate UI widget.

---

## 7. Case Clock

`_get_case_clock_signals(db)` in `app/services/signals.py` is a **placeholder** that returns `[]`. The intended behavior (when implemented) is to surface signals like "ADV-024-A entering typical hearing window for AG proceedings (Jul–Nov)" based on proceeding type and elapsed time.

Case Clock signals use the same `Signal` dataclass as other dashboard signals:
```python
{
    "id": ...,
    "kind": "case_clock",
    "severity": "info",
    "title": "...",
    "detail": "...",
    "action": None,
    "link": "...",
}
```

Until the signal list is populated, no Case Clock section renders in the rail.

---

## 8. Known gaps

| Gap | Remediation |
|---|---|
| `a` shortcut (scroll to action items) | Not in the SPA; the deadlines are always visible in the rail, so no scroll target is needed |
| Case Clock signals return `[]` | Non-goal for v1; when proceeding-type durations are calibrated, implement in `_get_case_clock_signals` |
| No manual action item creation | Non-goal for v1 (see §Non-goals) |

---

## 9. Empty states

| Situation | What renders |
|---|---|
| Case with no open action items | Rail shows "No open deadlines." in muted text |
| `source_document_id` is null | Title renders as plain text instead of a link |
| No Case Clock signals | Case Clock sub-section invisible |
| No open deadlines | Panel shows "No open deadlines." |

---

## 10. Keyboard-first interaction

| Key | Scope | Action | Source |
|---|---|---|---|
| Click item title | Dashboard | Open the inline Review panel for the source document | `Rail.tsx` → `onOpenDoc(id)` → `CasePage` sets `?view=review` |
| ✓ / × | Dashboard | Mark done / dismiss | `Rail.tsx` → `useCaseActionStatus` |

---

## 11. Data sources map

| Zone | Table | Phase |
|---|---|---|
| Action Items | `action_items` | Phase 3 (Frist extraction) + Phase 4 (enrichment) |
| Case → items | `case_id` FK | Phase 1 |
| Source document | `source_document_id` FK | Phase 3/4 (ingest + enrich) |
| Proceeding | `proceeding_id` FK | Phase 3 |
| Notification counts | `action_items` + `legal_costs` | Phase 3 |

---

## 13. Phase progression

| Phase | What landed |
|---|---|
| Phase 1 | `ActionItem` schema + `case_id` FK |
| Phase 3 | Frist extraction from cover letters at ingest |
| Phase 4 | Court-date + response/filing extraction during AI enrichment |
| Phase 5 | Dashboard panel (now `Rail.tsx`) + notification badges |
| Phase 7 | Action items included in case-chat context (`build_case_chat_prompt`) |

---

## 14. Non-goals

- No manual action item creation in v1 — all items originate from AI analysis of ingested documents.
- No per-item editing (title, due date, description) — these fields are set by the AI and not editable; status is the only user-controlled field.
- No calendar export (iCal/ics) — can be added later.
- No SMS/email reminders — out of scope for a privacy-first local installation.
- No calendar overlay on the graph — action items do not appear as annotations on graph nodes.
- No cross-case action item aggregation (beyond the global notification badge) — the Action Items panel is always case-scoped.
- Case Clock "typical-duration" signals are explicitly deferred to a future phase.

---

## 15. Verification

**Manual:**
1. `make seed && make run` → open a seeded case → Action Items panel shows open deadlines; relative dates render.
2. Click an item title with a `source_document_id` → the Review tab opens on that document.
3. ✓ on an item → `PATCH /api/v1/action-items/{id}` with `status=completed`; the item leaves the Deadlines list without a reload.
4. Seed a case with `ingest_date` > 90 days ago on all documents → dormancy alert appears in the AI Brief panel.

**Automated:**
- `tests/integration/test_v1_case_dashboard.py` — `PATCH /api/v1/action-items/{item}` returns 404 for a viewer share; `tests/integration/test_v1_documents.py` — the status round-trip.
- `tests/unit/test_intelligence_action_items.py`, `tests/unit/test_action_items_gate.py` — extraction.
- No vitest coverage of the Deadlines rail yet.

---

## 16. Success criteria

- Open action items in a seeded case are visible in the panel with relative dates (type badges are not in the SPA).
- Status PATCH round-trip: mark completed → item leaves the Deadlines list; `PATCH` back to `open` returns it.
- Item titles open the Review panel on the correct source document for every item that has a `source_document_id`.
- Dormancy alert surfaces for cases with > 90 days since last document activity.
- Notification badge on the sidebar reflects current overdue + upcoming count without page reload (pending issue #186).

---

## Related docs

- `docs/specs/00_vision.md` §6 — North star for deadlines and case clock
- `docs/specs/02_dashboard.md` §6 — Dashboard integration
- `docs/specs/03_correspondence_graph.md` — Graph view (action items annotate cases, not graph nodes)
- `docs/specs/07_case_chat.md` — Case chat uses top-10 open action items as context
- `docs/specs/08_financials.md` — Overdue costs surface in the same notification badge
