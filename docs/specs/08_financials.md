# Sanctuary — Financials

Companion document to `docs/specs/00_vision.md` §8 and `docs/specs/02_dashboard.md` §5/§9. Covers the full cost-tracking surface: per-document cost signals, case-level exposure, and the statutory line-item ledger.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟡 IMPLEMENTED — four CRUD routes, promote-to-cost bug, direction inversion, and per-proceeding split require remediation

| Layer | Status |
|---|---|
| `LegalCost` table + `LegalCostRepository` + `CostService` + `CostSummary` | ✅ |
| `Document.cost_delta` JSON + `CostDeltaSchema` validation | ✅ |
| `Document.cost_candidates` regex pre-extraction at ingest | ✅ |
| `Case.total_cost_exposure` (cents) + `recompute_total_cost_exposure` rollup | ✅ |
| `GET /costs` global cross-case browser | ✅ read-only |
| `GET /costs/new` + `GET /costs/cases/{id}/new` cost form | ✅ |
| Dashboard left-column `case_financials_panel.html` | ✅ partial — missing last-delta line and breakdown link |
| Dashboard `partials/dashboard/financials_view.html` view-mode tab | ✅ partial — no per-proceeding grouping |
| HUD `partials/hud/_cost_delta.html` + promote-to-cost button | ✅ partial — promote-to-cost has IntegrityError bug |
| `POST /costs` (create) | ❌ template posts to it; route absent |
| `POST /costs/{id}/update-field` (inline edit) | ❌ template posts to it; route absent |
| `POST /costs/{id}/pay` | ❌ template posts to it; route absent |
| `POST /costs/{id}/reimburse` | ❌ template posts to it; route absent |
| Per-proceeding cost split | ❌ no `proceeding_id` on `LegalCost` |
| Direction semantics consistent between prompt and HUD | ⚠ inverted — `prompts.py:35` and `_cost_delta.html` disagree |
| Overdue/upcoming alerts passed to `/costs` route | ⚠ template renders them; route does not pass them in |
| `app/templates/components/cost_row.html` | ⚠ unused macro file — deleted in this remediation |

### Implementation Deviations

| Feature | Vision §8 / Dashboard §5 | Code | Status |
|---|---|---|---|
| `cost_delta` JSON shape | `{amount, direction, description}` | Same; validated by `CostDeltaSchema` | ✅ |
| Cumulative `Case.total_cost_exposure` | "recomputed on ingest" | Σ\|amount\| via `recompute_total_cost_exposure`; trigger inside enrich task | ✅ Accepted — absolute value means signalled exposure always grows; direction is shown per-document |
| "No synthetic probability" | factual amounts only | RVG/GKG/JVEG statutory positions + §91 ZPO flag; no prediction | ✅ |
| Per-proceeding split in Financials view | "Includes a per-proceeding split" (dashboard §9) | Not implemented | ❌ Remediated — add nullable `proceeding_id` FK to `LegalCost` |
| `+450 € (Apr 02)` last-delta line in panel | dashboard §5 panel mock | Panel shows totals only | ❌ Remediated |
| `[breakdown →]` link in panel | dashboard §5 panel | Not present | ❌ Remediated |
| §91 ZPO reimbursable flag | mentioned | `is_reimbursable: Boolean, default=True` + `ANWALTSKOSTEN_GEGNER` category | ✅ |
| Paid vs. outstanding tracking | mentioned | Per-row `amount_paid`/`amount_reimbursed` + `CostStatus` + aggregated `total_outstanding` | ✅ |

---

## The core shift

**Traditional approach:** maintain a cost spreadsheet in parallel with the case file. Enter amounts manually after receiving invoices. Reconcile §91 ZPO claims by hand at the end.

**Sanctuary Financials:** every document that mentions a specific financial amount is flagged by the AI at ingest with a `cost_delta` — a lightweight signal extracted from the document text. The user can then *promote* that signal to a statutory line item (`LegalCost`) with the correct RVG/GKG position, VAT rate, and §91 ZPO reimbursability flag. The result is a living cost ledger that starts automatically and is refined by the user, not the reverse.

Two numbers coexist deliberately:

- **Signalled exposure** (`Case.total_cost_exposure`) — the running sum of absolute cost_delta amounts from documents. Grows with every new document that mentions money. It is what the documents *claim* is happening financially.
- **Booked total** (`cost_summary.total_gross` from `LegalCost` rows) — the formal statutory ledger. It is what has been *entered and categorised* as a real cost position.

