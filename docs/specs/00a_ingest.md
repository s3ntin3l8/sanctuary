# Sanctuary — Ingest Pipeline (Phase 3)

Companion document to `docs/vision.md`, `docs/triage.md`, and `docs/dashboard.md`. Covers how documents get into Sanctuary from the outside world.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

### Feature Matrix

| Feature | Status | Implementation |
|---------|--------|---------------|
| Gmail OAuth + allowlist | ✅ Implemented | `gmail.py`, `gmail_sync.py` |
| Bulk backfill | ✅ Implemented | `run_gmail_backfill` task |
| Continuous sync | ✅ Implemented | `sync_gmail_incremental` task |
| Scan folder watcher | ✅ Implemented | `scan_folder.py` |
| Document slicing (heuristics) | ✅ Implemented | `slicer.py` - 7 signals |
| Document slicing (AI) | ✅ Implemented | AI refinement pass |
| .eml upload | ✅ Implemented | `/upload` endpoint |
| Dedup (message-id) | ✅ Implemented | SHA256 fallback |
| Cover letter detection | ✅ Implemented | Phase 4 AI (`batch_analyzer.py`) |
| True originator | ✅ Implemented | Via AI pass |
| Proceeding detection | ✅ Implemented | `proceeding_analyzer.py` |
| Action item extraction | ✅ Implemented | AI extracts dates |
| Image conversion | ❌ Excluded | By design - PDF only input |

### Implementation Deviations

| Feature | Spec | Code | Status |
|--------|------|------|--------|
| Chat streaming | WebSocket | HTTP/1.1 + SSE | ✅ Accepted |
| Image formats | JPG/HEIC/TIFF | PDF only | ✅ Accepted |

**Chat Streaming Deviation:** The spec mentioned WebSocket for real-time AI chat streaming. The implementation uses HTTP/1.1 chunked transfer with SSE (Server-Sent Events). This achieves real-time streaming with lower complexity and better proxy/firewall compatibility. Latency is acceptable (~50-100ms per chunk).

**Image Conversion Deviation:** The spec listed JPG/HEIC/TIFF as supported input formats. Implementation was changed to PDF-only input. Rationale: scanners produce PDF natively, simplifying the pipeline and ensuring consistent downstream processing.

---

## The core shift

**Traditional DMS:** drop files into folders, tag them, move on.

**Sanctuary ingest:** the wire arrives in three shapes — Gmail messages, scanned PDFs, and manual .eml uploads — and the pipeline converges them into **one family tree per delivery**. A single scanned PDF that bundles a cover letter + opposing statement + three annexes becomes five typed `Document` rows with proper parent-child wiring, ready for triage with case/proceeding already attempted.

The ingest pipeline does the structural work up front so triage is a **strategic session**, not data entry (see `docs/triage.md`).

---

## 1. Three ingest paths

| Path | When to use | Privacy | v1 scope |
|---|---|---|---|
| **Gmail API** | Primary — 95% of legal comms arrive as email from the lawyer, via email. | Data flows Google → your machine → local DB; nothing goes the other way. | Full |
| **Scan folder** | Physical mail you scanned; photos from phone; PDFs rescued from other systems. | Fully local; nothing leaves the machine. | Full, with document slicing |
| **.eml upload** | Fallback: one-off mail not from the configured Gmail account; forwarded mail from an assistant; archives. | Fully local. | Full |

All three paths converge on the same downstream pipeline — `IngestBatch` → `Document`s → AI enrichment → triage queue.

```
  ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
  │  Gmail API    │   │  Scan folder  │   │  .eml upload  │
  │  (OAuth poll) │   │  (watcher)    │   │  (manual)     │
  └───────┬───────┘   └───────┬───────┘   └───────┬───────┘
          │                   │                   │
          ▼                   ▼                   ▼
  ┌──────────────────────────────────────────────────────┐
  │             RFC822 parser / PDF loader               │
  └────────────────────────┬─────────────────────────────┘
                           │
                           ▼
  ┌──────────────────────────────────────────────────────┐
  │   Create IngestBatch    (one source = one batch)     │
  └────────────────────────┬─────────────────────────────┘
                           │
         ┌─────────────────┴─────────────────┐
         ▼                                   ▼
  ┌──────────────┐              ┌──────────────────────┐
  │  PDF slicer  │              │  Attachment splitter │
  │  (scan path) │              │  (email path)        │
  └──────┬───────┘              └──────────┬───────────┘
         │                                 │
         └─────────────────┬───────────────┘
                           ▼
  ┌──────────────────────────────────────────────────────┐
  │   Per-attachment/slice → Document row                │
  │   Docling conversion → content, meta, chunks         │
  └────────────────────────┬─────────────────────────────┘
                           ▼
  ┌──────────────────────────────────────────────────────┐
  │   AI enrichment (Phase 4):                            │
  │   · cover letter detection                            │
  │   · attributed_originator                             │
  │   · significance_tier, document_type                  │
  │   · key_passages, cost_delta                          │
  │   · proceeding match via Az                           │
  │   · action items extraction                           │
  └────────────────────────┬─────────────────────────────┘
                           ▼
  ┌──────────────────────────────────────────────────────┐
  │   Triage queue (batch-aware, see docs/triage.md)     │
  └──────────────────────────────────────────────────────┘
```

