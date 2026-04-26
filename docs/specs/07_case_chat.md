# Sanctuary — AI Chat

Companion document to `docs/specs/00_vision.md` §7. Covers both scopes of AI chat: **case chat** (case dashboard slide-in, all proceedings) and **document chat** (document HUD drawer, single document). Both share the same service layer and panel component; they differ only in context assembly and the scope stored on the `Conversation` row.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟡 IMPLEMENTED — four known gaps (ActionItem/Claim context, passage deep-links, conversation history UI, proceeding toggle)

| Layer | Status |
|---|---|
| Schema — `Conversation` + `ConversationMessage` (migration `8ef2d25dee29`) | ✅ |
| Repository — `ChatRepository` (`get_or_create`, `add_message`, `messages`, `get`) | ✅ |
| API — `POST /api/chat/conversations` + `POST /api/chat/conversations/{id}/messages` (SSE) | ✅ |
| Service — `stream_answer()` async generator with SSE protocol | ✅ |
| Context builder — `build_case_chat_prompt()` + `build_document_chat_prompt()` | ✅ partial — ActionItem + Claim rows absent from case context |
| Semantic retrieval — `retrieve_top_docs()` via sqlite-vec + recency fallback | ✅ |
| User-reaction integration — `format_reactions_for_case/document()` | ✅ |
| System prompts — `DOC_CHAT_SYSTEM` + `CASE_CHAT_SYSTEM` with citation rules | ✅ |
| Citation extraction — `[DOC:<id>]` regex → `context_document_ids` JSON | ✅ |
| Shared panel — `partials/chat/_panel.html` (streaming, citation pills, empty state, suggested prompts) | ✅ |
| Case chat drawer — `partials/dashboard/ai_chat_stub.html` + top-bar `[✦ Ask AI]` button | ✅ |
| Document chat drawer — `partials/hud/_chat_drawer.html` + `_ask_ai.html` button | ✅ |
| Alpine.js client — `static/js/chat.js` (`aiChat()` component, SSE parser, citation renderer) | ✅ |
| Keyboard `/` → focus chat input; `Esc` → close; `hud-prefill-chat` event | ✅ |
| `Case.ai_brief` in case context | ✅ |
| `UserReaction` rows in context (both scopes) | ✅ |
| Semantic retrieval — top K=6 documents via `document_vectors` sqlite-vec | ✅ |
| `ActionItem` rows in case context | ❌ not assembled — spec §11 mentions "Recent ActionItem … records" |
| `Claim` rows in case context | ❌ not assembled — spec §11 mentions "… and Claim records" |
| Citation links open Document HUD **at the cited passage** | ⚠ links go to `/document/{id}` root — no `#p=<passage_id>` fragment |
| Conversation history dropdown (return to past threads) | ❌ only current conversation shown; `Conversation.title` never populated |
| "Limit to current proceeding" scope toggle | ❌ case chat always uses all proceedings' documents |
| Tests for chat routes, service, or repository | ❌ no test files |

### Implementation Deviations

| Feature | Vision §7 / Dashboard §11 | Code | Status |
|---|---|---|---|
| AI transport | "WebSocket" (original plan) | HTTP/1.1 SSE chunked transfer | ✅ Accepted — lower complexity, equivalent latency |
| Document chat as part of HUD | "Stub button in v1; drawer in Phase 7" | Fully implemented drawer shipped as part of Phase 7 | ✅ Promoted earlier than phased |
| Context: `ActionItem` + `Claim` | "Recent ActionItem and Claim records" (`02_dashboard.md §11`) | Context builder includes only `ai_brief`, reactions, retrieved docs | ⚠ Known gap |
| Citation links → passage in HUD | "clickable passage references open the document HUD at **that passage**" | Links to `/document/{id}` root; no passage scroll | ⚠ Known gap |
| Conversation history | "history dropdown … to return to past threads" | No history UI; `Conversation.title` not populated | ⚠ Known gap (deferred) |
| Proceeding scoping toggle | "Optional toggle: Limit to current proceeding" | Not implemented; retrieval searches all case documents | ⚠ Known gap (deferred) |

---

## The core shift

**Traditional legal research:** search a document management system for relevant files, read them, mentally synthesise the answer, copy-paste quotes into your response.

**Sanctuary AI Chat:** ask in plain language, at the moment you need the answer, against full case context. The AI already knows your triage reactions, the case brief, and which documents are semantically relevant — it answers with inline citations so you can verify immediately. Document chat is for focused questions about a single text; case chat is for strategic queries across the whole matter.