These two are not equal and should not be forced to reconcile. The panel shows Signalled exposure. The breakdown tab shows Booked. Both are labelled explicitly.

---

## Layout overview

### Surface 1 — Left-column panel (case dashboard)

```
FINANCIALS
Signalled:   1.690 €
             ┌─────────┬────────────┬──────────────┐
             │  Gross  │    Open    │ Reimbursable │
             │ 1.240 € │   890 €    │    450 €     │
             └─────────┴────────────┴──────────────┘
Last delta:  +450 €  Beschluss PKH  02.04  →
[breakdown →]
```

### Surface 2 — Financials view-mode tab (case dashboard main area)

```
FINANCIALS  ·  RVG / GKG BREAKDOWN

[All proceedings ▾]     Booked: 1.240 €   Paid: 350 €   Open: 890 €   §91: 450 €

── AG Hamburg  003 F 426/25 ────────────────────────────────────────────────────
 RVG  Verfahrensgebühr 1. Instanz   Nr. 3100   1.300 € netto   → 1.547 € brutto  [Offen]
 GKG  Gerichtskostenvorschuss       KV Nr. 1210   300 €                           [Bezahlt]
                                          subtotal: 1.600 € gross   paid: 300 €

── Case-level (no proceeding) ───────────────────────────────────────────────────
 §91  Anwaltskosten Gegner          —     450 €                                   [Strittig]

──────────────────────────────────────────────────────────────────────────────────
                                      Total: 1.240 € booked   Reimbursable: 450 €

Statutory basis:
  RVG Verfahrensgebühr Nr. 3100 VV RVG · GKG Vorschuss KV GKG Nr. 1210
  §91 ZPO — loser-pays reimbursement · JVEG — expert / interpreter fees
```

### Surface 3 — Per-document HUD sidebar (`_cost_delta.html`)

```
COST SIGNAL
Beschluss vom 02.04 introduces:
  + 450 €  Gerichtskostenvorschuss        [incoming — money owed to us]
  Gerichtsgebühren nach GKG

[+ promote to LegalCost]   → creates a VORSCHUSS line item in the ledger

Candidates from text:  450 €  · 1.240 EUR  · Nr. 3100 VV RVG
```

### Surface 4 — Global `/costs` page

Cross-case browser. Per-case sections with subtotals. Overdue and upcoming alerts at top. `[+ Add Cost]` for manual entry. Not case-scoped — shows all cases.

---

## 1. Data model — `Document.cost_delta`

```
cost_delta: JSON | null

{
  "amount":    float,          // amount in EUR (always positive)
  "direction": "incoming|outgoing|ruling|none",
  "description": "human-readable label"
}
```

Validated by `CostDeltaSchema` (`app/models/schemas.py:39-46`). Invalid direction collapses to `"none"` at write time.

**Direction semantics** (canonical definition — UI, prompt, and display must all agree):

| Direction | Meaning | HUD display |
|---|---|---|
| `incoming` | Money received or owed **to us** (Erstattung, Zahlung, PKH-Bewilligung) | `+` green, `trending_up` |
| `outgoing` | Money **we must pay** (Gebühr, Vorschuss, Kostenfestsetzung gegen uns) | `−` red, `trending_down` |
| `ruling` | Court-determined amount (Kostenfestsetzungsbeschluss, neither side yet pays) | amber, `gavel` |
| `none` | No specific financial direction; amount is mentioned | neutral |

**⚠ Known gap — direction inversion:** the AI enricher prompt (`app/services/intelligence/prompts.py:35`) currently defines `"incoming" = money we must pay, "outgoing" = money we are owed/claiming` — the opposite of the HUD template and of common sense. **Fix:** update `prompts.py:35` to match the canonical definition above. Do not re-run historic enrichments; the inversion is recorded here for future audits.

`Case.total_cost_exposure` uses `|amount|` (absolute value) regardless of direction — both income and outgoing cost signals add to the exposure figure. This is intentional: the exposure is "money in motion", not a net position.

---

## 2. Data model — `Document.cost_candidates`

```
cost_candidates: JSON | null

[
  {"type": "amount",       "value": float,  "context": "surrounding text"},
  {"type": "rvg_position", "value": "Nr. 3100 VV RVG", "context": "..."}
]
```

