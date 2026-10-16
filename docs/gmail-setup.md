# Connecting Gmail

Sanctuary can read your lawyer's emails from Gmail, group them by the case reference in the subject line, and import the history oldest-first. This guide covers the one-time Google setup, connecting, importing, and re-importing for testing.

## What you are agreeing to

- **Read-only.** Sanctuary requests only `gmail.readonly`. It cannot change, label, move, send or delete anything in your mailbox, and it refuses any grant broader than that. A test fails the build if any code path ever calls a Gmail write method.
- **Local.** Mail is processed on your machine. If an active AI endpoint is on the public internet, Settings → Gmail and the Import page warn you before you import — point Sanctuary at a local endpoint first (Settings → AI & Models).
- **Encrypted credentials.** The Google token (and AI API keys) are stored encrypted with `SECRETS_ENCRYPTION_KEY`.
- **A local copy of fetched mail** is kept in `data/gmail_raw/` (gitignored, owner-only) so you can re-import without asking Gmail again. You can clear it from the Import page.

## 1. Before you start

1. **Set `SECRETS_ENCRYPTION_KEY`** in `.env`. `make setup` generates one; for a plain Docker deployment generate it yourself:

   ```bash
   openssl rand -base64 32 | tr '+/' '-_'        # 44 characters
   # or: docker run --rm ghcr.io/s3ntin3l8/sanctuary:edge \
   #       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   **Back it up.** Losing it makes stored Gmail/AI credentials unreadable (you would reconnect Gmail and re-enter AI keys). Sanctuary refuses to start if encrypted credentials are stored and the key is missing; with nothing stored it starts, but connecting Gmail or saving an AI key fails with a 503 until the key is set.
2. **Know your public URL.** Google only accepts an `https://` redirect URI (or `http://localhost`). Behind a reverse proxy use that URL, e.g. `https://sanctuary.example.org`.
3. **Redis must be running** (imports are background jobs). It is part of `docker compose` and `make run`.

## 2. Create a Google OAuth client (once)

