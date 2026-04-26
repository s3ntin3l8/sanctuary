# Sanctuary — AI Chat

Companion document to `docs/specs/00_vision.md` §7. Covers both scopes of AI chat: **case chat** (case dashboard slide-in, all proceedings) and **document chat** (document HUD drawer, single document). Both share the same service layer and panel component; they differ only in context assembly and the scope stored on the `Conversation` row.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

| Layer | Status |
|---|---|
| Schema — `Conversation` + `ConversationMessage` (migration `8ef2d25dee29`) | ✅ |
| Repository — `ChatRepository` (`get_or_create`, `add_message`, `messages`, `get`) | ✅ |
| API — `POST /api/chat/conversations` + `POST /api/chat/conversations/{id}/messages` (SSE) | ✅ |
| Service — `stream_answer()` async generator with SSE protocol | ✅ |
| Context builder — `build_case_chat_prompt()` + `build_document_chat_prompt()` | ✅ |
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
| `ActionItem` rows in case context | ✅ |
| `Claim` rows in case context | ✅ |
| Citation links open Document HUD **at the cited passage** | ✅ |
| Conversation history dropdown (return to past threads) | ✅ |
| "Limit to current proceeding" scope toggle | ✅ |
| Tests for chat routes, service, or repository | ✅ |

### Implementation Deviations

| Feature | Vision §7 / Dashboard §11 | Code | Status |
|---|---|---|---|
| AI transport | "WebSocket" (original plan) | HTTP/1.1 SSE chunked transfer | ✅ Accepted — lower complexity, equivalent latency |
| Document chat as part of HUD | "Stub button in v1; drawer in Phase 7" | Fully implemented drawer shipped as part of Phase 7 | ✅ Promoted earlier than phased |
| Context: `ActionItem` + `Claim` | "Recent ActionItem and Claim records" (`02_dashboard.md §11`) | Context builder includes `ai_brief`, reactions, retrieved docs, open action items, and contested claims | ✅ Accepted |
| Citation links → passage in HUD | "clickable passage references open the document HUD at **that passage**" | Links include `#p=<passage_id>` fragment | ✅ Accepted |
| Conversation history | "history dropdown … to return to past threads" | Implemented via History dropdown in panel header | ✅ Accepted |
| Proceeding scoping toggle | "Optional toggle: Limit to current proceeding" | Implemented via `limitToProceeding` Alpine state | ✅ Accepted |

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
  title           TEXT  nullable
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

`Conversation.title` is auto-generated from the first message. `ConversationMessage.context_document_ids` is set on assistant messages (extracted from `[DOC:<id>]` citations in the AI response).

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
  "title": "What did I flag as lies?",
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

Includes:
- **Case Summary:** ID, Title, Status, Cost Exposure.
- **AI Brief:** Posture, Pressure Points, Next Move.
- **ActionItems:** Top 10 open items sorted by due date.
- **Claims:** Top 15 contested/asserted claims sorted by update date.
- **Retrieved Documents:** Top 6 semantically relevant documents with their key passages.
- **User Reactions:** Chronological reactions for documents in the case.

Document retrieval (`retrieve_top_docs`): embeds the query, searches `document_vectors` (sqlite-vec `WHERE embedding MATCH :blob ORDER BY distance LIMIT 6`), filters to `case_id`, returns `RetrievalHit` objects. If embeddings fail (model unavailable), falls back to the 6 most recent documents by `issued_date`.

### Document chat (`build_document_chat_prompt`)

Includes:
- **Document Metadata:** ID, Title, Case ID, Issued Date, Tier.
- **Key Passages:** Up to 10 identified passages.
- **Document Content:** First 6000 characters of the document body.
- **User Reactions:** Reactions specifically for this document.

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

Citations include `#p=<idx>` fragments when referring to specific passages, and the HUD's `_passages_spine.html` honors these for scroll-to behavior.

---

## 7. Frontend — `partials/chat/_panel.html`

Shared by both scopes. Initialised via `x-data="aiChat({scopeType, scopeId, suggestedPrompts})"`.

**Panel sections (top to bottom):**

| Section | Height | Content |
|---|---|---|
| Header | 48px | Scope title + History dropdown + `[+]` (new) + `[×]` (close) |
| Message list | flex-1, scrollable | User/Assistant bubbles + citation pills |
| Empty state | centred | ✦ icon + suggested prompts as clickable chips |
| Error banner | conditional | Red inline banner above input |
| Input | 56px | `<textarea>` with Enter-to-send |

**Streaming cursor:** `<span class="animate-pulse">▌</span>` appended to `streamBuffer` while the SSE stream is open.

---

## 8. Alpine.js client — `static/js/chat.js`

`Alpine.data('aiChat', ({scopeType, scopeId, suggestedPrompts}) => ({ … }))`

**Features:**
- **SSE Parser:** Handles chunked responses and JSON payloads.
- **Prefill Event:** Listens for `hud-prefill-chat` to populate the draft.
- **History Management:** Loads and lists past conversations.
- **Proceeding Scoping:** Optional filtering to the current proceeding.

---

## 9. Case dashboard integration

`partials/dashboard/ai_chat_stub.html` wraps `_panel.html` with `scope_type='case'`. The drawer is toggled by `chatOpen` in the `caseDashboard` Alpine scope.

**Top-bar `[✦ Ask AI]` button:**
```html
<button @click="chatOpen = true; docChatOpen = false" …>✦ Ask AI</button>
```

---

## 10. Document HUD integration

`partials/hud/_chat_drawer.html` wraps `_panel.html` with `scope_type='document'`. The drawer uses `docChatOpen` in the parent Alpine scope.

`partials/hud/_ask_ai.html` renders the `[✦ Ask about this document]` button that sets `docChatOpen = !docChatOpen`.

---

## 11. Keyboard-first interaction

| Key | Scope | Action | Source |
|---|---|---|---|
| `/` | Dashboard (no doc HUD) | Open case chat drawer + focus textarea | `dashboard.js` |
| `/` | Dashboard (doc HUD open) | Open document chat drawer + focus textarea | `dashboard.js` |
| `Esc` | Any | Close chat panels | `dashboard.js` |
| `Enter` | Chat input focused | Submit message | `chat.js` |

---

## 12. Empty states

| Situation | What renders |
|---|---|
| New conversation (no messages) | Centred ✦ icon + suggested prompt chips |
| Streaming in progress | Last assistant message shows `▌` cursor |
| AI provider unavailable | Red inline banner with connection error |
| No document embeddings | Retrieval falls back to 6 most-recent documents |

---

## 13. Success criteria

- AI Chat answers strategic questions using full case context (Brief, Actions, Claims, Reactions, Documents).
- Document chat focused on single document content and key passages.
- Streaming SSE provides real-time feedback with inline citations.
- History dropdown allows switching between past conversations.
- Citations link directly to the document HUD (with passage scrolling if available).
- Keyboard shortcuts and responsive drawers provide a seamless UX.

---

## Related docs

- `docs/specs/00_vision.md` — North star and phase roadmap
- `docs/specs/02_dashboard.md` — Case dashboard integration
- `docs/specs/04_document_hud.md` — Document HUD integration
- `docs/specs/06_truth_map.md` — Claims context source
- `docs/specs/08_financials.md` — Financial exposure context source
