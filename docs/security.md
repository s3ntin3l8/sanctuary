# Security: what is encrypted at rest, and what is not

Sanctuary holds privileged correspondence, so it helps to be exact about what the
application protects and what it leaves to the host. The short version: **the app
encrypts credentials and raw mail; everything else is plain text on disk, so put the
database and `data/` on an encrypted volume.**

## What the app encrypts

All of it with one Fernet key, `SECRETS_ENCRYPTION_KEY` (AES-128-CBC + HMAC).

| Data | Where | Marker |
|---|---|---|
| Gmail OAuth token, AI endpoint API keys, Gmail OAuth client secret | `user_settings` / `app_settings` JSON | `enc:v1:` prefix |
| Raw mail: the Gmail cache (messages and metadata sidecars) and the stored original `.eml` | `data/gmail_raw/`, `data/email_sources/` | `SNCENC1` file prefix |
| Full AI prompts and responses, **only while an admin switches capture on** | `data/ai_debug/**/*.md` | one `enc:v1:` line per call |

The key is **required**: the web app and every worker refuse to start without a valid
one, so nothing is ever written in the clear because a key was forgotten.

## What it does not encrypt

- **PostgreSQL.** Document text (`Document.content`), summaries, claims, passages,
  embeddings, chat history and contacts are stored as plain columns. Search
  (`ilike`, pgvector) needs them in the clear, so column encryption is not an option
  short of giving up search.
- **Stored documents and attachments** under `data/<case>/` and `data/_TRIAGE/`, scans
  under `data/scans/`, page thumbnails and slice files. They stay ordinary files you
  can open from the mount. Encrypting them in the application was considered and
  dropped: the key lives next to the data (see below), so it would only protect a copy
  of `data/` taken without `.env`, while costing every PDF/OCR/preview path a
  decrypt-to-temp step. (This is also why Paperless-ngx removed its optional document
  encryption in v3 and recommends disk encryption instead.)
- `data/ai_debug/runs.jsonl`: a metadata-only call index (timing, tokens, status), no
  document text, kept 30 days.

## Threat model

The key sits on the same host as the data (in `.env`). App-level encryption therefore
protects against:

- a copy of `data/` or a backup of it taken **without** `.env`,
- someone browsing the data folder for emails or tokens,
- a leaked cache or `.eml` on its own.

It does **not** protect against someone who has the whole host or the disk including
`.env`, nor against a reader of the Postgres volume or a `pg_dump`.

## Recommended setup

1. **Encrypt the volume that holds Postgres and `data/`.** LUKS on the data disk, or an
   encrypted ZFS dataset (on Proxmox: `zfs create -o encryption=on -o keyformat=passphrase
   rpool/sanctuary`). This is the actual protection for everything in the table above
   that the application leaves in the clear. Two details decide whether it works:
   - **Put Postgres on it too.** `db_data` in `docker-compose.yml` is a Docker named
     volume, so mounting the dataset at `data/` alone leaves the database (the document
     text, embeddings and chat history) on the unencrypted pool. Replace it with a bind
     mount on the dataset (`/path/to/dataset/postgres:/var/lib/postgresql/data`), or move
     Docker's `data-root` onto it.
   - **Unlock before starting the stack.** Both options need an explicit unlock after
     every reboot (`zfs load-key <dataset>` and mount; or LUKS via crypttab/keyfile).
     Until then the dataset is not mounted and a started stack would write into the bare
     mountpoint, a second `data/` tree that is shadowed when the real one mounts. Start
     compose only after the unlock (for example from a systemd unit ordered after it),
     not from `restart: unless-stopped` alone.
2. **Keep the key out of backups that contain `data/`.** Back up `SECRETS_ENCRYPTION_KEY`
   separately (password manager). Losing it makes stored credentials and raw mail
   unreadable; credentials must be re-entered and mail is re-fetched from Gmail where
   possible.
3. **Do not expose Postgres or Redis.** `docker-compose.yml` publishes both on
   `127.0.0.1` only, and `POSTGRES_PASSWORD` is required (no default). With
   `docker-compose.traefik.yml` neither is published at all.
4. Terminate TLS in front of the app (Traefik compose, or your reverse proxy).

## Rotating the key

1. Generate a new key and put it in `SECRETS_ENCRYPTION_KEY`; put the **old** key in
   `SECRETS_ENCRYPTION_KEY_PREVIOUS` (comma-separate several).
2. Restart. Startup samples one stored mail file and one credential and starts the
   reseal sweep (`reseal_stored_secrets_task`); it also runs daily. Wait for the
   `Reseal: encrypted=… rotated=… undecryptable=…` log line. It is written by the
   **maintenance worker** (not the web app) and only when something changed or could not
   be read, so a clean sweep prints nothing. The sweep reads and decrypts every stored
   mail file, which takes a while on a large mailbox.
3. Remove `SECRETS_ENCRYPTION_KEY_PREVIOUS`. `undecryptable` should be 0; anything
   counted there is sealed with a key that is in neither variable.

If stored data is sealed with a key that is not configured, startup logs one error that
names `SECRETS_ENCRYPTION_KEY_PREVIOUS`.

## AI call dumps

Prompts contain document text, so full dumps are **off by default** (Settings → AI →
Logs, admin). While on, each AI call is recorded as one encrypted line and deleted after
7 days; the call list (no text) is always kept for 30 days.

Error text: a pipeline stage error is cut to one bounded line without quoted model
input when it is written, and the call index's `error` field is sanitised at its
callers. Warning and error logs are **not** filtered: many still print the raw
exception, which for a failed JSON or validation parse can quote the model's answer.
Treat the server log as sensitive too (the encrypted volume covers it).

## Upgrading

- `SECRETS_ENCRYPTION_KEY` and `POSTGRES_PASSWORD` must be set. `make setup` generates
  both into `.env`; for a plain Docker deployment set them yourself.
- An existing database keeps the password it was created with: change it with
  `ALTER USER <POSTGRES_USER> PASSWORD '…'` (`sanctuary` unless you changed it) before
  changing `POSTGRES_PASSWORD`.
- Plaintext mail files and credentials from before encryption are sealed by the first
  reseal sweep; plaintext AI dumps are deleted at startup.