---

## 2. Gmail import (primary path)

The user's legal mail already lives in Gmail. Rather than forward, copy, or scrape, Sanctuary reads the user's own inbox directly through the Gmail API — with user-controlled filters so nothing irrelevant gets ingested.

### 2.1 OAuth scope

Read-only, narrow scope:

- **`https://www.googleapis.com/auth/gmail.readonly`** — read messages and attachments. No send, no modify, no labels write.
- User signs in to Google via OAuth; Sanctuary receives a refresh token stored locally and encrypted.
- **Token storage**:
  - macOS: system Keychain via `keyring` library
  - Linux: Secret Service / libsecret via `keyring`
  - Fallback: AES-256-GCM encrypted file with key derived from a user-supplied passphrase (prompted on startup)
- Revocation is always one-click: user revokes in their Google Account security page; Sanctuary detects the 401 and prompts re-auth.

### 2.2 Sender allowlist

User configures a list of email addresses that are considered "legal mail":

```
gmail_allowlist:
  - lawyer@kanzlei-funk.de
  - kanzlei@anwalt-mueller.de
  - noreply@justiz.hamburg.de
  - jugendamt-hn@hamburg.de
```

Only messages where the sender (`From:`) matches an entry on this list are considered for ingest. Unlisted senders are skipped entirely — never touched, never stored.

User-facing UI: a simple settings page with add/remove; suggested entries surfaced from recent Gmail senders (read via the API) but never auto-added.

### 2.3 Optional Gmail label filter

A second, narrower guardrail. User sets a Gmail filter in Gmail itself (e.g., "messages from lawyer@... get label `Legal/Funk`") and configures Sanctuary to only pull messages carrying that label:

```
gmail_label_filter: "Legal"
```

If set: Sanctuary queries `in:inbox label:Legal` and further filters the result by the sender allowlist. Both filters must match.

If unset: only the sender allowlist applies.

### 2.4 Bulk backfill mode

One-time historical import. User configures a window (`last 90 days`, `last 2 years`, `everything`) and clicks **Run backfill**. Sanctuary:

1. Queries Gmail for all messages matching the filters within the window
2. Streams messages in pages of 100
3. For each message: creates an `IngestBatch`, downloads attachments, kicks off the downstream pipeline
4. Progress bar shows `X of Y messages imported`
5. On completion, switches to continuous sync

Backfill is **resumable** — if interrupted, it picks up from the last-processed Gmail message ID. The user can run it again later for a different window without double-ingesting (see §14 dedup).

### 2.5 Continuous sync mode

Once seeded, Sanctuary polls the Gmail History API periodically for new messages:

- Default poll interval: 5 minutes
- Uses Gmail's `historyId` cursor so each poll only returns new changes since last sync
- On new message matching filters: same downstream pipeline as backfill
- Stored cursor in `UserSettings.settings_json["gmail_last_history_id"]`

Future enhancement: Gmail push notifications (Pub/Sub) for near-realtime — not v1.

### 2.6 Data locality

- All message bodies and attachments are downloaded to the local machine and stored in the project's data directory
- Nothing is sent back to Google beyond the OAuth token refresh flow
- Message metadata (subject, sender, message-id, thread-id) is stored in `IngestBatch`
- Attachments are stored in `data/attachments/<batch_id>/<filename>`
- Message-id is used for batch-level dedup (see §14)

---

## 3. Scan folder (secondary path)

For physical mail, court documents scanned at a copy shop, mobile phone photos of letters — anything not in email.

### 3.1 Folder watcher

A configured folder path is watched for new files:

- Linux: `inotifywait` / watchdog library
- macOS: `fsevents` / watchdog library
- Fallback: periodic scan (every 30s) for new files by mtime

Supported file types: `.pdf`, `.docx`, `.txt`, `.md`, `.pptx`, `.xlsx`, `.eml`

> **Design Decision (April 2026):** Image formats (`.jpg`, `.jpeg`, `.png`, `.tiff`, `.heic`) deliberately excluded. All input must be PDF. Scanners produce PDF natively. This simplifies the ingestion pipeline and ensures consistent downstream processing.