In the [Google Cloud console](https://console.cloud.google.com/) (the OAuth screens are grouped under *APIs & Services* or *Google Auth Platform*, depending on your console version):

1. Create a project (any name) and enable the **Gmail API** (*APIs & Services → Library*).
2. **OAuth consent screen / Audience:** user type **External**, leave it in **Testing**, and add **your own Google account as a test user**. Do not publish it — the Gmail read scope is a restricted scope, and Google only verifies production apps through a security assessment you do not need for personal use.
3. **Data access / Scopes:** add `https://www.googleapis.com/auth/gmail.readonly`.
4. **Credentials → Create credentials → OAuth client ID**, type **Web application**. Under *Authorized redirect URIs* add exactly:

   ```
   https://<your-sanctuary-host>/api/ingest/gmail/oauth/callback
   ```

   (locally: `http://localhost:8000/api/ingest/gmail/oauth/callback`). Copy the **client ID**.
5. **Create a client secret.** Open the client (*Google Auth Platform → Clients → your client*) and, under **Client secrets**, click **Add secret**. An empty list just means none exists yet. Copy the value immediately: Google shows a secret in full only when it is created (afterwards only its last characters), so if you miss it, add another secret and delete the old one. If the client has no *Client secrets* section at all, it is not a **Web application** client (Android, iOS and Chrome clients have no secret) — create a new client of type *Web application*.

> **Testing mode expires your login every 7 days.** Google invalidates refresh tokens of apps in *Testing* after a week. Sanctuary then shows **Reconnect required** (Settings → Gmail) and stops polling until you click *Reconnect*. This is expected, not a bug.

## 3. Tell Sanctuary

As an admin, open **Settings → Gmail → Google OAuth client** and paste the client ID and secret. The **Redirect URI** field is pre-filled with the callback on the host you are browsing from; register exactly that value in Google. No restart is needed. The secret is stored encrypted (`SECRETS_ENCRYPTION_KEY`) and never shown again; leave the field empty on later saves to keep it.

Changing the client does not break existing connections (each stores its own token); click *Reconnect* to move a connection to the new client.

**Headless alternative:** set `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` and optionally `GMAIL_REDIRECT_URI` in `.env` and restart (`docker compose up -d` or `make run`). They are the fallback: anything saved in Settings wins.

## 4. Connect

1. **Settings → Gmail → Connect Gmail.**
2. Google shows *"Google hasn't verified this app"* — normal for your own Testing app. Choose *Advanced → Go to … (unsafe)*.
3. The consent screen must list only **"View your email messages and settings"**. If it asks for more, stop and cancel.
4. Back in Sanctuary, open **Settings → Gmail → What Sanctuary reads** and set the **sender allowlist** (comma-separated addresses or domains, e.g. `kanzlei-vogt.de`), a **label**, or both — at least one is required, so Sanctuary can never be pointed at your whole mailbox. Only mail from allowlisted senders (and with that label, if set) is ever read. Click **Preview matches** to see roughly how many messages the filters match before you rely on them. Sanctuary sends the label to Gmail's search quoted (`label:"<your entry>"`), so enter it as it appears in Gmail, spaces and `/` included.

Connecting sets the sync starting point to *now*: nothing older is pulled in automatically.

## 5. Import the history (oldest first)

Open **Import history** (Settings → Gmail → Import history, or *Import from Gmail* on the Triage page). It lives inside Settings, so the settings navigation stays on the left.
A running import is also shown in the **processing queue** (rail icon) and as a banner on **Triage**, both with a Stop button, and a toast tells you when it finishes.

1. **Refresh index.** Reads only the *headers* (sender, subject, date) of every allowlisted message, all time. Nothing is imported yet. The page then lists the case references found in subjects — file numbers like `8372/25` and court Aktenzeichen — oldest history first. Replies with no reference inherit the one in their thread; the rest are under *No reference*.
2. **Create the case first** for a reference you want filed automatically. The case ID must be the file number with `/` replaced by `-` (`8372/25` → `8372-25`); a court reference matches the Aktenzeichen of one of the case's proceedings. The table shows the matching case, or *no case yet*. Imported mail for a reference with no case lands in Triage unfiled.
3. **Import in small steps.** Expand a reference, then use *Import next N oldest* (up to 100), or tick individual messages and *Import selected*. Already-imported messages are skipped.
4. Every selected email is ingested right away, oldest first, and its documents are then processed **concurrently** (extraction and the AI stages overlap across emails). Only relationship detection waits for the earlier documents of its case to be enriched (the processing queue shows *Waiting for earlier documents of the case*), so replies still link to the letters they answer. **Stop** ends the run after the current message. Results appear in Triage.

## 6. Keeping up with new mail

"New mail" is whatever Gmail received after the **sync point**. Choose how Sanctuary handles it under *Settings → Gmail → New mail*:

- **Notify me** (the default for a new connection) checks every 5 minutes, reading only the *headers* of new mail, and never imports on its own. A banner on **Import history** and a notice on **Triage** say "N new messages" with **Review** (see the list, tick what you want, *Import selected*), **Import all**, **Skip** and **Check now**. Skipping moves the sync point past the listed mail without importing or deleting anything; it stays available under Import history.
- **Import automatically** imports new mail from your filters every 5 minutes, straight into Triage.
- **Off** does nothing in the background. *Sync now* imports new mail when you ask.

In notify mode the Sync card's button is *Check for new mail* (it looks, it doesn't import).
- **Change sync point…** (in the Sync card) moves the starting point (today or an earlier date) and forgets failed messages. It is never cleared, so the next sync can never pull the whole mailbox.

## 7. Re-importing for testing

| I want to… | Do this |
|---|---|
| Re-process one email | Delete its bundle in Triage, then import it again (the Import page shows it as not imported once the bundle is gone). |
| Start from scratch | *Settings → Data → Clear all data*, then import again. The header/index and the local mail cache survive the wipe, so the re-import is **offline** — no Gmail traffic and no valid token needed. |
| Force a fresh fetch from Gmail | *Import history → Clear cache*, then import. |
| Re-check the mailbox for new headers | *Refresh index* (only fetches what is new). |

Fetched mail is cached *before* it is ingested, so a message that failed to ingest can be re-imported after the bug is fixed.

## 8. Disconnecting

*Settings → Gmail → Disconnect* forgets the connection locally, revokes Sanctuary's access at Google, forgets the mailbox index and stops running imports. Already-imported mail stays; your allowlist is kept; the local mail cache stays until you clear it. You can also revoke access any time at <https://myaccount.google.com/permissions>.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Google: `Error 400: redirect_uri_mismatch` | the Redirect URI in Settings → Gmail (or `GMAIL_REDIRECT_URI`) differs from the URI in the OAuth client (scheme, host, path, trailing slash). |
| No client secret to copy (the *Client secrets* list is empty) | None has been created yet — click **Add secret** on the client and copy it right away (section 2, step 5). No *Client secrets* section at all means the client is not a *Web application*; create one. |
| Google: *Access blocked* / `access_denied` | Your account is not a **test user** on the consent screen (step 2). |
| **Reconnect required** | The token was revoked or expired (weekly while in *Testing*). Click *Reconnect*. |
| *"Google granted different permissions than requested"* | The grant included more than read-only Gmail. Reconnect and approve only the Gmail read scope. |
| 503 mentioning `SECRETS_ENCRYPTION_KEY`, or the app won't start | The key is missing or wrong (step 1). Restore the original key from your backup. |
| 503 "Redis is unreachable" when starting an import | Start Redis (`docker compose up -d redis`). |
| "Connect Gmail first" / "Set a sender allowlist or a label first" | Complete step 4. |
| Index finishes but a few messages are "couldn't be read" | Gmail rate-limited the batch; *Refresh index* again to retry them. |
| An import seems stuck on "waiting for …" | Sequential mode is waiting for that email's documents to finish processing (AI queue). It moves on by itself after 30 minutes, or press *Stop*. |