The same service handles both scopes. The only difference is context assembly: document chat uses the document's `key_passages` and content; case chat uses `Case.ai_brief`, all `UserReaction` rows, and the top-K semantically-retrieved documents for the query. Both prompt the model to cite every factual claim with `[DOC:<id>]`.

---

## Layout overview

### Case chat (dashboard slide-in)

```
ADV-024-A  Musterklage GmbH vs. XY          [✦ Ask AI]  ← top-bar button
                                             ┌────────────── 400px ──────────────┐
                                             │ Case AI Chat          [+] [×]     │
                                             ├───────────────────────────────────┤
                                             │  ✦  What would you like to know?  │
                                             │                                   │
                                             │  [What are the open deadlines?]   │
                                             │  [Summarize current case state]   │
                                             │  [What claims has opposing made?] │
                                             │  [Current cost exposure?]         │
                                             │                                   │
                                             ├───────────────────────────────────┤
                                             │  You                       14:32  │
                                             │  What did I flag as 🚩 Lies?      │
                                             │                                   │
                                             │  Assistant               14:32 ▌  │
                                             │  You flagged doc #31 (Klage-      │
                                             │  erwiderung) 🚩 with note         │
                                             │  "contradicts own timeline."      │
                                             │                                   │
                                             │  ◦ ADV-024-A · #31                │
                                             ├───────────────────────────────────┤
                                             │  Ask the AI…              [send]  │
                                             │  Answers cite source documents    │
                                             └───────────────────────────────────┘
```

### Document chat (HUD drawer)

```
┌──── Document HUD ────────────────┐  ┌──── Document Chat ──────── 400px ──┐
│  Klageerwiderung Beklagter       │  │ Document Chat              [+] [×] │
│  [key passages …]                │  ├────────────────────────────────────┤
│                                  │  │  What are the key legal claims?    │
│  [✦ Ask about this document]  ──►│  │  Summarize the key passages        │
│                                  │  │  What deadlines does this create?  │
│  [🚩 Lies] [✅ True] [🔍] [⚖️]  │  │  What does this assert about opp.? │
│  [+ note]                        │  ├────────────────────────────────────┤
└──────────────────────────────────┘  │  …                                 │
                                      │  [send]                            │
                                      └────────────────────────────────────┘
```

---

## 1. Data model

```
Conversation
  id              INTEGER PK
  scope_type      TEXT  "case" | "document"
  scope_id        TEXT  case: Case.id (e.g. "ADV-024-A")
                        document: str(Document.id)
  title           TEXT  nullable — not yet populated
  ingest_date     DATETIME

  INDEX ix_conversations_scope(scope_type, scope_id)

ConversationMessage
  id                   INTEGER PK
  conversation_id      INTEGER FK → Conversation
  role                 TEXT  "user" | "assistant"
  content              TEXT
  context_document_ids JSON  nullable — list[int] of cited doc IDs (assistant only)
  ingest_date          DATETIME

  INDEX ix_conversation_messages_conversation(conversation_id)
```

`Conversation.title` is nullable and not yet populated — it exists for the future history-dropdown feature. `ConversationMessage.context_document_ids` is only set on assistant messages (extracted from `[DOC:<id>]` citations in the AI response).

---

## 2. API

### `POST /api/chat/conversations`

Get or create a conversation for the given scope.

**Request body:**
```json
{ "scope_type": "case", "scope_id": "ADV-024-A", "force_new": false }
```

**Response:**
```json
{
  "id": 12,
  "scope_type": "case",
  "scope_id": "ADV-024-A",
  "title": null,
  "messages": [
    { "role": "user", "content": "…", "context_document_ids": null },
    { "role": "assistant", "content": "…", "context_document_ids": [31, 47] }
  ]
}
```

`force_new=true` always creates a fresh conversation, ignoring any existing one for the scope.

### `POST /api/chat/conversations/{conversation_id}/messages`

Stream the AI response to a new user message.

**Request body:** `{ "content": "What did I flag as 🚩 Lies?" }`

**Response:** `Content-Type: text/event-stream` (SSE)

```
data: {"type": "token", "t": "You flagged"}
data: {"type": "token", "t": " doc #31"}
…
data: {"type": "citations", "docs": [{"doc_id": 31, "case_id": "ADV-024-A", "title": "Klageerwiderung Beklagter"}]}
data: {"type": "done"}
```

Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

---

## 3. Streaming service