Regex-extracted at ingest before AI enrichment (`app/services/ingestion/service.py:186-225`):
- EUR pattern: `\d{1,3}(?:\.\d{3})*(?:,\d{2})?` followed by `EUR|€|euros?`; filtered to 10 < amount < 1,000,000
- RVG position pattern: `Nr\.?\s*\d{1,4}\s*VV\s*RVG|KV\s*GKG\s*Nr\.?\s*\d+|§\s*\d+\s*ZPO`
- Capped at 20 candidates per document

`cost_candidates` is the fallback when `cost_delta` is null — the HUD sidebar renders the first 5 candidate amounts with their context.

---

## 3. Data model — `Case.total_cost_exposure`

```
total_cost_exposure: Integer, default=0   // stored in cents
```

Recomputed by `recompute_total_cost_exposure(case_id, db)` (`app/services/case_service.py:125-167`):
- Sums `|cost_delta.amount|` across all documents with non-null `cost_delta` for the case
- Skips `case_id == "_TRIAGE"` (pseudo-case for un-routed documents)
- Stores `int(round(total_euros * 100))` in cents
- **Commits itself** — callers do not need an additional `db.commit()`
- Triggered by the enrich Celery task after any document with a `cost_delta` is enriched; failure is logged but does not block the enrich pipeline

Display: always divide by 100.0 before passing to `format_eur()`.

---

## 4. Data model — `LegalCost`

Source: `app/models/database.py:510-572`.

```
id                  Integer PK
case_id             String FK→cases.id, NOT NULL, indexed
proceeding_id       Integer FK→proceedings.id, nullable  ← added in remediation migration

category            CostCategory, NOT NULL
status              CostStatus, default=OFFEN, NOT NULL, indexed

title               String, NOT NULL          // "Verfahrensgebühr 1. Instanz"
rvg_position        String, nullable          // "Nr. 3100 VV RVG"

amount_net          Float, NOT NULL           // Nettobetrag (EUR)
vat_rate            Float, default=0.0        // 0.19 for lawyer; 0.0 for court/expert
amount_gross        Float, NOT NULL           // Bruttobetrag = net * (1 + vat_rate)
amount_paid         Float, default=0.0
amount_reimbursed   Float, default=0.0

streitwert          Float, nullable           // Streitwert basis for this position
gebuehren_faktor    Float, nullable           // RVG factor e.g. 1.3 for Verfahrensgebühr
is_reimbursable     Boolean, default=True     // Erstattungsfähig nach §91 ZPO

issued_at           DateTime, nullable, indexed   // Rechnung / Kostenfestsetzung
due_at              DateTime, nullable, indexed   // Fälligkeitsdatum
paid_at             DateTime, nullable, indexed
source_document_id  Integer FK→documents.id, nullable
notes               Text, nullable
ingest_date         DateTime

indexes: ix_legal_costs_case_status (case_id, status)
         ix_legal_costs_status_due (status, due_at)
         ix_legal_costs_case_proceeding (case_id, proceeding_id)  ← added in remediation
```

### `CostCategory` enum — `app/models/enums.py:72-84`

| Value | Label | Short | Statutory basis |
|---|---|---|---|
| `gerichtskosten` | Gerichtskosten | GKG | Gerichtskostengesetz |
| `anwaltskosten` | Eigene Anwaltskosten | RVG | Rechtsanwaltsvergütungsgesetz |
| `anwaltskosten_gegner` | Anwaltskosten Gegner (§91) | §91 | ZPO §91 loser-pays claim |
| `sachverstaendiger` | Sachverständigenkosten | JVEG | Justizvergütungs- und -entschädigungsgesetz |
| `vorschuss` | Gerichtskostenvorschuss | Vorschuss | GKG Anlage 1 |
| `vollstreckung` | Vollstreckungskosten | Vollstr. | ZPO §788 |
| `auslagen` | Auslagen | Auslagen | RVG Nr. 7000 ff. |
| `sonstiges` | Sonstiges | — | — |

### `CostStatus` enum — `app/models/enums.py:87-94`

| Value | Label | Color |
|---|---|---|
| `offen` | Offen | error red |
| `bezahlt` | Bezahlt | own green |
| `erstattet` | Erstattet (§91) | primary |
| `teilweise` | Teilweise | amber |
| `strittig` | Strittig | error red |