## Email sources, dates, and attachment review

One outer message is one bundle. Attached `.eml` messages remain nested transport
sources with their own headers and attachment lists; this source tree is separate
from the legal cover-letter/enclosure tree. Expand **Email sources and
attachments** in a bundle to inspect senders, recipients (including Bcc when
present), forwarding context, transmission dates, and original parts.

**Received** uses Gmail's mailbox-arrival timestamp. Email sent time, court
manifest time, and ingest time stay separate. Cached imports reuse receipt
metadata from the index or an owner-only metadata sidecar next to the `.eml`.
For uploaded mail or incomplete old caches, the UI explicitly labels the email
sent/ingest date as a fallback with mailbox arrival unknown.

Incoming mail with attachments does not create a body document. Its complete
cleaned current note supplies grouping, attribution, and case-reference context,
including for a single attachment. Long notes are read in windows and reduced to
routing hints. These notes are not evidence for claims, actions, or deadlines.
Body-only messages and the user's own writing remain documents, per source.

Manifest matching requires unique normalized filenames. Transmission membership
is constrained by source, timestamp, and source label; proceeding identity alone
cannot attach a document to a cover letter. Ambiguous matches remain standalone
for review. Manual groups and confirmed structure survive reruns.

Every part has an outcome: ingested, duplicate (linked to the existing document),
ignored embedded asset, empty, unsupported, failed, attached email, or removed.
Unnamed files get generated names; empty/unsupported parts remain visible even
when a message produces no documents. Original bytes can be downloaded from the
source panel. Acknowledge source issues or archive the bundle after review.

## Exact-set replay after a test workspace reset

The provenance migration preserves existing email groups and marks their source
identity for reimport. It cannot recover exact MIME part boundaries from the old
rows, so automatic regrouping waits for replay. Legacy receipt dates are labelled
as having unknown provenance rather than being inferred from the old value.
Acknowledging source issues does not recover attachment boundaries or resume
automatic grouping; the source panel continues to show that reimport is required.

Run these commands in the application's configured environment (inside the web
container for a Docker deployment). Capture contains only IDs, receipt metadata,
and hashes, and lives in `data/gmail_replay/`, which survives workspace clearing.

1. Quiesce imports, sync/beat, and all document workers; wait for active jobs to
   finish and keep the web UI unavailable during the reset/replay maintenance.
2. Capture the exact previously imported set, including completed no-document
   messages: `python scripts/gmail_replay.py capture --online`. Online mode only
   reads missing raw mail/receipt metadata. Resolve any reported missing or
   ambiguous identities before clearing. Existing ready captures are protected;
   `--replace-capture` is an explicit replacement, never a resume operation.
3. Run `python scripts/gmail_replay.py verify-capture`. It refuses to proceed if
   any selected cached message is missing or its hash changed.
4. Use the existing **Clear workspace / Clear all data** operation. It clears
   case data for every user, preserving accounts, settings, audit history,
   Gmail index, raw cache, and the replay manifest.
5. Run `python scripts/gmail_replay.py replay`. It imports only the captured IDs
   and uses cached raw bytes and recorded mailbox dates; it is idempotent and
   needs no Gmail traffic. Start document workers with beat still paused.
6. Run `python scripts/gmail_replay.py verify` until processing finishes. Counts
   distinguish completed, processing, duplicate-only, source-review-required,
   failed stages, and missing messages. `import_errors` separately lists immediate
   ingest failures. Exit 0 means reconciled, 1 means missing/failed imports or
   pipeline stages, and 2 means processing or review is still pending.
7. Review source ambiguities and inspect representative receipt dates and
   transmission groups before restoring normal sync/beat and web access.

The replay script itself never clears data. Do not capture again after clearing;
use the protected original manifest to resume or verify the same selection.
