# Sanctuary — Settings

Companion document to `docs/specs/00_vision.md` §UI (⚙ rail icon). Covers all settings surfaces: Gmail OAuth ingest configuration, AI provider selection, appearance preferences, data maintenance, and per-user persistent state.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

| Layer | Status |
|---|---|
| Settings shell — `pages/settings/_layout.html` with 4-tab nav | ✅ |
| Page routes (`settings_page.py`): `/settings`, `/settings/gmail`, `/settings/ai`, `/settings/appearance`, `/settings/data` | ✅ |
| **Gmail tab** — OAuth flow, allowlist, label filter, backfill | ✅ |
| **AI tab** — provider detection, model selection, embed config, test connection, reindex, rebuild-index | ✅ |
| **Appearance tab** — theme toggle (light/dark/auto), default dashboard view, dashboard-card visibility | ✅ |
| **Data tab** — database stats, reset-enrichment | ✅ |
| `UserSettings` model — single `settings_json` JSON blob per `user_id` | ✅ |
| AI provider auto-detection (`ai_provider.py`) — Ollama / LMStudio / OpenAI discriminated by API fingerprinting | ✅ |
| `GET /api/settings/ai/models` — live model list from connected provider | ✅ |
| `POST /api/settings/ai/config` — save base URL, provider, API key, models, user context | ✅ |
| `POST /api/settings/ai/rebuild-index` — drop + recreate `document_vectors`, reindex all docs | ✅ |
| `POST /api/settings/ai/reindex` — quick reindex without DDL change | ✅ |
| `POST /api/settings/ai/test` — connection probe, returns inline HTMX toast | ✅ |
| `POST /api/settings/appearance/theme` — persist theme to `settings_json.theme` | ✅ |
| `POST /api/settings/appearance/dashboard-cards` — persist card visibility | ✅ |
| `POST /api/settings/maintenance/reset-enrichment` — wipe enrichment fields, re-queue | ✅ |
| `POST /api/user-settings/dashboard-view` — persist default view (`graph`/`timeline`/`truth`/`fin`) | ✅ |
| `POST /api/user-settings/active-proceeding/{case_id}` — persist active proceeding per case | ✅ |
| `POST /api/ingest/settings/update` — save allowlist + label filter | ✅ |
| `GET /api/ingest/gmail/oauth_start` → `GET /api/ingest/gmail/oauth/callback` | ✅ |
| `POST /api/ingest/gmail/backfill` — enqueue `run_gmail_backfill` task | ✅ |
| `app/api/settings_ai.py` — **dead code** duplicate | ❌ must delete — see §Known gaps |
| Database vacuum via settings UI | ❌ not implemented — non-goal for v1 |
| Per-user settings (multi-user) | ❌ single-user only (`user_id = "single_user"`) |

### Implementation Deviations

| Feature | Vision §UI / `00_vision.md` | Code | Status |
|---|---|---|---|
| Settings location | "⚙ icon on the sidebar rail" | `/settings/*` accessible from `⚙` in the 56 px icon rail | ✅ |
| Single-user model | Implicit — "privacy first, local" | `user_id = "single_user"` hard-coded | ✅ Accepted |
| AI provider type | "Ollama" (default) | Auto-detect: Ollama / LMStudio / OpenAI based on API fingerprint | ✅ |
| Appearance defaults | Dark slate aesthetic | Default `theme: "dark"` in `UserSettings.settings_json` | ✅ |
| `settings_ai.py` duplicate | Not specified | Declared `prefix="/api/settings/ai"` — same as `settings_ai_config.py`; not imported anywhere → dead code | ⚠ must delete |

---

## The core shift

**Traditional admin panel:** a configuration screen with dozens of fields grouped by area, where the user configures the system once on install and never returns.

**Sanctuary Settings:** a thin, focused panel that answers one question per tab — "Is Gmail connected?", "Which AI model am I running?", "How should the dashboard look?". Settings are stored as a JSON blob on a single `UserSettings` row; defaults are production-safe. The AI tab is the most visited — users reconfigure it as they experiment with different local models.

---

## Layout overview