Constants: `COST_CATEGORY_META` and `COST_STATUS_META` in `app/constants.py:52-113` provide `{label, short, color}` maps threaded into templates as `cost_category_meta` / `cost_status_meta`.

---

## 5. Statutory framing

German legal costs are governed by four statutes. Sanctuary tracks all four without enforcing any particular calculation — amounts are entered by the user or extracted from documents.

| Statute | Scope | Key metric |
|---|---|---|
| **RVG** — Rechtsanwaltsvergütungsgesetz | Own lawyer fees | Streitwert + Gebührenfaktor (e.g. 1.3 × Verfahrensgebühr) |
| **GKG** — Gerichtskostengesetz | Court filing fees and Vorschuss | Streitwert-based, KV GKG table |
| **JVEG** — Justizvergütungs- und -entschädigungsgesetz | Expert witnesses, interpreters | Hourly or per-page rates |
| **§§91–107 ZPO** | Cost allocation ("loser pays") | Winning party recovers costs from losing party; `is_reimbursable` flag |

The `ANWALTSKOSTEN_GEGNER` category is the explicit §91 ZPO claim line — the amount the opposing side owes us (or we owe them) under the cost order.

---

## 6. Two-stage cost extraction

### Stage 1 — Regex at ingest (`extract_cost_candidates`)

Runs synchronously in the ingest pipeline before AI enrichment. Populates `Document.cost_candidates`. Fast, zero-cost, catches amounts the AI might miss. Capped at 20 candidates.

### Stage 2 — AI enrichment (`document_enricher.py:179-194`)

The enricher prompt (`prompts.py:33-36`) asks the AI to identify *the most significant* financial amount in the document and assign it a direction. Result is validated by `CostDeltaSchema`. Invalid direction collapses to `"none"`.

**After remediation:** the enricher prompt direction definitions must match the canonical table in §1 (incoming = received/owed to us).

### Trigger chain

```
Document ingested → enrich_document_task runs
  → doc.cost_delta written
  → _trigger_cost_rollup()
      → recompute_total_cost_exposure(case_id, db)
          → Case.total_cost_exposure updated (cents)
```

The rollup runs at the end of the enrich task on both success and failure paths. Exceptions in the rollup are logged but do not fail the enrich pipeline.

---

## 7. Per-document HUD sidebar

Source: `app/templates/partials/hud/_cost_delta.html`.

Renders if `doc.cost_delta` is not null. Falls back to `doc.cost_candidates` (first 5 entries) if `cost_delta` is null but candidates exist.

```
COST SIGNAL

  + 450 €   Gerichtskostenvorschuss          [incoming]
             "Kostenfestsetzung nach GKG..."

Candidates: 450 €  · 1.240 EUR  · Nr. 3100 VV RVG
```

Direction → display mapping:

| Direction | Sign | Color | Icon |
|---|---|---|---|
| `incoming` | `+` | `text-originator-own` green | `trending_up` |
| `outgoing` | `−` | `text-error` red | `trending_down` |
| `ruling` | none | amber | `gavel` |
| `none` | none | secondary | — |

**`[+ promote to LegalCost]`** button — `POST /document/{doc_id}/cost-from-delta` (`app/api/documents.py:600-634`). Creates a `LegalCost` row from the `cost_delta`. Only shown when `doc.case_id` is set (i.e., document is not in the `_TRIAGE` pseudo-case).

**⚠ Known gap — `amount_gross` NOT NULL:** the current `promote_cost_delta` route sets `amount_net = cost_delta.amount` but does not set `amount_gross`, which is NOT NULL — this raises an `IntegrityError` on commit. Fix: compute `amount_gross` at promotion:
- Direction `outgoing`, category `SONSTIGES` (default): `amount_gross = amount_net * 1.19` (standard Anwalt VAT)
- Direction `ruling` or `incoming`: `vat_rate = 0.0`, `amount_gross = amount_net` (court fees are net)
- Expose category and vat_rate as optional query params to the promote endpoint so the user can override.

**⚠ Known gap — rollup not called after promote:** `promote_cost_delta` does not call `recompute_total_cost_exposure`. Fix: call it after the `LegalCost` row is committed (it commits internally — call after `db.commit()`).

---

## 8. Dashboard left-column panel

Source: `app/templates/partials/case_financials_panel.html`.

### Current state (incomplete)