`app/services/chat/chat_service.py` — `stream_answer(conversation, user_message, db)` is an async generator that:

1. Persists the user message via `ChatRepository.add_message(…, role='user')`.
2. Loads conversation history (excluding the just-persisted message) up to `MAX_HISTORY_TURNS = 20`.
3. Detects `scope_type` and delegates to the matching context builder.
4. Calls the configured AI provider with `num_ctx=16384`, `temperature=0.2`, `num_predict=800`.
5. Yields `data: {"type": "token", "t": "…"}` for each text chunk via `ai_provider.parse_stream_line()`.
6. Extracts `[DOC:(\d+)]` citations from the full response text (`_DOC_REF_RE`).
7. Resolves cited `Document` rows and builds citation metadata.
8. Persists the full assistant message with `context_document_ids`.
9. Yields `data: {"type": "citations", "docs": […]}` then `data: {"type": "done"}`.

AI provider is selected via `get_effective_config(db)` — supports Ollama, LMStudio, and OpenAI transparently.

---

## 4. Context assembly

### Case chat (`build_case_chat_prompt`)

```
=== CONTEXT ===
Case: ADV-024-A — Musterklage GmbH vs. XY
Status: active  |  Cost exposure: 1,690 EUR

--- Case AI Brief ---
Posture: …
Pressure points: …
Next move: …

--- Retrieved Documents (top 6) ---
[#47 Klageerwiderung Beklagter — critical]
Key passages: …

[#82 Stellungnahme Jugendamt — significant]
Key passages: …

--- User Reactions ---
#31 Klageerwiderung  🚩  "contradicts own timeline"
#55 Klage            ✅

=== CONVERSATION HISTORY ===
User: …
Assistant: …

=== CURRENT QUESTION ===
What did I flag as 🚩 Lies?
```

Document retrieval (`retrieve_top_docs`): embeds the query, searches `document_vectors` (sqlite-vec `WHERE embedding MATCH :blob ORDER BY distance LIMIT 6`), filters to `case_id`, returns `RetrievalHit` objects. If embeddings fail (model unavailable), falls back to the 6 most recent documents by `issued_date`.

**⚠ Known gap.** `ActionItem` rows (open deadlines, Fristen) and `Claim` rows (contested factual assertions) are referenced in `02_dashboard.md §11` but are **not assembled** in `build_case_chat_prompt`. As a result, "What are the open deadlines?" returns general brief information rather than citing specific `ActionItem` records with exact Frist dates, and "What did the AI extract as contested claims?" has no direct data source.

Remediation: add two context blocks to `context_builder.py`:
- `ActionItem`: open items for the case (`is_open=True`) with `due_date`, `description`, `source_document_id` — sorted by `due_date asc`; cap at 10.
- `Claim`: top contested and asserted claims (`status in [CONTESTED, ASSERTED]`) with `text`, `status`, count of supporting/contesting evidence — sorted by `updated_at desc`; cap at 15.

### Document chat (`build_document_chat_prompt`)

```
=== CONTEXT ===
Document: #47 Klageerwiderung Beklagter
Case: ADV-024-A  |  Issued: 14. Jan 2026  |  Tier: critical

--- Key Passages (up to 10) ---
…

--- Document Content (first 6000 chars) ---
…

--- User Reactions on this document ---
🚩  "contradicts own timeline"
```

---

## 5. System prompts

`app/services/chat/prompts.py` — two prompts, same citation discipline:

**`DOC_CHAT_SYSTEM`** — document-scoped:
> Answer only from the document context provided. Cite with `[DOC:<doc_id>]` immediately after each sentence that draws on the document. If the context lacks the answer, say so explicitly. Be concise and precise. Match the language of the user's question (German or English). Cite key passages verbatim when directly relevant.

**`CASE_CHAT_SYSTEM`** — case-scoped:
> Ground all factual statements in the provided documents; cite with `[DOC:<doc_id>]`. The Case AI Brief is a summary, not gospel — prefer primary document evidence when they conflict. User reactions (🚩/✅/🔍/⚖️) are high-weight signals; incorporate them in your answer. If the answer is not in the context, say so — do not speculate. Be direct. Cap answer at ~400 words unless the user asks for more. Match the language of the user's question.

---

## 6. Citation rendering

The AI embeds `[DOC:<id>]` markers in its response. The service extracts them with `_DOC_REF_RE = re.compile(r'\[DOC:(\d+)\]')` and streams the resolved document metadata as the `citations` SSE event. The Alpine client (`chat.js:renderContent()`) HTML-escapes the raw text and renders `[DOC:n]` tokens as inline pills; the `citations` event appends clickable document badges below the assistant message.