```
⚙ Settings

 [Gmail]  [AI]  [Appearance]  [Data]

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
  user_id     TEXT  NOT NULL, UNIQUE  default "single_user"
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

**Routes:** `GET /settings/gmail` (page) · `POST /api/ingest/settings/update` (form) · `GET /api/ingest/gmail/oauth_start` · `GET /api/ingest/gmail/oauth/callback` · `POST /api/ingest/gmail/backfill`

**Sections:**

| Section | What it does |
|---|---|
| **Gmail Connection** | Shows OAuth status (`gmail_credentials_json` non-null = connected). `[Connect Gmail]` redirects to `oauth_start`. |
| **Sender Allowlist** | Comma-separated email addresses or domains. Saved to `settings_json.gmail_allowlist`. Only messages from allowlisted senders are synced. |
| **Label Filter** | Optional Gmail label name. If set, only messages with this label are synced. |
| **Backfill** | `[30d] [90d] [180d]` buttons each POST to `/api/ingest/gmail/backfill` with `days=N`; enqueues `run_gmail_backfill.delay("single_user", days=N)`. |

### Gmail OAuth state machine

```
[Connect Gmail] ──► GET /api/ingest/gmail/oauth_start
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

**Routes:** `GET /settings/ai` (page) · `POST /api/settings/ai/test` · `GET /api/settings/ai/models` · `POST /api/settings/ai/config` · `POST /api/settings/ai/reindex` · `POST /api/settings/ai/rebuild-index`

**Sections:**

| Section | What it does |
|---|---|
| **Connection** | `base_url` field + `[Test connection]` → POST `/api/settings/ai/test`. Returns inline HTMX toast (✓ green or ✗ red). On success, fires `HX-Trigger: refresh-models` to auto-load models. |
| **Provider** | `provider` select (`auto` / `ollama` / `lmstudio` / `openai`). `auto` fingerprints the endpoint: if `/v1/models` returns `{"object":"list"}`, it's LMStudio-compatible; if `/api/tags` returns Ollama model list, it's Ollama; fallback is Ollama. |
| **API Key** | Only visible/required for OpenAI. LMStudio/Ollama use `"not-needed"`. |
| **Summary Model** | `<select>` populated by `GET /api/settings/ai/models` from the live provider. Default: `qwen3.5:9b`. |
| **Embedding Model** | Separate `<select>` from the same model list. Default: `nomic-embed-text:v1.5`. |
| **Embedding Dimensions** | Integer field. Default: 768 (matches `nomic-embed-text`). Must match the model's actual output size; mismatch causes `vec0` errors. |
| **User Context** | Optional free-text injected into all AI prompts as extra context (e.g. "This is a German family law case"). |
| **[Save Config]** | POST to `/api/settings/ai/config`. Saves and calls `ai_provider.reload_from_db(db)`. |
| **[Reindex]** | POST to `/api/settings/ai/reindex`. Quick reindex of all documents using current embed model (no DDL change). |
| **[Rebuild Index]** | POST to `/api/settings/ai/rebuild-index`. Drops and recreates `document_vectors` virtual table with new `embed_dim`, then reindexes all documents. **Destructive** — use when changing embedding model or dimensions. |

---

## 4. Tab: Appearance

**Routes:** `GET /settings/appearance` (page) · `POST /api/settings/appearance/theme` · `POST /api/settings/appearance/dashboard-cards`

| Setting | `settings_json` key | Values |
|---|---|---|
| Theme | `theme` | `"light"` / `"dark"` / `"auto"` |
| Default dashboard view | persisted via `POST /api/user-settings/dashboard-view` | `"graph"` / `"timeline"` / `"truth"` / `"fin"` |
| Dashboard card visibility | `dashboard_cards.{action_items,costs,documents}` | `true` / `false` |

Theme is applied at page load from `settings_json.theme`; the CSS variable switch is handled in the base template.

`POST /api/user-settings/dashboard-view` persists the per-user default view. This is the same endpoint used by the case dashboard when the user switches view via the top-bar tabs or keyboard shortcuts.

---

## 5. Tab: Data

**Routes:** `GET /settings/data` (page) · `POST /api/settings/maintenance/reset-enrichment`

**Sections:**

| Section | What it does |
|---|---|
| **Database Stats** | Read-only: DB file size (MB), document count, case count, claim count, cost count. Computed by `settings_page.py:_stats()` on each page load. |
| **Reset Enrichment** | POST to `/api/settings/maintenance/reset-enrichment`. Wipes AI-generated fields (summary, significance tier, cost_delta, etc.) on all documents and re-queues them for enrichment. Use when switching AI models. |