Renders `Case.total_cost_exposure / 100.0` as the big number plus a 3-cell grid (Gross / Open / Reimbursable from `cost_summary`).

### After remediation

```
FINANCIALS
Signalled:   1.690 €          ← Case.total_cost_exposure
             ┌──────────┬─────────────┬────────────────┐
             │  Gross   │    Open     │ Reimbursable   │
             │ 1.240 €  │   890 €     │    450 €       │
             └──────────┴─────────────┴────────────────┘
Last delta:  + 450 €  Beschluss PKH  02.04  →          ← link opens HUD
[breakdown →]                                           ← setView('fin')
```

- **"Signalled"** label on the big number (to distinguish it from the Booked total in the breakdown tab).
- **Last delta row** — pull the most recent document with a non-null `cost_delta` for the case from the dashboard context (add to `CaseDashboardService.build_context`); display `format_eur(amount)` with direction sign, document title, and `issued_date`. Clicking the title opens the document HUD.
- **`[breakdown →]`** — an `hx-on:click="setView('fin')"` Alpine call that switches the main area to the Financials view-mode tab.

---

## 9. Financials view-mode tab

Source: `app/templates/partials/dashboard/financials_view.html`.

Activated by `?view=fin` or keyboard `$`. Keyboard shortcut already in `dashboard.js:375`.

### Current state (incomplete)

Renders the full case cost list without per-proceeding grouping. Subtotals exist at the bottom.

### After remediation

```
FINANCIALS  ·  RVG / GKG BREAKDOWN          [All proceedings ▾]

Booked: 1.547 €    Paid: 300 €    Open: 1.247 €    §91 ZPO: 450 €

── AG Hamburg  003 F 426/25 ─────────────────────────────────
 RVG  Verfahrensgebühr  Nr. 3100  1.300 netto  1.547 brutto   [Offen]
 GKG  Vorschuss         KV 1210     300 netto    300 brutto   [Bezahlt]
                                          subtotal: 1.847 €   paid: 300 €

── Case-level (no proceeding) ───────────────────────────────
 §91  Anwaltskosten Gegner           450 netto    450 brutto  [Strittig]
                                          subtotal:   450 €

──────────────────────────────────────────────────────────────
              Booked total: 1.547 €   Reimbursable: 450 €
```

**Proceeding grouping:** costs with `proceeding_id` set are grouped under their proceeding name + Az. Costs without `proceeding_id` fall into "Case-level (no proceeding)". The `[All proceedings ▾]` dropdown filters to the active proceeding or shows all.

**Reuses:** `partials/cost_row.html` per row (not `components/cost_row.html` — the latter is unused and will be deleted).

---

## 10. Cost CRUD routes

Source (current): `app/api/costs.py`.

**⚠ Known gap:** four endpoints are missing. All templates post to them; all return 404/405 today.

### `POST /costs`

Create a new `LegalCost` from the `cost_form.html` form. Returns the new row as a `cost_row.html` partial swapped into `#cost-form-container`. After commit, call `recompute_total_cost_exposure(case_id, db)`.

Required form fields: `case_id`, `proceeding_id` (optional), `category`, `title`, `amount_net`, `vat_rate`, `amount_gross`, `issued_at`.

### `POST /costs/{cost_id}/update-field`

Inline edit — update a single field on an existing row. Form body: `field=<name>&value=<value>`. Returns the updated `cost_row.html` partial. After commit, call `recompute_total_cost_exposure`.

### `POST /costs/{cost_id}/pay`

Sets `amount_paid = amount_gross`, `status = BEZAHLT`, `paid_at = now`. Returns updated `cost_row.html`. Cross-case guard: 404 if the row's `case_id` does not match. Call `recompute_total_cost_exposure` after commit.

### `POST /costs/{cost_id}/reimburse`

Sets `amount_reimbursed = amount_gross`, `status = ERSTATTET`. Returns updated `cost_row.html`. Same cross-case guard and rollup call.

All four routes follow the pattern: `thin route → CostService(db) → LegalCostRepository → db.commit() → recompute_total_cost_exposure → return partial`.

---

## 11. Per-proceeding split

**⚠ Known gap — schema:** `LegalCost` has no `proceeding_id`. Required changes:

### Migration

Add nullable `proceeding_id` FK and index to `legal_costs`:

```python
# alembic migration: add_proceeding_id_to_legal_costs.py
op.add_column("legal_costs", sa.Column("proceeding_id", sa.Integer(),
    sa.ForeignKey("proceedings.id"), nullable=True))
op.create_index("ix_legal_costs_case_proceeding", "legal_costs",
    ["case_id", "proceeding_id"])
```

Existing rows will have `proceeding_id = NULL` — they fall into the "Case-level" group in the UI.

### `promote_cost_delta` — inherit from source document

When promoting a `cost_delta` to a `LegalCost`, inherit `source_document.proceeding_id` as the `LegalCost.proceeding_id`. If the document has no proceeding, the cost is case-level.

### Repository

`LegalCostRepository.get_by_case(case_id, proceeding_id=None)`:
- If `proceeding_id` is given, filter `WHERE proceeding_id = :pid`.
- If `None`, return all (for the breakdown view) or `WHERE proceeding_id IS NULL` (for "Case-level" group).

### Cost form

Add a proceeding dropdown to `cost_form.html`, defaulting to the active proceeding from the dashboard. Optional — user can clear it to create a case-level cost.

---

## 12. Single source of truth — reconciliation policy

Two numbers appear on the dashboard and are **intentionally different**:

| Number | Source | Label in UI | Meaning |
|---|---|---|---|
| `Case.total_cost_exposure` | Σ\|doc.cost_delta.amount\| in cents | **"Signalled"** (panel big number) | What documents claim is financially at stake |
| `cost_summary.total_gross` | Σ LegalCost.amount_gross | **"Booked"** (breakdown tab header) | Formal statutory line items entered into the ledger |

The two diverge when:
- Documents signal costs that haven't been promoted to `LegalCost` rows yet.
- `LegalCost` rows are entered manually without a source document.
- A `cost_delta` is promoted but uses a different amount than the document's signal.

**No forced reconciliation.** Micro-copy on the panel explains the distinction ("Signalled exposure — documents indicate. Booked → for the statutory ledger."). The user decides when to promote a signal into a booked position.

---

## 13. Global `/costs` page

Source: `app/api/costs.py` (route), `app/templates/pages/costs.html`.

Cross-case view. Per-case sections with subtotals. Used to see outstanding costs across all active matters.

**⚠ Known gap:** the `/costs` route does not pass `overdue_costs` or `upcoming_costs` to the template, but `costs.html:56-98` renders alert sections for them. Fix: compute in the route:

```python
overdue_costs = cost_repo.get_pending()  # status=OFFEN
# filter to due_at < now
upcoming_costs = [c for c in cost_repo.get_pending() if c.due_at and c.due_at < now + timedelta(days=7)]
```

Pass both to the template context.

**⚠ Known gap:** `get_costs_for_page` reads `case.streitwert` which does not exist on the `Case` model — always returns `None`. Either add `Case.streitwert` (a per-case value-in-dispute used for RVG calculation) or remove the reference and derive Streitwert from the highest `LegalCost.streitwert` in the case. The field is rendered on the per-case cost section header.

---

## 14. Notification integration

`helpers.py:112-121` computes `overdue_costs` count for the global notification badge (costs with `due_at < now AND status NOT IN (BEZAHLT, ERSTATTET)`). This already feeds the rail `🔔` badge.

No new notification work required for the Financials spec — the infrastructure exists.

---

## 15. Empty states

| Situation | What renders |
|---|---|
| Case with no `cost_delta` documents and no `LegalCost` rows | Panel: "0 € · No costs recorded" + `[+ Add Cost]` link opening `/costs/cases/{id}/new` |
| Case with `cost_delta` signals but no `LegalCost` rows | Panel: Signalled total + "No statutory positions entered yet. [+ Add Cost]" in breakdown tab |
| Case with overdue costs | Breakdown tab: red overdue alert at top (mirrors `costs.html:56-98`) |
| All costs paid and reimbursed | Open = 0 €; "All costs resolved" in muted text below totals |
| Document with no `cost_delta` and no `cost_candidates` | HUD cost sidebar not rendered (section suppressed entirely) |
| Document with `cost_candidates` but no `cost_delta` | HUD sidebar renders candidates list only; no promote button |
| Promote-to-cost on a triage-only document (`case_id = _TRIAGE`) | Promote button hidden; would silently create an orphaned `LegalCost` with invalid `case_id` |

---

## 16. Keyboard-first interaction