### 3.2 Pre-processing

- No image conversion - input must already be PDF
- Files validated by magic bytes
- PDF pages counted for slicing decisions

Result: every incoming file is already a PDF.

### 3.3 Lifecycle

```
watched_folder/incoming/   ← new files drop here
        │
        ▼
   (pre-process)
        │
        ▼
watched_folder/processing/   ← held during ingest; atomic move prevents re-picks
        │
        ├── success → watched_folder/processed/YYYY-MM-DD/<batch_id>/
        └── failure → watched_folder/failed/<batch_id>/ + error log
```

The user can drop files into `incoming/`, check `processed/` for what landed, and inspect `failed/` if something went wrong.

### 3.4 Mobile capture

Users can AirDrop / share from their phone to a sync folder (Dropbox, Syncthing, iCloud Drive with local mirror), pointed at the watched folder. This gives a "scan and forget" workflow without Sanctuary needing its own mobile app.

---

## 4. Document slicing (new primitive)

The hardest part of scan ingest: **one scanned PDF usually contains multiple documents.** A typical incoming scan from the lawyer:

```
Page 1:    Cover letter from LG Hamburg ("in der Anlage übersenden wir...")
Page 2:    Court ruling (Beschluss)
Page 3-7:  Opposing counsel's Klageerwiderung
Page 8-9:  Anlage K1 (invoice)
Page 10-12: Anlage K2 (prior correspondence)
Page 13:   Jugendamtsbericht (single page)
```

Without slicing, all 13 pages become one opaque `Document` — useless for the graph, useless for relationships, useless for significance tiering.

### 4.1 Approach: hybrid with user review

For v1, slicing is **explicit and user-reviewable**. Automated unattended slicing is a later enhancement — mistakes here are expensive because they corrupt the parent-child hierarchy and scramble attribution.

The flow:

1. User drops a scanned PDF into the watched folder (or uploads directly)
2. Pipeline renders page thumbnails
3. **Heuristic pass** proposes cut-lines between pages (§4.2)
4. **AI pass** refines proposals with per-page OCR context (§4.3)
5. User opens the **Slicing UI** (§4.4) to confirm/adjust cuts
6. On confirm → N `Document` rows are created with proper `parent_id` wiring
7. Each slice goes through the normal Docling conversion + AI enrichment pipeline

### 4.2 Heuristic signals (fast, deterministic)

Each is a cheap cue that a new document starts at page N:

- **Page-number reset**: prior page footer says "Seite 3/5", current page says "Seite 1/2"
- **Letterhead change**: visual hash of the top 20% of the page differs significantly from prior page
- **Salutation + signature pattern**: prior page contains "Mit freundlichen Grüßen" + signature block; current page opens with salutation or letterhead
- **Blank page**: blank intervening pages often indicate a cover-separator
- **Date line change**: top-of-page date on prior ≠ top-of-page date on current ✅ IMPLEMENTED
- **Azeichen change**: court file number in header/footer changes
- **Enclosure marker**: text "Anlage K1" or "Anlage 1" appears prominently near the top

Each signal contributes a score; pages with score > threshold are marked as **proposed cut points**.

### 4.3 AI signal

For each candidate cut point (from heuristics) + a few "close calls", the AI is given:

```
You're deciding whether page N is the start of a new document.
Previous page (last 500 chars of OCR): "... Mit freundlichen Grüßen / Unterschrift"
Current page (first 500 chars of OCR): "Landgericht Hamburg / An ... / Aktenzeichen: 003 F 426/25"
Is this page the start of a new document? (yes/no/unsure)
```

The AI confirms/refines the heuristic's proposals. When heuristic and AI disagree, the cut is still proposed but marked **low-confidence** for user attention in the slicing UI.

### 4.4 Slicing UI

