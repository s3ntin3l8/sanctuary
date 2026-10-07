# Sanctuary — Settings

Companion document to `docs/specs/00_vision.md` §UI (⚙ rail icon). Covers all settings surfaces: Gmail OAuth ingest configuration, AI provider selection, appearance preferences, data maintenance, and per-user persistent state.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

| Layer | Status |
|---|---|
| Settings shell — `frontend/src/features/settings/SettingsLayout.tsx` (grouped side nav; admin-only tabs hidden for regular users) | ✅ |
| Page routes (`app/api/settings_page.py`): `/settings` → `/settings/account`, `/settings/{account,gmail,identity,ai,appearance,data,export}`, `/admin/users` — all return `spa_index()`; identity/ai/data/export/admin are admin-only | ✅ |
| **Account tab** — `AccountPage.tsx`: profile, e-mail, password (`/api/v1/settings/account`, `PUT …/profile`, `PUT …/email`, `PUT …/password`) | ✅ |
| **Gmail tab** — `GmailPage.tsx`: OAuth flow, allowlist, label filter, backfill | ✅ |
| **Identity tab** — `IdentityPage.tsx`: own name, own parties, user context (`GET`/`PUT /api/v1/settings/identity`) | ✅ |
| **AI tab** — `AiPage.tsx`: endpoint instances, per-role model selection (chat/embed/ocr), test connection, reindex, rebuild-index, extraction engine, worker/OCR concurrency, debug redaction | ✅ |
| **Appearance tab** — `AppearancePage.tsx`: theme (light/dark), dashboard-card visibility, timezone (admin) | ✅ |
| **Data tab** — `DataPage.tsx`: database stats, reset-enrichment, clear-all-data, debug logs | ✅ |
| **Export tab** — `ExportPage.tsx`: download link to `GET /api/v1/settings/data/export` | ✅ |
| **Admin › Users** — `frontend/src/features/admin/AdminUsersPage.tsx`: create / activate / role / password reset / delete / reassign cases / signup toggle (`/api/v1/admin/users*`, `PUT /api/v1/admin/signup`) | ✅ |
| `UserSettings` model — single `settings_json` JSON blob per `user_id` | ✅ |
| AI provider auto-detection (`ai_provider.py`) — Ollama / LMStudio / OpenAI discriminated by API fingerprinting | ✅ |
| `GET /api/v1/settings/ai/instances/{instance_id}/models` — live model list from an endpoint (`useInstanceModels`) | ✅ |
| `POST`/`PUT`/`DELETE /api/v1/settings/ai/instances[/{instance_id}]` — manage endpoints (base URL, API key, label); `PUT /api/v1/settings/ai/roles/{role}` — bind a model to the chat/embed/ocr role | ✅ |
| `POST /api/v1/settings/ai/rebuild-index` — resize `document_chunks.embedding` column, reindex all docs; progress via `GET …/reindex/status` | ✅ |
| `POST /api/v1/settings/ai/reindex` — quick reindex without DDL change | ✅ |
| `POST /api/v1/settings/ai/instances/{instance_id}/test` — connection probe, result shown as a toast (`useTestInstance`) | ✅ |
| `PUT /api/v1/settings/appearance/theme` — persist theme to `settings_json.theme` (`useSaveTheme`) | ✅ |
| `PUT /api/v1/settings/appearance/dashboard-cards` — persist card visibility (`useSaveDashboardCards`) | ✅ |
| `POST /api/v1/settings/data/reset-enrichment` — wipe enrichment fields, re-queue (`useMaintenance`) | ✅ |
| Default dashboard view persistence | — (not in the SPA; `CasePage.tsx` keeps the view in the URL) |
| Active proceeding per case — `GET /api/v1/cases/{case_id}?proceeding=` persists the selection via `user_settings_service.set_active_proceeding` | ✅ |
| `PUT /api/v1/settings/gmail/filters` — save allowlist + label filter (`useSaveGmailFilters`) | ✅ |
| `GET /api/ingest/gmail/oauth/start` → `GET /api/ingest/gmail/oauth/callback` (`app/api/ingestion_settings.py`; the start URL is returned in `GmailView.oauth_start_url`) | ✅ |
| `POST /api/v1/settings/gmail/backfill` — enqueue `run_gmail_backfill` task (`useGmailBackfill`) | ✅ |
| Database vacuum via settings UI | ❌ not implemented — non-goal for v1 |
| Per-user settings (multi-user) | ✅ `UserSettings` is keyed by `user_id`; AI/identity/data/export/timezone are global admin settings, account/gmail/appearance are per user |

### Implementation Deviations