Database vacuum is **not exposed** in v1 — the SQLite WAL auto-checkpoints; explicit vacuum is a non-goal.

---

## 6. Known gaps

| Gap | Remediation |
|---|---|
| `app/api/settings_ai.py` (39 LoC) is dead code — declares `prefix="/api/settings/ai"` with `/test` and `/reindex` but is **not imported** in `app/main.py` or `app/api/__init__.py` | Delete the file as part of "clean as you go". No runtime impact since it was never registered. |
| Database vacuum not exposed | Non-goal for v1 — SQLite WAL auto-checkpoints; add `VACUUM` endpoint only if storage concerns arise. |
| Multi-user settings | Non-goal for v1 — `user_id = "single_user"` is hard-coded everywhere. |

---

## 7. Empty and error states

| Situation | What renders |
|---|---|
| Gmail not connected | Connection section shows "Not connected" + `[Connect Gmail]` button |
| AI provider unreachable | `/api/settings/ai/test` returns red toast; model selects show "No models found — check connection" |
| Rebuild-index DDL failure | `/api/settings/ai/rebuild-index` returns red toast with error detail |
| Settings not yet persisted (first run) | `_get_or_create()` creates a `UserSettings` row with defaults; no error |
| `settings_json` null | All reads coalesce to `{}` → env var defaults applied by `get_effective_config` |

---

## 8. Files that will change

**Deleted (clean as you go):**
- `app/api/settings_ai.py` (39 LoC) — dead code duplicate of `settings_ai_config.py`; not imported anywhere.

**Modified (documentation cross-reference):**
- `docs/specs/00_vision.md §UI:318` (⚙ rail icon row) — add "See `docs/specs/10_settings.md`".

No other code changes are required for this spec.

---

## 9. Phase progression

| Phase | What landed |
|---|---|
| Phase 1 | `UserSettings` schema |
| Phase 2 | Appearance settings (theme) |
| Phase 3 | Gmail OAuth + allowlist + label filter |
| Phase 4 | AI config tab (model selection, embed dim) |
| Phase 5 | Dashboard-view persistence (`/api/user-settings/dashboard-view`) |
| Phase 6 | Maintenance tab (reset-enrichment) |

---

## 10. Non-goals

- No multi-user settings (single-user app; `user_id = "single_user"` everywhere).
- No password authentication or session management (local installation, trusted host).
- No plugin system or per-tab extension points.
- No database vacuum endpoint in v1.
- No settings export/import (portable JSON backup of `settings_json` out of scope).
- No AI model performance benchmarks or comparison view.
- No scheduled tasks management UI (Celery beat schedule is static; no UI to add/remove).

---

## 11. Verification

**Manual:**
1. `make run` → open `/settings/gmail` → "Not connected" state visible; `[Connect Gmail]` present.
2. `/settings/ai` → `[Test connection]` with Ollama running → green toast; model selects populate.
3. Change theme to "light" → page reloads in light mode; `/settings/appearance` reflects choice.
4. `/settings/data` → DB stats render with real counts.
5. Confirm `app/api/settings_ai.py` is deleted and `pytest -q` stays green (post-deletion).

**Automated:**
- `tests/integration/test_settings_routes.py` (if present — verify `GET /settings/*` returns 200).
- No route exists for the deleted `settings_ai.py` → existing tests should not reference it.

---

## 12. Success criteria

- All four `/settings/*` page routes return 200 without authentication.
- `POST /api/settings/ai/config` saves and reloads the AI provider; subsequent `/test` reflects the new provider.
- OAuth flow completes without `state` mismatch; `gmail_connected_at` appears in settings page post-callback.
- `POST /api/settings/ai/rebuild-index` with a new `embed_dim` recreates `document_vectors` at the new dimension; all documents reindexed.
- `app/api/settings_ai.py` removed; `pytest -q` green; `/api/settings/ai/test` still returns 200 (served by `settings_ai_config.py`).
- Theme persistence: switching to "light" in Appearance persists across page reloads.

---

## Related docs

- `docs/specs/00_vision.md` §UI — sidebar rail layout, ⚙ settings entry
- `docs/specs/00a_ingest.md` — Gmail sync pipeline (settings feeds into the ingest configuration)
- `docs/specs/07_case_chat.md` — AI provider used by chat; embedding model feeds semantic retrieval
- `docs/specs/09_timeline.md` — `default_view` in Appearance determines which view opens first
