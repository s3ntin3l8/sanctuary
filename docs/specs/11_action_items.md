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
| `partials/case_action_items_panel.html` — open / completed / all tabs, 12-item cap | ✅ |
| Next Deadline sub-section (earliest open item) | ✅ |
| Source-document link per item (`source ↗` button opens Document HUD) | ✅ |
| `PATCH /action-item/{item_id}/status` — complete / reopen / dismiss | ✅ |
| Notification count: overdue deadlines + upcoming (7d) + hearings (30d) (`helpers.py:99-135`) | ✅ |
| Dormancy alert (`_compute_dormancy_alert`, `case_service.py:454`, 90-day threshold) | ✅ |
| Keyboard `a` scrolls to action items panel (`dashboard.js:393-397`) | ✅ |
| `a` shortcut listed in keyboard-shortcuts modal | ❌ undocumented — see Known gap §8 |
| Case Clock signals (`_get_case_clock_signals`, `signals.py:73-80`) | ❌ returns `[]` — placeholder only |
| Manual action item creation UI | ❌ AI-ingest-only in v1 |
| Per-item edit (title, due date, description) | ❌ status-only PATCH |
| Action items in case-chat context | ✅ — `build_case_chat_prompt` includes top 10 open items |
| Action items in ⌘K results | ✅ — overdue/upcoming included in notifications bar |

### Implementation Deviations

| Feature | Vision §6 / Dashboard §6 | Code | Status |
|---|---|---|---|
| Case-wide (not proceeding-scoped) | "All case-wide by default (not scoped to the current proceeding)" | `get_by_case(case_id)` — no proceeding filter | ✅ |
| Frist extraction | "Deadlines, court dates, response-required, filing-required" | `ActionItemType` has all four; AI writes them on ingest | ✅ |
| Source document link | "Click an item → opens source HUD" | `window._dashOpenDoc(id)` via Alpine; present when `source_document_id` non-null | ✅ |
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
│  ACTION ITEMS                  [open] [completed] [all]  │
├──────────────────────────────────────────────────┤
│  [deadline]  Klageerwiderung einreichen   source ↗  │
│              in 3 days                             │
│  [deadline]  Stellungnahme Kostenantrag   source ↗  │
│              in 9 days                             │
│  [court]     Anhörung AG Hamburg                   │
│              in 2 weeks                            │
│  [response]  Antwort auf Schriftsatz RA Müller     │
│              2 days ago  ← overdue                 │
├──────────────────────────────────────────────────┤
│  ⏱  NEXT DEADLINE                                │
│     Klageerwiderung einreichen                     │
│     in 3 days  ·  24.04.2026                      │
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
| `open` → `completed` | `PATCH /action-item/{id}/status` (`status=completed`) | User (Mark done) |
| `open` → `dismissed` | `PATCH /action-item/{id}/status` (`status=dismissed`) | User (Dismiss) |
| `completed` → `open` | `PATCH /action-item/{id}/status` (`status=open`) | User (Reopen) |
| `dismissed` → `open` | `PATCH /action-item/{id}/status` (`status=open`) | User (Reopen) |
| Created | `ActionItemRepository.create_action_item()` | AI pipeline only |

Returns `204 No Content`; the panel refreshes via HTMX `hx-target="body"` out-of-band swap on the notification badge.

---

## 4. Panel

`partials/case_action_items_panel.html` renders inside the dashboard's right column, below the AI Brief.

**Three-tab filter** (`open` default / `completed` / `all`) — Alpine `x-data="{ filter: 'open' }"` state; no server round-trip on tab switch.

**Item row:**
- Type badge (error background for `deadline`; tertiary for others)
- Title (one line, truncated)
- Due date — `format_relative_time` filter ("in 3 days", "2 days ago")
- `source ↗` link if `source_document_id` is set — calls `window._dashOpenDoc(id)` to open the Document HUD

**12-item cap:** `{% for item in _items[:12] %}` — cases with many action items show the most recent 12 only; the full list is accessible from the completed tab.

**Next Deadline sub-section** (rendered below the list when `_next_item` is non-null):
- Shows a timer icon, "NEXT DEADLINE" label, item title, relative time, and absolute date (`dd.mm.yyyy`).
- `_next_item` is the earliest open `deadline` or `court_date` item for the case.

---

## 5. Notification badges

`helpers.py:99-135` builds the notification context for the sidebar badge and global notifications panel:

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

`_compute_dormancy_alert(case, db)` (`case_service.py:454-486`) scans all `ACTIVE` proceedings of a case and returns a warning string if any proceeding has had no document activity for more than 90 days (`DORMANCY_DAYS = 90`).