| Key | Action | Implemented |
|---|---|---|
| `$` | Switch case dashboard to Financials view | ✅ `dashboard.js:375` |
| `+` (while Financials view active) | Open new-cost form for current case | ❌ to implement |
| `j` / `k` | Navigate between cost rows in breakdown table | ❌ to implement |
| `Enter` on a cost row | Open source document HUD (if `source_document_id` set) | ❌ to implement |

---

## 17. Data sources map

| Financials zone | Primary source | Populated by |
|---|---|---|
| Signalled exposure big number | `Case.total_cost_exposure` ÷ 100 | Phase 4 (enrich task rollup) |
| Last-delta row | Most recent `Document.cost_delta` for the case | Phase 4 |
| Breakdown Gross / Open / Reimbursable | `CostSummary` from `LegalCost` rows | User or promote-to-cost |
| Statutory line items | `LegalCost` rows | Manual entry + promote-to-cost |
| Per-proceeding grouping | `LegalCost.proceeding_id` | Manual entry + promote (inheriting from doc) |
| Per-document cost signal | `Document.cost_delta` | Phase 4 (AI enricher) |
| Per-document candidates | `Document.cost_candidates` | Phase 3 (regex at ingest) |
| HUD promote button | `Document.cost_delta` non-null + `doc.case_id != _TRIAGE` | Phase 4 |
| Global `/costs` view | All `LegalCost` rows | Phase 1 (schema) |
| Overdue alert | `LegalCost.due_at < now AND status != BEZAHLT/ERSTATTET` | Phase 1 |

---

## 18. Files that will change

### New

| File | Purpose |
|---|---|
| `alembic/versions/<hash>_add_proceeding_id_to_legal_costs.py` | Add nullable `proceeding_id` FK + index to `legal_costs` |
| `tests/integration/test_costs_crud.py` | Coverage for `POST /costs`, `/pay`, `/reimburse`, `/update-field`, and `promote_cost_delta` |

### Modified

| File | Change |
|---|---|
| `app/api/costs.py` | Add `POST /costs`, `POST /costs/{id}/update-field`, `POST /costs/{id}/pay`, `POST /costs/{id}/reimburse`; pass `overdue_costs` + `upcoming_costs` to `/costs` template context |
| `app/api/documents.py:600-634` | Fix `cost-from-delta` to compute `amount_gross`; call `recompute_total_cost_exposure` after commit |
| `app/models/database.py` (`LegalCost`) | Add `proceeding_id` column + FK + relationship |
| `app/repositories/legal_cost.py` | `get_by_case(case_id, proceeding_id=None)` with optional filter; `bulk_sum_by_cases` proceeding split |
| `app/services/intelligence/prompts.py:33-36` | Fix direction semantics: `incoming = money received/owed to us`, `outgoing = money we must pay` |
| `app/templates/partials/case_financials_panel.html` | Add "Last delta" row + `[breakdown →]` link; relabel big number to "Signalled" |
| `app/templates/partials/dashboard/financials_view.html` | Group rows by proceeding with subtotals; label total as "Booked" |
| `app/templates/partials/cost_form.html` | Add proceeding dropdown |
| `app/templates/partials/hud/_cost_delta.html` | Micro-copy aligned with corrected direction semantics |
| `app/services/case_service.py:125-167` | Add code comment documenting absolute-value decision in rollup |
| `app/services/case_dashboard_service.py` | Add last-cost-delta document to context for panel |
| `tests/unit/test_intelligence_document_enricher.py` | Lock corrected direction semantics; add test for promote-to-cost `amount_gross` |
| `docs/specs/00_vision.md` | §8 link to `08_financials.md` |
| `docs/specs/02_dashboard.md` | §5/§9/§15 link to `08_financials.md`; clarify "Signalled" vs. "Booked" |

### Deleted

| File | Reason |
|---|---|
| `app/templates/components/cost_row.html` | Unused macro file — no template references; `partials/cost_row.html` is the live one. Pre-release "clean as you go." |

---

## 19. Phase progression