```
┌──────────────────────────────────────────────────────────────────────┐
│  Slicing: Kanzleischreiben_2026-03-14.pdf                            │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  Document 1 ───────────────────────────────────────────────────────── │
│                                                                       │
│    ┌───────┐  ┌───────┐                                               │
│    │ Page 1│  │ Page 2│      Cover letter LG Hamburg                  │
│    │       │  │       │      [cover_letter] [Court]                   │
│    └───────┘  └───────┘                                               │
│                                                                       │
│  ✂ cut  (AI + 3 heuristic signals)                                   │
│                                                                       │
│  Document 2 ───────────────────────────────────────────────────────── │
│                                                                       │
│    ┌───────┐  ┌───────┐  ┌───────┐  ┌───────┐  ┌───────┐              │
│    │ Page 3│  │ Page 4│  │ Page 5│  │ Page 6│  │ Page 7│              │
│    │       │  │       │  │       │  │       │  │       │              │
│    └───────┘  └───────┘  └───────┘  └───────┘  └───────┘              │
│                                                                       │
│    Klageerwiderung Beklagter   [statement] [Opposing]  (via court)    │
│                                                                       │
│  ✂ cut  (AI high confidence)                                         │
│                                                                       │
│  Document 3 ───────────────────────────────────────────────────────── │
│                                                                       │
│    ┌───────┐  ┌───────┐                                               │
│    │ Page 8│  │ Page 9│      Anlage K1 — Rechnung                     │
│    │       │  │       │      [annex, proof]                           │
│    └───────┘  └───────┘                                               │
│                                                                       │
│  ⚠ Possible cut here?  (heuristic only, AI unsure)                   │
│                                                                       │
│    ┌───────┐                                                          │
│    │ Page10│                                                          │
│    │       │                                                          │
│    └───────┘                                                          │
│                                                                       │
│  ...                                                                  │
│                                                                       │
│  [keep all proposed cuts]  [customize]  [treat as single document]    │
└──────────────────────────────────────────────────────────────────────┘
```

Interactions:

- **Drag a cut-line** between any two pages to add a split
- **Click a cut-line** to remove it (merge adjacent documents)
- **Click a document header** to edit its proposed title / role / attributed originator before confirming
- **[keep all]** accepts all AI+heuristic proposals as-is
- **[treat as single document]** rejects all cuts → one big Document
- **[customize]** enters full manual mode — thumbnails only, user draws all cuts

### 4.5 Slice output

After user confirms, N `Document` rows are created:

- First slice (typically the cover letter) → `role=cover_letter`, `court_relay=True`, `parent_id=NULL`
- Subsequent slices → `role=enclosure`, `parent_id=<first_slice.id>`
- Each slice's pages are extracted to a per-slice PDF for archival
- Each slice runs through Docling → content, meta, chunks
- AI enrichment proceeds per-slice normally

### 4.6 Slicing is optional

A scanned PDF that is actually a single document (e.g., a standalone 30-page Gutachten) passes through slicing with zero proposed cuts; user confirms "treat as single document" and proceeds. Slicing never forces multiple documents where one is correct.

---

## 5. Manual .eml upload (fallback)

Always-available path for mail outside the configured Gmail account — a forwarded message from an assistant, an archive from a prior provider, a one-off from a non-allowlisted sender that the user wants to ingest anyway.

- Drag-and-drop or file picker
- Same RFC822 parser used by the Gmail path; behaves identically downstream
- `IngestBatch.source_type = manual`
- `IngestBatch.raw_source_path` points at the uploaded .eml
- No allowlist enforcement — the user explicitly chose this file, so trust the intent

---

## 6. Shared parsing layer

Gmail API returns RFC822 message bytes (via `messages.get(format='raw')`); the .eml upload path also holds RFC822 bytes. A single parser handles both:

```python
# app/services/ingest/email_parser.py
def parse_rfc822(raw_bytes: bytes) -> ParsedEmail:
    # standard lib email.parser
    # extracts: subject, from, to, cc, message-id, date, in-reply-to
    # extracts: all attachments (content-disposition=attachment or inline with filename)
    # decodes charsets; handles multipart/mixed, multipart/alternative
    ...
```

Output is a normalized structure; the rest of the pipeline doesn't care which path produced it.

---

## 7. IngestBatch creation

One external source = one `IngestBatch` row:

| Source | `source_type` | `raw_source_path` | `message_id` |
|---|---|---|---|
| Gmail message | `email` | `data/gmail/<message_id>.eml` (cached) | RFC5322 Message-Id |
| Folder drop (scan) | `scan` | `data/scans/<batch_id>/original.pdf` | null |
| Manual .eml upload | `manual` | `data/uploads/<batch_id>.eml` | RFC5322 Message-Id |

Initial fields:
- `sender_email` — from the email `From:` header; null for pure scans
- `subject` — email subject; for scans, derived from the filename
- `received_at` — email `Date:` header; for scans, file mtime
- `status` — starts `pending`, flips to `processing`, then `completed` or `failed`
- `case_id` / `proceeding_id` — null initially; populated after AI detection (Phase 4) or by user confirmation in triage

*Note:* `IngestBatch.message_id` column doesn't yet exist in Phase 1 — it will be added as part of the Phase 3 data-model touchpoints (see §13).

---

## 8. Attachment / slice → Document

Each attachment (from an email) or each slice (from a scanned PDF) becomes one `Document`:

1. Compute content hash (`SHA-256`) of the raw file bytes
2. Check dedup (§14) — if already ingested, link to existing Document
3. Create Document with `ingest_batch_id`, `parent_id` (if enclosed under a cover letter), `file_path`, `content_hash`
4. Kick off Docling conversion (async background task)
5. Docling result → `content` (markdown), `meta` (pages, headings, chunks)
6. Mark `pipeline_stages.extract` → `completed`; `pipeline_state` recomputes to `completed` (or `partial` if later AI stages pending)
7. Trigger AI enrichment (Phase 4)

Parent-child wiring:

- **Email path**: cover letter detected → its `id` becomes `parent_id` for all sibling attachments
- **Scan path**: the first slice (always the cover letter in typical scans) is the parent; subsequent slices link to it

---

## 9. Cover letter detection

Hybrid — fast heuristic narrows candidates, AI makes the final call.

### 9.1 Heuristic signals (per document)

Each signal contributes a probability:

- **Filename**: contains `Begleitschreiben`, `Anschreiben`, `cover`, `Übersendungsschreiben`
- **Sender = court**: sender email domain matches a court allowlist (`@justiz.*`, `@*gericht*`)
- **Length**: short (typically < 2 pages)
- **Body keywords**: "in der Anlage übersenden wir", "übermitteln wir Ihnen", "zur Kenntnis und Stellungnahme"
- **Has enclosures in the same batch**: a bare letter with no siblings is probably standalone, not a cover

Score > threshold → candidate cover letter.

### 9.2 AI pass

For candidates, AI is prompted with the full document content:

```
Is this document a court cover letter (Begleitschreiben) that is primarily
forwarding other documents? If yes, identify:
  - What is being forwarded (e.g., "Schreiben der Gegenseite vom 12.03")
  - Is the court adding substantive content of its own (e.g., a deadline, a ruling)?
Return: {"is_cover_letter": bool, "court_relay": bool, "enclosed_descriptions": [...]}
```

### 9.3 Outputs

- `Document.role = cover_letter` / `enclosure` / `standalone`
- `Document.court_relay = True` (pure relay — just forwarding) or `False` (court added substance)
- For each enclosed document in the batch, the AI's enclosed description is used to attribute the originator (§10)

---

## 10. True originator attribution

The court as infrastructure principle: when a Begleitschreiben wraps an opposing counsel's statement, that enclosed document's originator is **opposing counsel**, not the court. The correspondence graph must show the opposing side as the sender; the court is just routing.

### 10.1 Extraction

From the cover letter text, the AI extracts phrases like:

- "übersenden wir das Schreiben der Gegenseite vom 12.03" → attribute enclosure to opposing counsel
- "überreichen wir den Bericht des Jugendamts Bezirk Hamburg-Nord" → attribute enclosure to `Jugendamt Bezirk Hamburg-Nord`
- "übermitteln wir unseren Beschluss vom 02.04" → NOT a relay; court is the actor, `court_relay=False`

### 10.2 Applying to children

For each enclosed document in the batch, the AI matches descriptions to filenames/content and writes:

- `Document.attributed_originator = "Opposing counsel"` (or the specific party name)
- `Document.originator_type = OriginatorType.OPPOSING` (or `COURT`, `THIRD_PARTY`, etc.)
- `Document.extraction_confidence["originator"] = "high" | "medium" | "low"`

### 10.3 Low-confidence handling

When the AI isn't sure — multiple enclosures, ambiguous descriptions, or the cover letter doesn't clearly say — the attribution is set with `confidence = low` and surfaces in triage as a field needing user confirmation. Triage UI highlights low-confidence fields (see `docs/triage.md` §2).

---

## 11. Proceeding detection

Each batch should land in the right case AND the right proceeding. The cover letter typically carries the Aktenzeichen; we match on that.

### 11.1 Extraction

AI or regex extracts Az patterns from the cover letter header/footer:

- German court Az patterns: `NNN F NNN/YY` (Familiengericht), `NN O NNN/YY` (Landgericht Zivilkammer), `XII ZB NNN/YY` (BGH), etc.
- Normalization: strip whitespace, uppercase, remove punctuation variation

### 11.2 Matching

```python
proceeding = proceeding_repo.get_by_az(extracted_az)
if proceeding:
    batch.proceeding_id = proceeding.id
    batch.case_id = proceeding.case_id
else:
    # Auto-create candidate proceeding
    court_level = infer_court_level(court_name)  # "Amtsgericht" → ag
    candidate = Proceeding(
        case_id=None,  # user confirms case in triage
        court_name=extracted_court_name,
        court_level=court_level,
        az_court=extracted_az,
        status=ProceedingStatus.ACTIVE,
    )
    # Stored as "proposed" — user confirms/links during triage
```

### 11.3 Internal ID fallback

If no Az is extractable but the lawyer's internal reference is (`Unser Zeichen: 8124/25`) and it matches a known `Case.id` — that attribution fires too. Vision doc §"Case IDs" covers this distinction.