**⚠ Known gap.** Citations currently link to `/document/{doc_id}` (document root). Vision §7 and `02_dashboard.md §11` say "clickable passage references open the document HUD at that passage." The fix requires:
- AI response format: `[DOC:<id>#p=<passage_index>]` — extend prompt to include passage index hint from `key_passages`.
- `_DOC_REF_RE` extended to capture the optional `#p=` suffix.
- `chat.js:renderContent()` generates `href="/document/{id}#p={idx}"` and the HUD's `_passages_spine.html` already honours URL hash for scroll-to.

---

## 7. Frontend — `partials/chat/_panel.html`

Shared by both scopes. Initialised via `x-data="aiChat({scopeType, scopeId, suggestedPrompts})"`.

**Panel sections (top to bottom):**

| Section | Height | Content |
|---|---|---|
| Header | 48px | Scope title + `[+]` (new conversation) + `[×]` (close) |
| Message list | flex-1, scrollable | User bubbles (right, primary-container) + assistant replies (left, surface-container) + citation pills |
| Empty state | centred | ✦ icon + suggested prompts as clickable chips |
| Error banner | conditional | Red inline banner above input |
| Input | 56px | `<textarea>` — Enter sends, Shift+Enter newlines; "Answers cite source documents" helper |

**Streaming cursor:** `<span class="animate-pulse">▌</span>` appended to `streamBuffer` while the SSE stream is open.

**Citation pills** (below each assistant message):
```html
<a href="/document/{doc_id}" class="…">
  {case_id} · #{doc_id}   <!-- e.g. "ADV-024-A · #31" -->
</a>
```

---

## 8. Alpine.js client — `static/js/chat.js`

`Alpine.data('aiChat', ({scopeType, scopeId, suggestedPrompts}) => ({ … }))`

**State:**

| Property | Initial | Role |
|---|---|---|
| `conversationId` | `null` | Persisted conversation ID from API |
| `messages` | `[]` | Rendered message list (role + content + citations) |
| `draft` | `''` | Textarea bind |
| `streaming` | `false` | Disables send during stream; shows cursor |
| `streamBuffer` | `''` | Accumulates SSE tokens for the in-progress message |
| `error` | `null` | Error banner text |
| `loading` | `true` | Suppresses UI until `init()` completes |

**SSE parser:** Reads chunked response via `ReadableStream` + `TextDecoder`. Splits on `\n\n`, extracts `data:` lines, JSON-parses each payload. Handles incomplete chunks via buffer carry-over.

**Prefill event:** The HUD emits `hud-prefill-chat` with `detail.prompt` when the user clicks a suggested passage-level question — `chat.js` listens for this window event and sets `draft`.

---

## 9. Case dashboard integration

`partials/dashboard/ai_chat_stub.html` wraps `_panel.html` with `scope_type='case'` and the case's `case.id`. The drawer is a `position: fixed` right-side overlay (`z-50`, `w-[400px]`), toggled by `chatOpen` in the `caseDashboard` Alpine scope.

**Top-bar `[✦ Ask AI]` button** (`top_bar.html:107-114`):
```html
<button @click="chatOpen = true; docChatOpen = false" …>✦ Ask AI</button>
```

`window.dispatchEvent(new CustomEvent('close-chat'))` closes the drawer from anywhere (Esc handler in `dashboard.js`).

---

## 10. Document HUD integration

`partials/hud/_chat_drawer.html` wraps `_panel.html` with `scope_type='document'` and `str(doc.id)`. The drawer uses `docChatOpen` in the parent Alpine scope.

`partials/hud/_ask_ai.html` renders the `[✦ Ask about this document]` button that sets `docChatOpen = !docChatOpen` and closes the case chat if open.

**Passage-prefill:** When a passage has an inline "ask AI" affordance, it emits `hud-prefill-chat` with the passage excerpt as the prompt text — the document chat drawer picks it up and pre-fills the textarea.

---

## 11. Keyboard-first interaction

| Key | Scope | Action | Source |
|---|---|---|---|
| `/` | Dashboard (no doc HUD) | Open case chat drawer + focus textarea | `dashboard.js` |
| `/` | Dashboard (doc HUD open) | Open document chat drawer + focus textarea (via `hud-focus-chat` event) | `dashboard.js` |
| `Esc` | Any | Close chat panels (`chatOpen = docChatOpen = false`), then HUD, then other overlays | `dashboard.js` |
| `Enter` | Chat input focused | Submit message | `chat.js` + template |
| `Shift+Enter` | Chat input focused | Insert newline | template |