| Feature | Vision §UI / `00_vision.md` | Code | Status |
|---|---|---|---|
| Settings location | "⚙ icon on the sidebar rail" | `/settings/*` accessible from `⚙` in the 56 px icon rail | ✅ |
| Single-user model | Implicit — "privacy first, local" | `UserSettings` keyed by `user_id`; admin role gates the global tabs | ✅ Accepted |
| AI provider type | "Ollama" (default) | Auto-detect: Ollama / LMStudio / OpenAI based on API fingerprint | ✅ |
| Appearance defaults | Dark slate aesthetic | Default `theme: "dark"` in `UserSettings.settings_json` | ✅ |
| Settings scope | Single-user implicit | Account, Gmail, Appearance (theme/cards) per user; AI, Identity, Data, Export, timezone and user admin are admin-only global settings | ✅ Accepted |

---

## The core shift

**Traditional admin panel:** a configuration screen with dozens of fields grouped by area, where the user configures the system once on install and never returns.

**Sanctuary Settings:** a thin, focused panel that answers one question per tab — "Is Gmail connected?", "Which AI model am I running?", "How should the dashboard look?". Settings are stored as a JSON blob on a single `UserSettings` row; defaults are production-safe. The AI tab is the most visited — users reconfigure it as they experiment with different local models.

---

## Layout overview

```
⚙ Settings                       local · nothing leaves this device

 WORKSPACE      Account · Appearance
 INTELLIGENCE   AI & Models* · Identity & Context*
 GMAIL          Gmail
 DATA           Data* · Export*
 ADMIN          Users*                       * admin-only (hidden otherwise)

┌─ Gmail ──────────────────────────────────────────────────────────────────┐
│  Gmail Connection                                                         │
│  ○ Not connected   [Connect Gmail]                                        │
│                                                                           │
│  Sender Allowlist                                                         │
│  [ra.mueller@kanzlei.de, kanzlei-schmidt.com]                             │
│                                                                           │
│  Label Filter (optional)                                                  │
│  [Sanctuary]                                                              │
│                                                                           │
│  [Save]          Backfill: [30 days] [90 days] [180 days]                 │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 1. `UserSettings` model

```
UserSettings
  id          INTEGER PK
  user_id     INTEGER  FK → users.id, NOT NULL, UNIQUE
  settings_json JSON nullable
  updated_at  DATETIME  auto-updated

settings_json shape (defaults):
{
  "theme": "dark",                // "light" | "dark" | "auto"
  "dashboard_cards": {
    "action_items": true,
    "costs": true,
    "documents": true
  },
  "ai": {
    "base_url": "http://127.0.0.1:11434",
    "provider": "auto",           // "ollama" | "lmstudio" | "openai" | "auto"
    "api_key": "not-needed",
    "summary_model": "qwen3.5:9b",
    "embed_model": "nomic-embed-text:v1.5",
    "embed_dim": 768,
    "user_context": ""            // optional free-text injected into all prompts
  },
  "gmail_allowlist": [],          // list[str] — email or domain patterns
  "gmail_label_filter": "",       // optional Gmail label to filter synced messages
  "gmail_credentials_json": null, // OAuth token blob (set by callback)
  "gmail_connected_at": null      // ISO datetime string
}
```

`get_effective_config(db)` in `app/services/ai_config.py` merges `settings_json.ai` with environment variable defaults (`AI_BASE_URL`, `AI_SUMMARY_MODEL`, `AI_EMBED_MODEL`, `AI_EMBED_DIM`) — database values win over env when set.

---

## 2. Tab: Gmail

**Routes:** `GET /settings/gmail` (SPA, `GmailPage.tsx`) · `GET /api/v1/settings/gmail` · `PUT /api/v1/settings/gmail/filters` · `GET /api/ingest/gmail/oauth/start` · `GET /api/ingest/gmail/oauth/callback` · `POST /api/v1/settings/gmail/backfill`

**Sections:**

| Section | What it does |
|---|---|
| **Gmail Connection** | Shows OAuth status (`gmail_credentials_json` non-null = connected). `[Connect Gmail]` navigates to `GmailView.oauth_start_url`. |
| **Sender Allowlist** | Comma-separated email addresses or domains. Saved to `settings_json.gmail_allowlist`. Only messages from allowlisted senders are synced. |
| **Label Filter** | Optional Gmail label name. If set, only messages with this label are synced. |
| **Backfill** | `[30d] [90d] [180d]` buttons each POST to `/api/v1/settings/gmail/backfill` with `{days: N}` (`useGmailBackfill`); enqueues `run_gmail_backfill` for the current user. |

### Gmail OAuth state machine

```
[Connect Gmail] ──► GET /api/ingest/gmail/oauth/start
                    │  Sets session cookie oauth_state = random 32-byte token
                    │  Generates Google OAuth consent URL
                    │  Redirects to Google
                    ▼
              Google consent screen
                    │
                    ▼
       GET /api/ingest/gmail/oauth/callback?code=...&state=...
                    │  Validates state cookie (CSRF guard)
                    │  Fetches token via flow.fetch_token(code=code)
                    │  Writes credentials to settings_json.gmail_credentials_json
                    │  Redirects back to /settings/gmail
