# Sanctuary — Home Page

Companion document to `docs/vision.md`, `docs/triage.md`, `docs/dashboard.md`, and `docs/ingest.md`. Covers the primary landing page: cross-case priority view, the "what needs my attention right now" screen.

---

## The core shift

**Traditional legal DMS home**: recently-opened files, pinned folders, new uploads, a generic activity feed.

**Sanctuary Home**: the **case-level triage moment**. Just as the per-document triage view decides what deserves your attention out of a batch, the Home page decides which *cases* deserve your attention out of your whole portfolio.

Home answers one question: **"Given everything happening across all my cases, what should I do next?"**

It does not answer:
- "What documents do I have?" (that's ⌘K or a specific case)
- "What's the full history?" (that's a specific case's Timeline view)
- "What are all my contacts?" (that's ⌘K)

Home is temporal, decision-oriented, and ruthless about noise — cases with nothing new don't appear prominently.

---

## Layout overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Good morning, Björn.  Apr 16, 2026                                        │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ⚑ TODAY                                                         [3 items] │
│  ───────────────────────────────────────────────────────────────────────── │
│  ⚑ Apr 18 (2d)    Kostenvorschuss fällig          ADV-031-B     [open →]  │
│  ⚑ Apr 30 (14d)   Stellungnahme zu Beschluss      ADV-024-A     [open →]  │
│  · Jun 15 (60d)   Verhandlungstermin AG Hamburg   ADV-024-A     [open →]  │
│                                                                             │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ⊞ AWAITING TRIAGE                                             [1 bundle]  │
│  ───────────────────────────────────────────────────────────────────────── │
│  5 docs · 14 Apr · anwalt@kanzlei.de → ADV-024-A ?            [triage →]  │
│                                                                             │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ⚡ SINCE YOUR LAST VISIT (6 hours ago)                      [review all]  │
│  ───────────────────────────────────────────────────────────────────────── │
│  ADV-024-A   +3 docs · 1 new action · significant           [open case →]  │
│  ADV-092-B   +1 doc  · Jugendamtsbericht                    [open case →]  │
│                                                                             │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ⌛ SIGNALS                                                                 │
│  ───────────────────────────────────────────────────────────────────────── │
│  ⚠ ADV-031-B quiet 6 months — longer than typical for OLG      [check →]  │
│  ◦ ADV-024-A entering typical hearing window (Jul–Nov)        [details →]  │
│  ⚠ Gmail sync: auth expired, reconnect                        [reconnect] │
│                                                                             │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ACTIVE CASES                                              [see all →]     │
│  ───────────────────────────────────────────────────────────────────────── │
│  ┌─ ADV-024-A ──────────────────┐  ┌─ ADV-031-B ──────────────────┐        │
│  │ Musterklage GmbH vs. XY      │  │ Vane vs. Vane                │        │
│  │ ● Active · AG Hamburg        │  │ ● Active · OLG Hamburg       │        │
│  │ Status: awaiting Stellungn.  │  │ ⚠ Dormant 6 months            │        │
│  │ ⚑ Frist Apr 30               │  │ Total exposure 840 €          │        │
│  │ 1.690 €  · 3 new · 14 d      │  │ last activity Oct 2025        │        │
│  └──────────────────────────────┘  └──────────────────────────────┘        │
│                                                                             │
│  ┌─ ADV-019-C ──────────────────┐  ┌─ ADV-055-D ──────────────────┐        │
│  │ Mercury Tech IP Dispute      │  │ Smith vs. City Council       │        │
│  │ ● Active · LG Berlin         │  │ ● Active · AG Hamburg        │        │
│  │ Status: settlement talks     │  │ Status: discovery            │        │
│  │ Total exposure 12.400 €      │  │ Total exposure 3.200 €        │        │
│  │ quiet 3 weeks                │  │ 1 new · 5 d                   │        │
│  └──────────────────────────────┘  └──────────────────────────────┘        │
└────────────────────────────────────────────────────────────────────────────┘
```

Five zones, stacked vertically, in priority order: **Today → Triage → Delta → Signals → Active Cases**. The top four are "attention panels" that collapse to zero when empty; Active Cases is always rendered as the baseline.

---

## 1. Greeting bar

Minimal top strip; no page-level controls because Home doesn't need a proceeding switcher or view-mode tabs.

```
Good morning, Björn.  Apr 16, 2026
```

- Time-of-day greeting (morning/afternoon/evening based on local time)
- Today's date in short form
- That's it — **no proceeding switcher, no view mode tabs, no cross-case filter**. Home is intentionally a single view.

---

## 2. Today panel

The highest-priority cross-case list. Action items due soon, sorted by urgency.

```
⚑ TODAY                                                         [3 items]
─────────────────────────────────────────────────────────────────────────
⚑ Apr 18 (2d)    Kostenvorschuss fällig          ADV-031-B     [open →]
⚑ Apr 30 (14d)   Stellungnahme zu Beschluss      ADV-024-A     [open →]
· Jun 15 (60d)   Verhandlungstermin AG Hamburg   ADV-024-A     [open →]
```

### What appears here

Open `ActionItem` rows across **all** cases, filtered to:

- **Overdue** (due_date < now, status=open) — always shown, always critical icon
- **Due in the next 30 days** — shown by default
- Far-future items (>30 days) not shown in Today panel; visible inside each case dashboard

### Row structure

- **Urgency icon** — `⚑` critical (overdue or ≤7 days); `·` near (8–30 days)
- **Due date + relative days** — absolute + countdown
- **Action type** is implied by the icon/styling (deadline = red, court date = blue); explicit label only on hover
- **Title** — from `ActionItem.title`
- **Case chip** — the internal case ID, clickable to open the case dashboard
- **[open →]** — direct link to the source document HUD

### Empty state

If no action items due in next 30 days:

```
⚑ TODAY
─────────────────────────────────────────────────────────────────────────
  No deadlines in the next 30 days.
```

Compact; panel doesn't disappear (it's a heartbeat indicator) but takes minimal space.

---

## 3. Awaiting Triage panel

Pending triage bundles across all cases. Not individual documents — bundles, because that's the triage unit.

```
⊞ AWAITING TRIAGE                                             [1 bundle]
─────────────────────────────────────────────────────────────────────────
5 docs · 14 Apr · anwalt@kanzlei.de → ADV-024-A ?            [triage →]
3 docs · 12 Apr · (scan folder) → ?                          [triage →]
```

### What appears here

`IngestBatch` rows where `status != completed` — i.e., at least one child document still has `needs_review=True`.

### Row structure

- **Doc count** — `5 docs` in this bundle
- **Received date** — batch arrival date
- **Source hint** — sender email, or `(scan folder)`, or `(manual upload)`
- **Case suggestion** — AI-detected case with confidence indicator (`ADV-024-A ?` = suggested but unconfirmed; `ADV-024-A` = user-confirmed; `?` = no match yet)
- **[triage →]** — jump directly into the triage pane for this batch

### Empty state

Collapsed entirely when zero bundles pending.

```
⊞ AWAITING TRIAGE
─────────────────────────────────────────────────────────────────────────
  Triage queue is clear. ✓
```

Shown as a single congratulatory line.

---

## 4. Delta feed — "Since your last visit"

Cross-case delta: what's new since the user last opened the app.

```
⚡ SINCE YOUR LAST VISIT (6 hours ago)                      [review all]
─────────────────────────────────────────────────────────────────────────
ADV-024-A   +3 docs · 1 new action · significant           [open case →]
ADV-092-B   +1 doc  · Jugendamtsbericht                    [open case →]
```

### What appears here

One row per case that has at least one new `Document` since `UserSettings.settings_json["last_home_visit"]`. Rows sorted by maximum significance of the new documents (critical > significant > informational > administrative).

### Row structure

- **Case chip** — internal ID
- **Delta summary**:
  - Count of new documents (`+3 docs`)
  - Count of new action items (`1 new action`)
  - Highest significance tier of the new documents (`significant`)
  - OR the title of the single new document if exactly one (`Jugendamtsbericht`)
- **[open case →]** — jumps to the case dashboard, which will show its own delta banner for the specific documents

### The "last visit" timestamp

Stored in `UserSettings.settings_json["last_home_visit"]`. Updated to `now()` whenever the user dismisses the delta feed (via `[review all]` or an explicit dismiss) OR navigates away from Home. This means opening Home passively doesn't "count" as reviewing — only explicit actions advance the timestamp.

### Empty state

```
⚡ SINCE YOUR LAST VISIT
─────────────────────────────────────────────────────────────────────────
  Nothing new since your last visit.
```

---

## 5. Signals panel

Ambient alerts that don't fit the other panels — dormancy warnings, Case Clock predictions, system status.

```
⌛ SIGNALS
─────────────────────────────────────────────────────────────────────────
⚠ ADV-031-B quiet 6 months — longer than typical for OLG      [check →]
◦ ADV-024-A entering typical hearing window (Jul–Nov)        [details →]
⚠ Gmail sync: auth expired, reconnect                        [reconnect]
⚠ 2 documents stuck in ingest_status=FAILED                  [review →]
```

### Signal types

| Signal | Source | Action |
|---|---|---|
| **Dormancy alert** | Proceeding silent longer than Case Clock typical range | Opens case dashboard |
| **Case Clock window** | Entering a typical-event window (hearing, ruling, etc.) | Shows the Case Clock prediction detail |
| **Gmail sync issue** | OAuth expired/revoked or sync failing | Opens Gmail settings to reconnect |
| **Ingest failures** | N documents with `ingest_status=FAILED` | Opens triage with failed filter |
| **Ingest backfill progress** | Bulk Gmail backfill running | Shows progress bar |
| **AI provider unreachable** | Ollama/LM Studio/OpenAI endpoint failing | Opens AI provider settings |

### Severity

- `⚠` warning — requires user action soon
- `◦` neutral — informational, no action required but worth knowing

### Empty state

When no signals:

```
⌛ SIGNALS
─────────────────────────────────────────────────────────────────────────
  All systems quiet.
```

---

## 6. Active Cases strip

Baseline view: every non-closed case gets a compact status card, always rendered.

```
ACTIVE CASES                                              [see all →]
─────────────────────────────────────────────────────────────────────────
┌─ ADV-024-A ──────────────────┐  ┌─ ADV-031-B ──────────────────┐
│ Musterklage GmbH vs. XY      │  │ Vane vs. Vane                │
│ ● Active · AG Hamburg        │  │ ● Active · OLG Hamburg       │
│ Status: awaiting Stellungn.  │  │ ⚠ Dormant 6 months            │
│ ⚑ Frist Apr 30               │  │ Total exposure 840 €          │
│ 1.690 €  · 3 new · 14 d      │  │ last activity Oct 2025        │
└──────────────────────────────┘  └──────────────────────────────┘
```

### Card content

- **Internal ID** (lead identifier)
- **Title** (1 line, truncated)
- **Status dot + active proceeding** — `● Active · AG Hamburg`
- **One-line status** — first line from `Case.ai_brief.status_line`; graceful degradation if brief not generated yet (shows "No brief yet")
- **Next action** — closest open `ActionItem` (icon + date)
- **Compact metric row** — `total_cost_exposure · new_docs_since_last_visit · days_since_last_activity`

### Ordering

1. Cases with unreviewed delta (green dot = "new stuff")
2. Cases with imminent deadlines (within 14 days)
3. Cases with dormancy alerts
4. All other active cases, by most-recently-active

### What's NOT on a card

- Court Az (that's context, shown inside the case dashboard)
- Document list (never a list)
- Explicit "archive" / "close" actions (those live in the case dashboard)

### Empty state

```
ACTIVE CASES
─────────────────────────────────────────────────────────────────────────
  No active cases yet. [+ add new case]
```

---

## 7. "You're caught up" composite empty state

When all four attention panels (Today, Triage, Delta, Signals) are empty AND every active case is silent:

```
✓ You're caught up.

No deadlines this month.
No bundles pending triage.
Nothing new since your last visit.
All signals quiet.

Your active cases are steady. Go get coffee.
```

This is the *desired* end state — not a failure mode. The fact that Home can reach this state is part of its design; contrast with a traditional DMS where something always demands attention.

Active Cases strip still renders below so the user can tap into a specific case if they want.

---

## 8. Attention scoring

What does "deserves attention" mean concretely? Each action item, bundle, case, and signal has an implicit or explicit attention score used for ordering within its panel.

### Action items (Today panel)

```
score = f(days_until_due, action_type, significance_of_source_doc)
```

- Overdue: always highest (sorted by most-overdue first)
- Due today / tomorrow: next
- Due this week: next
- Due this month: next
- Within each tier: `critical` source docs > `significant` > `informational`

### Triage bundles

```
score = f(age, doc_count, suggested_case_confidence)
```

- Older batches > newer (older means more stale / neglected)
- Batches with no case suggestion rise — user must decide
- Larger batches > smaller (more work blocking)

### Delta feed

```
score = max_significance_tier_of_new_docs(case)
```

- Cases with `critical` new docs appear first
- Ties broken by count, then recency

### Signals

```
fixed severity ordering: system-failure > dormancy > Case Clock window
```

System issues (Gmail auth, AI provider, ingest failures) always rank above case-specific signals, because they block everything else.

### Active Cases strip

```
score = f(has_delta, imminent_deadline, dormancy, last_activity)
```

Priority tiers described in §6. Within each tier, most-recently-active first.

---

## 9. Keyboard-first interaction

Home is designed to be navigable without touching the mouse.

| Key | Action |
|---|---|
| `j` / `k` | Next / previous item in current panel |
| `Enter` | Open the highlighted item |
| `t` | Jump to Today panel |
| `i` | Jump to Awaiting Triage panel (I for Inbox) |
| `d` | Jump to Delta feed |
| `s` | Jump to Signals |
| `c` | Jump to Active Cases strip |
| `/` | Focus ⌘K palette (alternate shortcut) |
| `?` | Show keyboard cheat sheet |
| `r` | Mark delta reviewed (advance last-visit timestamp) |

Shift+key jumps to the *end* of each panel (e.g., `Shift+T` = last item in Today).

---

## 10. Data sources map

| Zone | Source | Populated by phase |
|---|---|---|
| Greeting / date | local system clock | — |
| Today panel | `ActionItem` rows across cases, `due_date <= now + 30d`, `status=open` | Phase 1/3 |
| Awaiting Triage panel | `IngestBatch` rows with `status != completed` | Phase 3 |
| Delta feed row per case | compare `Document.created_at` vs. `UserSettings.last_home_visit` per case | Phase 5 |
| Delta row significance | max `Document.significance_tier` of new docs | Phase 4 |
| Signals — dormancy | `Proceeding` silence vs. Case Clock typical range | Phase 5 |
| Signals — Case Clock windows | derived per proceeding + `ActionItem` history | Phase 5 |
| Signals — Gmail auth | OAuth token state | Phase 3 |
| Signals — ingest failures | `Document.ingest_status=FAILED` count | Phase 1 |
| Active Cases card — status line | `Case.ai_brief.status_line` | Phase 5 |
| Active Cases card — next action | closest open `ActionItem` by case | Phase 1/3 |
| Active Cases card — exposure | `Case.total_cost_exposure` | Phase 4 |
| Active Cases card — delta | new doc count since last visit | Phase 5 |
| Last-visit timestamp | `UserSettings.settings_json["last_home_visit"]` | Phase 5 |

---

## 11. Empty-state philosophy

Each panel has its own "nothing to show" message that is **short, non-apologetic, and lets the panel collapse to ~1 line**. The common pitfall with dashboard-style pages is that empty states either feel like failures ("No data yet 😞") or take too much vertical space with illustrations and CTAs.

Sanctuary's rule: **empty is a success signal, not a failure.** A quiet panel is good news — something is not demanding attention.

| Panel | Empty-state text |
|---|---|
| Today | `No deadlines in the next 30 days.` |
| Awaiting Triage | `Triage queue is clear. ✓` |
| Delta feed | `Nothing new since your last visit.` |
| Signals | `All systems quiet.` |
| Active Cases | `No active cases yet. [+ add new case]` |
| Composite (all empty) | `✓ You're caught up. Go get coffee.` |

No illustrations, no empty-state graphics, no upsells. One line each.

---

## 12. What Home is NOT

Explicit non-goals — these would all pull Home back toward a DMS mental model.

- **Not a global document list.** There's no "recent documents" feed. Documents live inside cases; if you want a specific doc, ⌘K.
- **Not a global timeline.** No chronological feed of every document ingested. Timeline lives per-case as a view mode.
- **Not a notifications page.** Notifications are reactive, surfaced via the rail's 🔔 button. Home is proactive — it pulls things forward; it doesn't catalog alerts.
- **Not a settings page.** Gmail setup, AI provider config, etc. all live under Settings (rail ⚙). Home only *surfaces* when these need attention via the Signals panel.
- **Not a case directory.** The Active Cases strip is a scoped overview, not a browser. The full case directory is `/cases`.
- **Not an AI chat surface.** Chat is case-scoped (case dashboard) or document-scoped (document HUD). Home has no chat panel — "ask AI (global)" is available in ⌘K for rare cross-case questions.
- **Not a reporting surface.** Exports, printouts, sharable summaries belong in a separate Reports view (out of scope for v1).

---

## 13. Files to create / modify

### New

| File | Purpose |
|---|---|
| `app/api/home.py` | Home route: aggregated context for all panels |
| `app/services/home_service.py` | Cross-case aggregation: today's actions, pending triage, delta, signals |
| `app/services/attention_scoring.py` | Scoring functions per panel (§8) |
| `app/services/signals.py` | Signal detection: dormancy, Case Clock, system health |
| `app/templates/pages/home.html` | Main Home template |
| `app/templates/partials/home/today_panel.html` | Today panel |
| `app/templates/partials/home/triage_panel.html` | Awaiting Triage panel |
| `app/templates/partials/home/delta_feed.html` | Delta feed |
| `app/templates/partials/home/signals_panel.html` | Signals panel |
| `app/templates/partials/home/active_case_card.html` | Single case card |
| `app/templates/partials/home/caught_up.html` | Composite empty state |
| `static/js/home.js` | Keyboard navigation, last-visit timestamp update |

### Modified

| File | Change |
|---|---|
| `app/api/dashboard.py` | Either renamed to `home.py` or slimmed down to the global dashboard function only |
| `app/templates/partials/sidebar.html` | Replaced by rail per navigation architecture (separate effort) |
| `app/services/case_service.py` | Extend `get_dashboard_stats` to return structured Home context |
| `app/models/database.py` | (no new columns — Phase 1 already provisioned everything) |

---

## 14. Phase progression map

Home lights up progressively as backing systems come online.

| Phase | What Home shows |
|---|---|
| **Phase 2** (triage) | Today panel (from `ActionItem`), Awaiting Triage panel, Active Cases strip (with basic status — no AI brief yet), partial Signals (ingest failures only). No Delta feed, no Case Clock signals. |
| **Phase 3** (email ingest) | Awaiting Triage fills up properly with real batches; Gmail sync signals start appearing. |
| **Phase 4** (document intelligence) | Delta feed starts showing significance tiers; Active Cases cards start showing better next-action data. |
| **Phase 5** (case AI brief) | Active Cases status lines populate from `Case.ai_brief`; Case Clock signals appear; delta feed shows true significance-weighted ordering. |
| **Phase 6+** | No changes to Home — it's a Phase 5 feature that completes at Phase 5. Truth Map and Chat are case-scoped and don't surface on Home. |

---

## 15. Success criteria

Home is done when:

- **Time-to-orient ≤5 seconds**: opening the app and glancing at Home tells you what needs attention without scrolling or clicking.
- **Ruthless noise filtering**: cases with nothing new don't appear in the attention panels. An inbox with 900 documents and 4 active cases still produces a Home screen that fits on one laptop viewport.
- **"You're caught up" is reachable**: a user who has just triaged everything and responded to all deadlines sees the composite empty state. Not aspirational — a real daily occurrence.
- **Keyboard-only navigation works**: `j/k` through every panel, `Enter` opens the highlighted item, zero mouse needed.
- **Delta timestamp semantics are correct**: opening Home passively doesn't advance the last-visit timestamp; only explicit "review" or navigation does. Bouncing in and out of Home doesn't hide new items.
- **Panels collapse when empty**: no "No data" decorations taking up vertical space; one-line empty states only.
- **Signals surface real issues**: Gmail auth issue, failed ingests, dormant cases all reach the user through Signals, not buried in a settings menu.
- **Active Cases cards reflect the AI brief**: once Phase 5 lands, status lines on cards match the one-line summary you'd get from opening the case.

---

## Related docs

- `docs/vision.md` — north-star architecture, navigation architecture section
- `docs/triage.md` — where the Awaiting Triage link goes
- `docs/dashboard.md` — where the case-level links go
- `docs/ingest.md` — how bundles arrive in the first place