---

## 12. Conversation history

**⚠ Known gap.** `02_dashboard.md §11` states: "user can return to past threads via a history dropdown at the top of the chat panel." This is not implemented. The `[+]` button creates a new conversation (`force_new=true`) but there is no UI to list or return to prior threads. `Conversation.title` is never populated (always `null`).

Remediation:
- `ChatRepository.list_by_scope(scope_type, scope_id) → list[Conversation]` — ordered by `ingest_date desc`.
- `POST /api/chat/conversations/{id}/title` — auto-generates a title from the first user message (single AI call or just truncate to 50 chars).
- Add a `[▾ History]` dropdown in the panel header that lists past conversations; clicking one calls `loadConversation(id)`.

---

## 13. Empty states

| Situation | What renders |
|---|---|
| New conversation (no messages) | Centred ✦ icon + 4 suggested prompt chips |
| Streaming in progress | Last assistant message shows `▌` cursor; send button disabled |
| AI provider unavailable (timeout / connection error) | Red inline banner: "AI is not available — check that Ollama is running." |
| No document embeddings (retrieval fallback) | Retrieval silently falls back to 6 most-recent documents; no visible degradation |
| Empty `key_passages` on document chat | Context contains first 6000 chars of `Document.content` only; panel renders normally |
| `Case.ai_brief` is null (brief not yet generated) | Context skips the brief block; retrieval + reactions still work |

---

## 14. Data sources map

| Context block | Source table / field | Scope | Phase |
|---|---|---|---|
| Case brief (posture, pressure_points, next_move) | `Case.ai_brief` JSON | case | Phase 5 |
| Retrieved documents | `document_vectors` sqlite-vec + `Document` | case | Phase 4 (embeddings) + Phase 7 (retrieval) |
| User reactions | `UserReaction` (all docs in case, or single doc) | both | Phase 2 |
| Document key passages | `Document.key_passages` JSON | document | Phase 4 |
| Document content | `Document.content` (first 6000 chars) | document | Phase 3 |
| Conversation history | `ConversationMessage` (last 20 turns) | both | Phase 7 |
| ActionItem rows | `ActionItem` (open, ordered by due_date) | case | Phase 3/4 — **⚠ not yet in context** |
| Claim rows | `Claim` (CONTESTED + ASSERTED) | case | Phase 6 — **⚠ not yet in context** |

---

## 15. Files that will change

### Modified

| File | Change |
|---|---|
| `app/services/chat/context_builder.py` | Add `ActionItem` + `Claim` context blocks to `build_case_chat_prompt()` |
| `app/services/chat/chat_service.py` | Extend `_DOC_REF_RE` and citation metadata to capture optional `#p=<idx>` passage fragment |
| `app/services/chat/prompts.py` | Update `CASE_CHAT_SYSTEM` to request `[DOC:<id>#p=<idx>]` format; update `DOC_CHAT_SYSTEM` similarly |
| `static/js/chat.js` | Update `renderContent()` and citation href generation to include `#p=` fragment |
| `app/repositories/chat.py` | Add `list_by_scope(scope_type, scope_id)` for history dropdown |
| `app/templates/partials/chat/_panel.html` | Add `[▾ History]` dropdown in header; populate from `list_by_scope` |
| `app/api/chat.py` | Add `GET /api/chat/conversations?scope_type=…&scope_id=…` for history list; add `POST /api/chat/conversations/{id}/title` |
| `docs/specs/00_vision.md` §7 | Link to `07_case_chat.md` |
| `docs/specs/02_dashboard.md` §11 | Link to `07_case_chat.md` |

### New

| File | Purpose |
|---|---|
| `tests/integration/test_chat_routes.py` | Happy path for both scopes; SSE stream parses cleanly; citations extracted; cross-scope 404; `force_new` creates new conversation |
| `tests/unit/test_chat_context_builder.py` | Assert ActionItem + Claim blocks appear in case context once remediated; assert document context includes key_passages |

### Deleted

None.

---

## 16. Phase progression map