```

On reconnect (token refresh), the callback overwrites the previous credentials. The Celery task `sync_gmail_incremental` uses the stored credentials for continuous sync.

---

## 3. Tab: AI

**Routes:** `GET /settings/ai` (SPA, `AiPage.tsx`, admin-only) · `GET /api/v1/settings/ai` · `POST`/`PUT`/`DELETE /api/v1/settings/ai/instances[/{instance_id}]` · `POST …/instances/{instance_id}/test` · `GET …/instances/{instance_id}/models` · `PUT /api/v1/settings/ai/roles/{role}` · `GET /api/v1/settings/ai/health` · `POST /api/v1/settings/ai/reindex` · `POST /api/v1/settings/ai/rebuild-index` · `GET …/reindex/status`

AI settings are a list of **endpoint instances** (label, base URL, API key, detected models) plus three **roles** (`chat`, `embed`, `ocr`), each bound to one instance + model.

**Sections:**

| Section | What it does |
|---|---|
| **Connection** | Per endpoint: `base_url` field + `[Test]` → `POST /api/v1/settings/ai/instances/{instance_id}/test` (`useTestInstance`); result shown as a toast, and the endpoint's model list (`useInstanceModels`) is refetched. |
| **Provider** | `provider` select (`auto` / `ollama` / `lmstudio` / `openai`). `auto` fingerprints the endpoint: if `/v1/models` returns `{"object":"list"}`, it's LMStudio-compatible; if `/api/tags` returns Ollama model list, it's Ollama; fallback is Ollama. |
| **API Key** | Only visible/required for OpenAI. LMStudio/Ollama use `"not-needed"`. |
| **Summary Model** | `<select>` populated by `GET /api/v1/settings/ai/instances/{instance_id}/models` from the live endpoint; saved via `PUT /api/v1/settings/ai/roles/chat`. Default: `qwen3.5:9b`. |
| **Embedding Model** | Separate `<select>` from the same model list; saved via `PUT /api/v1/settings/ai/roles/embed`. Default: `nomic-embed-text:v1.5`. |
| **Embedding Dimensions** | Integer field. Default: 768 (matches `nomic-embed-text`). Must match the model's actual output size; mismatch causes pgvector dimension errors. |
| **User Context** | Lives on the Identity tab (`PUT /api/v1/settings/identity`): optional free-text injected into all AI prompts as extra context (e.g. "This is a German family law case"). |
| **[Save]** | `PUT /api/v1/settings/ai/instances/{instance_id}` (endpoint) / `PUT /api/v1/settings/ai/roles/{role}` (model binding). Saves and reloads the chat/embed/ocr providers from the DB. |
| **[Reindex]** | `POST /api/v1/settings/ai/reindex` (`useReindex`). Quick reindex of all documents using current embed model (no DDL change). |
| **[Rebuild Index]** | `POST /api/v1/settings/ai/rebuild-index` (`useReindex`; progress polled via `useReindexStatus`). Clears `document_chunks` and resizes its `embedding` column (`ALTER COLUMN ... TYPE vector(N)`) to the new `embed_dim`, then reindexes all documents. **Destructive** — use when changing embedding model or dimensions. |

---

## 4. Tab: Appearance

**Routes:** `GET /settings/appearance` (SPA, `AppearancePage.tsx`) · `GET /api/v1/settings/appearance` · `PUT /api/v1/settings/appearance/theme` · `PUT /api/v1/settings/appearance/dashboard-cards` · `PUT /api/v1/settings/appearance/timezone` (admin)

| Setting | `settings_json` key | Values |
|---|---|---|
| Theme | `theme` | `"light"` / `"dark"` |
| Timezone (admin, global) | `timezone` | IANA name from `AppearanceView.timezone_choices` |
| Dashboard card visibility | `dashboard_cards.{action_items,costs,documents}` | `true` / `false` |

Theme is applied on the SPA shell (`frontend/src/theme.ts`, toggled from the rail and ⌘K) and persisted with `useSaveTheme`.

A persisted default dashboard view is not in the SPA — `CasePage.tsx` keeps the active view in the URL.

---

## 5. Tab: Data

**Routes:** `GET /settings/data` (SPA, `DataPage.tsx`, admin-only) · `GET /api/v1/settings/data` · `POST /api/v1/settings/data/reset-enrichment` · `POST /api/v1/settings/data/clear-all-data` · `GET /api/v1/settings/data/debug-logs[/view]`

**Sections:**

| Section | What it does |
|---|---|
| **Database Stats** | Read-only: DB file size (MB), document count, case count, claim count, cost count. Returned by `GET /api/v1/settings/data` (`DataView`). |
| **Reset Enrichment** | `POST /api/v1/settings/data/reset-enrichment` (`useMaintenance`). Wipes AI-generated fields (summary, significance tier, cost_delta, etc.) on all documents and re-queues them for enrichment. Use when switching AI models. |

Database vacuum is **not exposed** in v1 — Postgres autovacuum handles this; explicit vacuum is a non-goal.

---

## 6. Known gaps

| Gap | Remediation |
|---|---|
| Database vacuum not exposed | Non-goal for v1 — Postgres autovacuum handles this; add a manual `VACUUM` endpoint only if storage concerns arise. |
| Per-user AI / identity / data settings | Non-goal for v1 — these tabs are global and admin-only. |

---

## 7. Empty and error states

| Situation | What renders |
|---|---|
| Gmail not connected | Connection section shows "Not connected" + `[Connect Gmail]` button |
| AI provider unreachable | `POST /api/v1/settings/ai/instances/{instance_id}/test` reports `ok: false`; error toast; model selects show "No models found — check connection" |
| Rebuild-index DDL failure | `POST /api/v1/settings/ai/rebuild-index` returns a generic `{detail, code}` error (DDL detail is not leaked) shown as a toast |
| Settings not yet persisted (first run) | `_get_or_create()` creates a `UserSettings` row with defaults; no error |
| `settings_json` null | All reads coalesce to `{}` → env var defaults applied by `get_effective_config` |

---

## 9. Phase progression

| Phase | What landed |
|---|---|
| Phase 1 | `UserSettings` schema |
| Phase 2 | Appearance settings (theme) |
| Phase 3 | Gmail OAuth + allowlist + label filter |
| Phase 4 | AI config tab (model selection, embed dim) |
| Phase 5 | Active-proceeding persistence (`GET /api/v1/cases/{case_id}?proceeding=`) |
| Phase 6 | Maintenance tab (reset-enrichment) |

---

## 10. Non-goals

- No per-user AI, identity, data or export settings — these are global and admin-only.
- No plugin system or per-tab extension points.
- No database vacuum endpoint in v1.
- No settings export/import (portable JSON backup of `settings_json` out of scope).
- No AI model performance benchmarks or comparison view.
- No scheduled tasks management UI (Celery beat schedule is static; no UI to add/remove).

---

## 11. Verification

**Manual:**
1. `make run` → open `/settings/gmail` → "Not connected" state visible; `[Connect Gmail]` present.
2. `/settings/ai` → `[Test]` on an endpoint with Ollama running → success toast; model selects populate.
3. Toggle theme to "light" from the rail → `/settings/appearance` reflects the choice after reload.
4. `/settings/data` → DB stats render with real counts.

**Automated:**
- `tests/integration/test_settings_routes.py` — data stats, maintenance, appearance, identity, gmail, AI instances/roles/reindex, admin gating; `test_settings_pages_are_spa_routes` checks the `/settings/*` page routes.
- `tests/integration/test_admin_and_account.py` — `/api/v1/admin/*` and `/api/v1/settings/account/*`.
- `frontend/src/features/settings/SettingsPages.test.tsx` — settings pages (vitest).

---

## 12. Success criteria

- All `/settings/*` page routes return the SPA shell for a signed-in user; admin tabs return 403 for regular users.
- `PUT /api/v1/settings/ai/roles/{role}` saves and reloads the provider; a subsequent `…/instances/{instance_id}/test` reflects the new endpoint.
- OAuth flow completes without `state` mismatch; `GmailView.connected_at` is set post-callback.
- `POST /api/v1/settings/ai/rebuild-index` with a new `embed_dim` resizes `document_chunks.embedding` (and `claims.embedding`) to the new dimension; all documents reindexed.
- Theme persistence: switching to "light" persists across page reloads.

---

## Related docs

- `docs/specs/00_vision.md` §UI — sidebar rail layout, ⚙ settings entry
- `docs/specs/00a_ingest.md` — Gmail sync pipeline (settings feeds into the ingest configuration)
- `docs/specs/07_case_chat.md` — AI provider used by chat; embedding model feeds semantic retrieval
- `docs/specs/09_timeline.md` — `default_view` in Appearance determines which view opens first