| Phase | What the Financials feature gains |
|---|---|
| Phase 1 | `LegalCost` table schema, `CostCategory`/`CostStatus` enums, `Case.total_cost_exposure` column |
| Phase 3 | `extract_cost_candidates` regex runs at ingest; `Document.cost_candidates` populated |
| Phase 4 | AI enricher writes `Document.cost_delta`; enrich task triggers `recompute_total_cost_exposure` |
| Phase 5 | Dashboard left-column panel renders signalled exposure and booked summary |
| Phase 5+ ← current | Global `/costs` page (read-only); HUD cost sidebar; promote-to-cost |
| Remediation | CRUD routes; per-proceeding split; promote bug fix; direction semantics; panel last-delta + breakdown link |
| v2 | Automated RVG fee calculation from Streitwert + Gebührenfaktor; PDF Kostenrechnung import |

---

## 20. Non-goals

- **No synthetic cost predictions.** No "expected total by end of proceeding" figure. Only factual amounts from documents or manual entries.
- **No automated RVG fee calculation from Streitwert.** The user enters amounts; the system tracks and categorises them. RVG position and factor are stored but not computed.
- **No PDF Kostenrechnung import.** Cost bills are promoted from extracted `cost_delta` signals or entered manually. Structured PDF parsing is v2.
- **No multi-currency.** All amounts in EUR. Foreign-currency costs must be converted before entry.
- **No budget vs. actual tracking.** No budget columns. This is a cost log, not a cost plan.
- **No court-fee calculator.** Streitwert is stored per `LegalCost` row as metadata, not used to derive GKG amounts.

---

## 21. Verification

### Manual test steps

1. `make seed && make run` → `/costs` shows seeded cost rows; `Mark Paid` button transitions status to BEZAHLT; row swaps in place with updated color.
2. `GET /costs/new` → submit form with required fields → row appears under the correct case in `/costs`.
3. `/cases/<id>?view=fin` → costs grouped by proceeding with subtotals; "Case-level" group for costs without a proceeding.
4. Switch active proceeding in top bar → `[All proceedings ▾]` dropdown filters correctly.
5. Open document HUD with `cost_delta` → click `[+ promote to LegalCost]` → no IntegrityError; row appears in breakdown tab; left-panel Signalled total unchanged (correct — promote creates a LegalCost but does not change the cost_delta signal).
6. Panel: "Last delta" row shows most recent document with a cost_delta; clicking its title opens the HUD at that document.
7. Panel: `[breakdown →]` switches main area to Financials tab.
8. Direction semantics: open a doc whose enricher detected `incoming` → HUD shows `+` green amount.

### Automated coverage

| Test file | What it covers |
|---|---|
| `tests/unit/test_case_service_cost_rollup.py` | `recompute_total_cost_exposure` — sums, cents conversion, `_TRIAGE` skip |
| `tests/unit/test_intelligence_document_enricher.py` | `cost_delta` write, invalid direction → `none`; **add** test locking corrected direction semantics |
| `tests/unit/test_repositories.py` | `LegalCostRepository.create_cost`, `get_by_case`, `sum_amounts` |
| `tests/integration/test_ingestion_pipeline.py` | `cost_candidates` populated at ingest |
| `tests/integration/test_api_routes.py` | `GET /costs` 200 |
| **New** `tests/integration/test_costs_crud.py` | `POST /costs` happy path + missing fields 422; `POST /pay` + `/reimburse` happy path + cross-case 404; `promote_cost_delta` no IntegrityError; `amount_gross` computed correctly per direction |

---

## 22. Success criteria

- `POST /costs`, `/pay`, `/reimburse`, `/update-field` all respond 200 with a valid HTMX partial; no 404/405 from any form action on the page.
- `promote_cost_delta` endpoint: zero `IntegrityError` across all `direction × category` combinations; `amount_gross` always set; rollup called; booked total reflects the new row within the same request.
- Panel "Signalled" number and breakdown "Booked" number carry distinct labels visible without tooltip; both non-zero on a case with both signal and booked costs.
- Per-proceeding grouping renders correctly for a case with two or more proceedings and at least one cost in each; "Case-level" group appears only when `proceeding_id IS NULL` rows exist.
- Direction semantics: an AI-enriched doc with an incoming amount shows `+` green in HUD; an outgoing amount shows `−` red; matches new prompt definition.
- `/costs` overdue alert section is not empty when a cost with `due_at < now AND status = OFFEN` exists.
- All existing tests pass after the direction-semantics patch to `prompts.py`.

---

*Related: `docs/specs/00_vision.md` §8 — cost delta north star · `docs/specs/02_dashboard.md` §5 Financials panel / §9 Financials view mode · `docs/specs/04_document_hud.md` §8f — cost delta rail*