---

## 12. Action item extraction

Cover letters typically say things like:

> Wir bitten Sie, Ihre Stellungnahme bis zum **30.04.2026** einzureichen.
> Die mündliche Verhandlung findet am **15.06.2026, 10:00 Uhr**, im Sitzungssaal 3 statt.

Each extracted obligation becomes an `ActionItem` linked to the cover letter as its `source_document_id`:

```
ActionItem {
    case_id: ADV-024-A,
    proceeding_id: <proc>,
    source_document_id: <cover letter>,
    action_type: deadline | court_date | response_required | filing_required,
    title: "Stellungnahme zu Klageerwiderung",
    due_date: 2026-04-30,
    location: "Sitzungssaal 3" (court_date only),
    status: open,
}
```

AI returns a list of `detected_actions` with confidence per item; low-confidence actions show in triage but require user confirmation before going live. High-confidence actions land directly.

---

## 13. Multi-bundle handling

Not every email is one bundle. Two real-world cases:

### 13.1 Email with multiple cover letters

Lawyer forwards a daily court digest: email contains Begleitschreiben A + its enclosures, then Begleitschreiben B + its enclosures.

Handling:
- Attachments are processed in order
- Each detected cover letter starts a new "bundle group"
- Subsequent non-cover attachments attach to the most recent cover as `parent`
- Triage UI groups by cover letter within the batch (see `docs/triage.md` §1)

### 13.2 Scanned PDF with multiple cover letters

Same pattern, just inside one PDF:

```
Pages 1-3:   Cover letter A + enclosure
Pages 4-6:   Cover letter B + enclosure + annex
Pages 7-10:  Cover letter C + enclosure
```

The slicer (§4) detects each new cover letter start; parent wiring cascades as with the email path.

---

## 14. Dedup — idempotency at every level

Re-running an ingest is safe and produces no duplicates.

### 14.1 Batch-level (Message-Id)

- `IngestBatch.message_id` (new column, Phase 3) stores RFC5322 Message-Id
- On ingest, check if a batch with this message_id already exists
- If yes: skip entirely, return existing batch
- Applies to both Gmail API and .eml upload paths
- Scan folder path has null message_id → falls through to attachment-level dedup

### 14.2 Attachment/slice-level (content_hash)