| Phase | What lands |
|---|---|
| **Phase 1** (schema) | `Conversation`, `ConversationMessage` tables; `Case.ai_brief`, `UserReaction` models |
| **Phase 4** (doc intelligence) | `Document.key_passages` populated; `document_vectors` embeddings written |
| **Phase 5** (case AI) | `Case.ai_brief` generated and kept current |
| **Phase 6** (Truth Map) | `Claim` rows available — not yet wired into chat context |
| **Phase 7** (AI Chat) | Full streaming service, both scopes, citation extraction, panel component, Alpine client |
| **Phase 7+ (remediation)** | ActionItem + Claim in context; passage deep-links; conversation history dropdown; proceeding scope toggle |

---

## 17. Non-goals

- **No WebSocket** — SSE over HTTP/1.1 is sufficient and avoids stateful connection management.
- **No AI-to-AI multi-turn planning** — single question → single streamed answer; no autonomous tool use.
- **No inline reaction tagging** — user cannot tag messages with 🚩/✅ inside the chat panel; reactions happen at triage and remain doc-scoped.
- **No cross-case chat** — scope is always a single case or a single document; aggregation across cases belongs to ⌘K search.
- **No real-time collaboration** — single-user; no shared chat sessions.
- **No message editing or deletion** — `ConversationMessage` rows are append-only; history is authoritative.
- **No PDF / attachment export** of chat transcripts.
- **No explicit per-claim reactions from inside chat** — the AI can recall and discuss user reactions; it cannot add new ones.

---

## 18. Verification

### Manual

1. `make seed && make run` → open a case dashboard → click `[✦ Ask AI]` → panel slides in; 4 suggested prompt chips visible.
2. Click a suggested prompt → question appears in message list; AI streams a response; `[ADV-024-A · #<id>]` citation pills appear below.
3. Click a citation pill → navigates to `/document/<id>` (HUD opens). *(Post-remediation: navigates to `/document/<id>#p=<idx>` and HUD scrolls to passage.)*
4. Open a document HUD → click `[✦ Ask about this document]` → document chat drawer opens; suggested prompts are document-scoped.
5. Type a question in document chat → response cites only documents linked to this document's context.
6. Press `Esc` → chat closes; HUD remains open.
7. Press `/` on dashboard → case chat opens and textarea receives focus.
8. Click `[+]` (new conversation) → panel clears; suggested prompts reappear.
9. Close and reopen case chat → prior conversation is restored (no `force_new`).
10. *(Post-remediation)* Ask "What are the open deadlines?" → response lists `ActionItem` rows with specific `due_date` values.

### Automated

`tests/integration/test_chat_routes.py`:
- `test_create_or_get_conversation` — POST `/api/chat/conversations` → 200, `id` field present.
- `test_force_new_conversation` — second POST with `force_new=true` → different `id`.
- `test_stream_message_case_scope` — POST message → SSE stream contains `token`, `citations`, `done` events in order.
- `test_stream_message_doc_scope` — same for `scope_type="document"`.
- `test_cross_conversation_message_404` — POST message to non-existent `conversation_id` → 404.

`tests/unit/test_chat_context_builder.py` (post-remediation):
- `test_case_context_includes_action_items` — seed open ActionItem → assert it appears in prompt string.
- `test_case_context_includes_claims` — seed contested Claim → assert it appears in prompt string.
- `test_document_context_includes_key_passages` — seed doc with `key_passages` → assert passages in prompt.

---

## 19. Success criteria

- Case chat answers "What are the open deadlines?" with specific `ActionItem` dates (post-remediation).
- Case chat answers "What is contested?" with specific `Claim` text (post-remediation).
- Citation pills include passage fragment `#p=<idx>` and HUD scrolls to the cited passage (post-remediation).
- `[+]` new conversation clears history; re-opening the chat restores the prior conversation.
- Both scopes (case + document) stream responses without error on a seeded dataset.
- `pytest tests/integration/test_chat_routes.py -v` all green.
- No test references `mock_ai_provider` — integration tests use a deterministic stub that returns a fixed response with a `[DOC:<id>]` citation, verifying the extraction and persistence pipeline end-to-end.

---

## Related docs

- `docs/specs/00_vision.md` — §7 AI Chat; §5 Triage (UserReaction origin); Phase Matrix
- `docs/specs/02_dashboard.md` — §11 Case AI Chat; §10 Document HUD slide-in
- `docs/specs/04_document_hud.md` — §8h "Ask about this document" HUD integration; `_ask_ai.html`; `hud-prefill-chat` event
- `docs/specs/06_truth_map.md` — `Claim` rows that will be wired into case chat context (remediation)
- `docs/specs/08_financials.md` — `LegalCost` / cost exposure surface that case chat can reference via `ai_brief`