**Logic:**
1. For each active proceeding, find `max(Document.ingest_date)` for that `proceeding_id`.
2. Fall back to `proceeding.started_at` or `proceeding.ingest_date` if no documents.
3. `days_silent = (now - last_activity).days`
4. Return `"{court_name} ({az_court}) has had no activity for {N} days."` for the most-silent proceeding exceeding the threshold; otherwise `None`.

The alert string is injected into the AI Brief context and surfaces in the case dashboard's brief panel. It is not a separate UI widget.

---

## 7. Case Clock

`_get_case_clock_signals(db)` in `services/signals.py:73-80` is a **placeholder** that returns `[]`. The intended behavior (when implemented) is to surface signals like "ADV-024-A entering typical hearing window for AG proceedings (Jul–Nov)" based on proceeding type and elapsed time.

Case Clock signals use the same `Signal` dataclass as other dashboard signals:
```python
{"id": ..., "kind": "case_clock", "severity": "info", "title": "...", "detail": "...", "action": None, "link": "..."}
```

Until the signal list is populated, the Case Clock section in the right-column panel is empty (renders nothing due to the `{% if signals %}` guard).

---

## 8. Known gaps

| Gap | Remediation |
|---|---|
| `a` shortcut undocumented in keyboard modal | Add `a → scroll to Action Items` row to `partials/hud/_shortcuts.html` |
| Case Clock signals return `[]` | Non-goal for v1; when proceeding-type durations are calibrated, implement in `_get_case_clock_signals` |
| No manual action item creation | Non-goal for v1 (see §Non-goals) |

---

## 9. Empty states

| Situation | What renders |
|---|---|
| Case with no action items yet | Panel shows "No action items yet." in muted text |
| All items completed/dismissed, filter=`open` | "No open action items." with muted text; switch to `completed` tab to see history |
| `source_document_id` is null | No `source ↗` link appears; item row is narrower |
| No Case Clock signals | Case Clock sub-section invisible |
| No next open deadline | Next Deadline sub-section invisible |

---

## 10. Keyboard-first interaction

| Key | Scope | Action | Source |
|---|---|---|---|
| `a` | Dashboard | Smooth-scroll to `#action-items-anchor` | `dashboard.js:393-397` |
| Click "source ↗" | Dashboard | Open Document HUD for source document | `_dashOpenDoc(id)` |
| Click type badge | Dashboard | No action (badge is presentational only) | — |

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

## 12. Files that will change

**Modified (documentation cross-reference):**
- `docs/specs/02_dashboard.md §6` — collapse inline action-items description to one-paragraph summary + link to this spec.
- `docs/specs/00_vision.md §6` — add "See `docs/specs/11_action_items.md`" footnote.
- `app/templates/partials/hud/_shortcuts.html` — add `a → Action Items` row (resolves Known gap §8).

**No code changes required** beyond the keyboard-shortcut documentation fix.

---

## 13. Phase progression

| Phase | What landed |
|---|---|
| Phase 1 | `ActionItem` schema + `case_id` FK |
| Phase 3 | Frist extraction from cover letters at ingest |
| Phase 4 | Court-date + response/filing extraction during AI enrichment |
| Phase 5 | Dashboard panel (`case_action_items_panel.html`) + notification badges |
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
2. Click `[completed]` tab → completed items appear; `[open]` returns to open items.
3. Click "source ↗" on an item with a `source_document_id` → Document HUD slides in.
4. Press `a` → page smoothly scrolls to the action items panel.
5. `PATCH /action-item/{id}/status` with `status=completed` → item disappears from `open` tab; reappears in `completed`; notification badge decrements.
6. Seed a case with `ingest_date` > 90 days ago on all documents → dormancy alert appears in the AI Brief panel.

**Automated (existing):**
- `tests/unit/test_action_item_repository.py` (if present)
- `grep -rn 'ActionItem' tests/` to locate current coverage

---

## 16. Success criteria

- All action items in a seeded case are visible in the panel with correct type badges and relative dates.
- Status PATCH round-trip: mark completed → item moves to completed tab; mark open → item returns to open tab.
- `source ↗` links open the correct Document HUD for every item that has a `source_document_id`.
- Dormancy alert surfaces for cases with > 90 days since last document activity.
- Notification badge on the sidebar reflects current overdue + upcoming count without page reload.
- `a` key shortcut scrolls to the action items panel from anywhere on the case dashboard.

---

## Related docs

- `docs/specs/00_vision.md` §6 — North star for deadlines and case clock
- `docs/specs/02_dashboard.md` §6 — Dashboard integration
- `docs/specs/03_correspondence_graph.md` — Graph view (action items annotate cases, not graph nodes)
- `docs/specs/07_case_chat.md` — Case chat uses top-10 open action items as context
- `docs/specs/08_financials.md` — Overdue costs surface in the same notification badge