- SHA-256 of the raw file bytes stored in `Document.content_hash` (already exists)
- On create: check if a Document with this content_hash exists for the same case
- If yes in same case: link (don't create) — this handles "opposing counsel re-submitted the same exhibit"
- If yes in a different case: still create (separate matter) but flag the cross-case reference for the user

### 14.3 Bulk backfill replay

A user running backfill again with the same window should be a no-op in terms of ingest (all messages already seen) but may trigger a re-enrichment if AI models have changed. Re-enrichment is opt-in, not automatic.

---

## 15. Failure modes

Every failure surfaces in triage with context and a retry affordance. Never silent.

### 15.1 Gmail path

| Failure | Handling |
|---|---|
| OAuth token expired | Background refresh; if refresh fails, surface "Sign in to Gmail" prompt on next visit |
| OAuth revoked | Same as expired; requires user action |
| Rate limit hit | Exponential backoff; retry automatically; log a warning |
| Message fetch failed | Record batch as `failed` with error; individual retry button in UI |
| Attachment download failed | Document created with `pipeline_state=failed` (extract stage failed); retry button |

### 15.2 Scan folder path

| Failure | Handling |
|---|---|
| File format unsupported | Move to `failed/`, log reason |
| PDF password-protected | Move to `failed/`, log "password required"; future: UI to enter |
| Image conversion failed | Move to `failed/`, log exception |
| Slicing didn't converge | Present "treat as single document?" fallback in slicing UI |

### 15.3 .eml upload path

| Failure | Handling |
|---|---|
| Malformed .eml | Reject at upload, show error to user |
| Empty (no attachments, no body) | Accept but create a zero-attachment batch; still visible in triage |

### 15.4 Downstream (all paths)

| Failure | Handling |
|---|---|
| Docling conversion failed | `pipeline_stages.extract` → `failed`; document shows in triage with stage-level retry button |
| OCR returned empty content | Extract stage completes but `content=null`; triage flag `conversion_failed` |
| AI provider unavailable | AI pipeline stages stay `pending`; document arrives in triage without enrichment; retry once provider is back (see §23) |
| AI returned malformed JSON | Stage marked `failed`; logged for debugging; retry automatically up to 3 times |

---

## 16. Privacy model

Sanctuary's core promise is that your data stays on your machine. Ingest is the only subsystem that reaches outside the machine, so its privacy boundaries are explicit.

### 16.1 What leaves your machine

- **Gmail path only**: OAuth handshake tokens with Google; API requests to `gmail.googleapis.com` (Google sees that YOU are reading YOUR own inbox — which they already know)
- **Nothing else**: no message bodies posted anywhere; no attachments uploaded; no metadata exported; no telemetry

### 16.2 What stays on your machine

- All message bodies, all attachments, all Docling output
- All AI interactions (prompts go to the local AI provider per vision)
- OAuth refresh token (encrypted; see §2.1)
- All `IngestBatch`, `Document`, derived data

### 16.3 OAuth minimum

- Scope: `gmail.readonly` — read-only, no send, no modify
- Revocable at any time from the user's Google Account
- Token storage is local and encrypted

### 16.4 Scan folder + .eml upload

Fully offline. No network activity whatsoever beyond whatever the local AI provider does.

### 16.5 Future

If we add cloud sync or collaboration later, the privacy doc (see §0 cross-refs) will be updated. For v1, the rule is: **Gmail reads out; nothing goes in.**

---

## 17. Data model touchpoints

| Field | Populated by | Phase |
|---|---|---|
| `IngestBatch.source_type` | path (gmail / scan / manual) | 3 |
| `IngestBatch.received_at` | email Date header or file mtime | 3 |
| `IngestBatch.sender_email` | email From header | 3 |
| `IngestBatch.subject` | email Subject or scan filename | 3 |
| `IngestBatch.raw_source_path` | archived original | 3 |
| `IngestBatch.message_id` (new column) | RFC5322 Message-Id or null | 3 |
| `IngestBatch.case_id` | Proceeding → Case match or user confirm | 3 (AI) + 2 (user) |
| `IngestBatch.proceeding_id` | Az match or auto-create | 3 |
| `IngestBatch.status` | pipeline progression | 3 |
| `Document.ingest_batch_id` | FK set at creation | 3 |
| `Document.parent_id` | cover letter → enclosures wiring | 3 |
| `Document.role` | cover_letter / enclosure / standalone | 3/4 |
| `Document.court_relay` | AI detection | 4 |
| `Document.attributed_originator` | AI extraction from cover letter | 4 |
| `Document.document_type` | AI classification | 4 |
| `Document.significance_tier` | AI assignment | 4 |
| `Document.content`, `meta` | Docling | 3 |
| `Document.content_hash` | SHA-256 at create | 3 |
| `ActionItem` rows | extracted from cover letter content | 3/4 |
| `Proceeding.az_court` | Az extraction | 3 |
| `Proceeding.court_name`, `court_level` | inferred or user-confirmed | 3 |

### New column needed

- `IngestBatch.message_id` — add in Phase 3 startup migration (simple add-column)

---

## 18. Files to create / modify

### New

| File | Purpose |
|---|---|
| `app/services/ingest/__init__.py` | Module exports |
| `app/services/ingest/gmail.py` | Gmail API client, OAuth, backfill, continuous sync |
| `app/services/ingest/scan_folder.py` | Folder watcher, image→PDF conversion |
| `app/services/ingest/slicer.py` | Heuristic + AI slicing for scanned PDFs |
| `app/services/ingest/email_parser.py` | Shared RFC822 parser |
| `app/services/ingest/batch_orchestrator.py` | Creates IngestBatch, dedup, spawns downstream |
| `app/services/ingest/cover_letter_detector.py` | Heuristic + AI cover-letter detection |
| `app/services/ingest/originator_attributor.py` | AI-driven attribute_originator assignment |
| `app/services/ingest/proceeding_detector.py` | Az extraction, match-or-create |
| `app/api/ingest.py` | Routes: `/ingest/gmail/*`, `/ingest/upload`, `/ingest/slice/*` |
| `app/templates/pages/gmail_settings.html` | OAuth + allowlist + label filter config UI |
| `app/templates/pages/slicing_review.html` | The slicing UI (§4.4) |
| `app/tasks/gmail_sync.py` | Celery / background task for continuous sync |
| `app/tasks/scan_watcher.py` | Background task for folder watcher |
| `alembic/versions/<xxx>_add_message_id_to_ingest_batches.py` | Migration for §17 |
| `scripts/gmail_oauth_setup.py` | One-time CLI setup helper |

### Modified

| File | Change |
|---|---|
| `app/models/database.py` | Add `IngestBatch.message_id` column |
| `app/repositories/ingest_batch.py` | Add `get_by_message_id()` lookup |
| `app/services/ai_summary.py` | Already wires to Proceeding auto-triage; extend prompt for slicing and originator hints |
| `app/config.py` | Gmail OAuth client ID/secret config, scan folder path, poll cadence |
| `CLAUDE.md` | Add ingest setup note under Run section |

---

## 19. Phase progression map

| Phase | Implementation Status |
|-------|----------------------|
| **Phase 2** (triage) | ✅ Implemented - Manual .eml upload works end-to-end through triage |
| **Phase 3a** (Gmail core) | ✅ Implemented - Gmail OAuth + allowlist, bulk backfill, continuous sync |
| **Phase 3b** (scan folder) | ✅ Implemented - Folder watcher, single-document ingest |
| **Phase 3c** (slicing) | ✅ Implemented - Multi-doc slicing with 7 heuristics + AI refinement |
| **Phase 4** (AI intelligence) | ✅ Implemented - Cover letter detection, originator, proceeding, action items |

All phases complete as of April 2026.

---

## 20. Non-goals

- **Forwarded-to-app email address** — requires running a local SMTP server or reliable relay. Scope creep; Gmail API solves the same problem better.
- **Generic IMAP support** — Outlook/iCloud/other providers. Deferred until there's a concrete user need; Gmail is where the legal mail actually lives.
- **Automated unattended slicing** — slicing without user review. Mistakes here are expensive; user review is non-optional for v1.
- **Mobile capture app** — user can use phone's native scan app + a sync folder. No native mobile app in scope.
- **Email-send capability** — Sanctuary reads mail; it doesn't send mail. Compose and file-via-email are not on the roadmap.
- **Scraping / mining the user's whole inbox** — the allowlist is intentional; we ingest only from configured senders. No "find legal mail automatically" feature.
- **Re-enrichment on AI model changes** — automatic re-run of AI enrichment over all past documents when a model upgrades. Opt-in only; a v1.x enhancement.

---

## 21. Success criteria (VERIFIED April 2026)

All success criteria verified as implemented:

| Criterion | Status |
|-----------|--------|
| **Gmail flow** | ✅ Verified - OAuth, allowlist, backfill, continuous sync all working |
| **Scan flow** | ✅ Verified - Folder watcher + slicing within ~30s |
| **Slicing review** | ✅ Verified - UI with manual override |
| **Dedup** | ✅ Verified - message-id + content_hash |
| **Failure visibility** | ✅ Verified - Stage status + retry UI |
| **Privacy** | ✅ Verified - Local processing only |
| **Attribution correctness** | ✅ Verified - AI assigns via Phase 4 |
| **Proceeding match** | ✅ Verified - Auto-matching implemented |
| **Action items** | ✅ Verified - Date extraction working |

---

## Related docs

- `docs/vision.md` — north-star architecture, data model, principles
- `docs/triage.md` — where ingested batches land for user review (Phase 2)
- `docs/dashboard.md` — how ingested documents surface in the case command center (Phase 5+)

## 22. Slicing UI — keyboard shortcuts

The slicing review screen supports keyboard-first operation:

| Key | Action |
|---|---|
| `←` / `→` | Move focus to previous / next page thumbnail |
| `↑` / `↓` | Jump to previous / next document group |
| `c` | Insert a cut line before the focused page |
| `Backspace` / `Delete` | Remove the cut line before the focused page (merges with previous group) |
| `Enter` | Confirm all cuts and proceed to document creation |
| `Esc` | Cancel slicing and discard the uploaded file |
| `1` | Accept all AI-proposed cuts (`[keep all proposed cuts]`) |
| `0` | Treat entire PDF as a single document (reject all cuts) |
| `e` | Edit the focused document group's title/role before confirming |

---

## 23. AI-down graceful degradation

AI enrichment is **not** a blocker for completing triage. The pipeline stages can be in `pending` or `failed` state while a bundle is confirmed:

- Triage allows per-document metadata confirmation and bundle confirmation regardless of `pipeline_state`.
- If the AI provider is unreachable, documents arrive in triage with `pipeline_state=partial` (extraction done, AI stages pending). All form fields that would normally be AI-prefilled are empty — the user fills them manually.
- Once the AI provider comes back online, failed/pending stages can be retried via the per-stage retry button in the pipeline status chip (stepper UI). The retry re-runs only the failed or pending stages; already-completed stages are skipped.
- Force-confirming a bundle whose AI stages never ran is safe — the user's manual metadata edits are authoritative.

---

## 24. `content_hash` — not a unique constraint

`Document.content_hash` stores a SHA-256 of the raw file bytes for deduplication purposes. It is **not** a `UNIQUE` constraint in the database. The same file may legally appear in multiple cases (e.g., an exhibit submitted to two different proceedings). The dedup logic in `batch_orchestrator.py` checks for an existing document with the same hash **within the same case** and links rather than duplicates; cross-case matches still create a new row. Migrations must not add a UNIQUE index on this column.
