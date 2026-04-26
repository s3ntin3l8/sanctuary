# Sanctuary — Financials

Companion document to `docs/specs/00_vision.md` §8 and `docs/specs/02_dashboard.md` §5/§9. Covers the full cost-tracking surface: per-document cost signals, case-level exposure, and the statutory line-item ledger.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

| Layer | Status |
|---|---|
| `LegalCost` table + `LegalCostRepository` + `CostService` + `CostSummary` | ✅ |
| `Document.cost_delta` JSON + `CostDeltaSchema` validation | ✅ |
| `Document.cost_candidates` regex pre-extraction at ingest | ✅ |
| `Case.total_cost_exposure` (cents) + `recompute_total_cost_exposure` rollup | ✅ |
| `GET /costs` global cross-case browser | ✅ |
| `GET /costs/new` + `GET /costs/cases/{id}/new` cost form | ✅ |
| Dashboard left-column `case_financials_panel.html` | ✅ |
| Dashboard `partials/dashboard/financials_view.html` view-mode tab | ✅ |
| HUD `partials/hud/_cost_delta.html` + promote-to-cost button | ✅ |
| `POST /costs` (create) | ✅ |
| `POST /costs/{id}/update-field` (inline edit) | ✅ |
| `POST /costs/{id}/pay` | ✅ |
| `POST /costs/{id}/reimburse` | ✅ |
| Per-proceeding cost split | ✅ |
| Direction semantics consistent between prompt and HUD | ✅ |
| Overdue/upcoming alerts passed to `/costs` route | ✅ |

### Implementation Deviations

| Feature | Vision §8 / Dashboard §5 | Code | Status |
|---|---|---|---|
| `cost_delta` JSON shape | `{amount, direction, description}` | Same; validated by `CostDeltaSchema` | ✅ |
| Cumulative `Case.total_cost_exposure` | "recomputed on ingest" | Σ\|amount\| via `recompute_total_cost_exposure`; trigger inside enrich task | ✅ Accepted — absolute value means signalled exposure always grows; direction is shown per-document |
| "No synthetic probability" | factual amounts only | RVG/GKG/JVEG statutory positions + §91 ZPO flag; no prediction | ✅ |
| Per-proceeding split in Financials view | "Includes a per-proceeding split" (dashboard §9) | Implemented via `proceeding_id` on `LegalCost` | ✅ Accepted |
| `+450 € (Apr 02)` last-delta line in panel | dashboard §5 panel mock | Implemented in left-column panel | ✅ Accepted |
| `[breakdown →]` link in panel | dashboard §5 panel | Implemented | ✅ Accepted |
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

**Direction semantics** (canonical definition):

| Direction | Meaning | HUD display |
|---|---|---|
| `incoming` | Money received or owed **to us** (Erstattung, Zahlung, PKH-Bewilligung) | `+` green, `trending_up` |
| `outgoing` | Money **we must pay** (Gebühr, Vorschuss, Kostenfestsetzung gegen uns) | `−` red, `trending_down` |
| `ruling` | Court-determined amount (Kostenfestsetzungsbeschluss, neither side yet pays) | amber, `gavel` |
| `none` | No specific financial direction; amount is mentioned | neutral |

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

Regex-extracted at ingest before AI enrichment:
- EUR pattern: `\d{1,3}(?:\.\d{3})*(?:,\d{2})?` followed by `EUR|€|euros?`; filtered to 10 < amount < 1,000,000
- RVG position pattern: `Nr\.?\s*\d{1,4}\s*VV\s*RVG|KV\s*GKG\s*Nr\.?\s*\d+|§\s*\d+\s*ZPO`
- Capped at 20 candidates per document

`cost_candidates` is the fallback when `cost_delta` is null — the HUD sidebar renders the first 5 candidate amounts with their context.

---

## 3. Data model — `Case.total_cost_exposure`

```
total_cost_exposure: Integer, default=0   // stored in cents
```

Recomputed by `recompute_total_cost_exposure(case_id, db)`:
- Sums `|cost_delta.amount|` across all documents with non-null `cost_delta` for the case
- Skips `case_id == "_TRIAGE"`
- Stores `int(round(total_euros * 100))` in cents
- **Commits itself** — callers do not need an additional `db.commit()`
- Triggered by the enrich Celery task

Display: always divide by 100.0 before passing to `format_eur()`.

---

## 4. Data model — `LegalCost`

```
id                  Integer PK
case_id             String FK→cases.id, NOT NULL, indexed
proceeding_id       Integer FK→proceedings.id, nullable

category            CostCategory, NOT NULL
status              CostStatus, default=OFFEN, NOT NULL, indexed

title               String, NOT NULL
rvg_position        String, nullable

amount_net          Float, NOT NULL
vat_rate            Float, default=0.0
amount_gross        Float, NOT NULL
amount_paid         Float, default=0.0
amount_reimbursed   Float, default=0.0

streitwert          Float, nullable
gebuehren_faktor    Float, nullable
is_reimbursable     Boolean, default=True

issued_at           DateTime, nullable, indexed
due_at              DateTime, nullable, indexed
paid_at             DateTime, nullable, indexed
source_document_id  Integer FK→documents.id, nullable
notes               Text, nullable
ingest_date         DateTime

indexes: ix_legal_costs_case_status (case_id, status)
         ix_legal_costs_status_due (status, due_at)
         ix_legal_costs_case_proceeding (case_id, proceeding_id)
```

### `CostCategory` enum

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

---

## 5. Statutory framing

German legal costs are governed by four statutes. Sanctuary tracks all four without enforcing any particular calculation — amounts are entered by the user or extracted from documents.

| Statute | Scope | Key metric |
|---|---|---|
| **RVG** — Rechtsanwaltsvergütungsgesetz | Own lawyer fees | Streitwert + Gebührenfaktor |
| **GKG** — Gerichtskostengesetz | Court filing fees and Vorschuss | Streitwert-based |
| **JVEG** — Justizvergütungs- und -entschädigungsgesetz | Expert witnesses, interpreters | Hourly or per-page rates |
| **§§91–107 ZPO** | Cost allocation ("loser pays") | Winner party recovery |

---

## 6. CRUD and Promotions

- **Promote Signal:** HUD sidebar button converts a `cost_delta` signal into a `LegalCost` position, calculating `amount_gross` based on category/direction and triggering exposure recomputation.
- **CRUD Routes:** Full support for `POST /costs`, `POST /costs/{id}/pay`, `POST /costs/{id}/reimburse`, and `POST /costs/{id}/update-field`.
- **Per-Proceeding Split:** Costs can be assigned to a specific `proceeding_id` or left at case-level.
- **Exposure Rollup:** `Case.total_cost_exposure` always reflects the current sum of signalled document costs.

---

## 7. Success criteria

- Financial exposure panel shows "Signalled" total from documents and "Booked" summary from statutory ledger.
- Breakdown tab groups costs by proceeding with subtotals.
- Promotions from document signals correctly calculate VAT and gross amounts.
- CRUD operations for payments and reimbursements update the ledger and trigger exposure recomputations.
- Global costs page correctly displays overdue and upcoming alerts.
- Correct direction semantics (incoming = received/owed) reflected across all UI surfaces.

---

## Related docs

- `docs/specs/00_vision.md` — North star vision
- `docs/specs/02_dashboard.md` — Dashboard integration
- `docs/specs/04_document_hud.md` — HUD cost signals
