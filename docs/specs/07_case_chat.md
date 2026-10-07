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
| API — `POST /api/v1/chat/conversations` + `POST /api/v1/chat/conversations/{id}/messages` (SSE) | ✅ |
| Service — `stream_answer()` async generator with SSE protocol | ✅ |
| Context builder — `build_case_chat_prompt()` + `build_document_chat_prompt()` | ✅ |
| Semantic retrieval — `retrieve_top_docs()` via pgvector chunk search + recency fallback | ✅ |
| User-reaction integration — `format_reactions_for_case/document()` | ✅ |
| System prompts — `DOC_CHAT_SYSTEM` + `CASE_CHAT_SYSTEM` with citation rules | ✅ |
| Citation extraction — `[DOC:<id>]` regex → `context_document_ids` JSON | ✅ |
| Shared panel — `frontend/src/features/chat/ChatDrawer.tsx` (streaming, citation pills, empty state, suggested prompts) | ✅ |
| Case chat drawer — `ChatDrawer` mounted by `frontend/src/features/cases/dashboard/CasePage.tsx` + top-bar `[✦ Ask AI]` button | ✅ |
| Document chat drawer — `ChatDrawer` mounted by `frontend/src/features/documents/DocumentPage.tsx` + `[✦ Ask about this document]` button | ✅ |
| Client hooks — `frontend/src/api/chat.ts` (`useOpenConversation`, `useConversation(s)`, `streamMessage` SSE reader, `citationsFromIds`) | ✅ |
| Keyboard `/` → open chat drawer; `Esc` → close; passage pick prefills the composer (`ChatDrawer` `prefill` prop) | ✅ |
| `Case.ai_brief` in case context | ✅ |
| `UserReaction` rows in context (both scopes) | ✅ |
| Semantic retrieval — top K=6 documents via `document_chunks.embedding` pgvector | ✅ |
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
| Proceeding scoping toggle | "Optional toggle: Limit to current proceeding" | `ChatDrawer` checkbox sends `proceeding_id` in `MessageSend` | ✅ Accepted |

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

### `POST /api/v1/chat/conversations`

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

### `POST /api/v1/chat/conversations/{conversation_id}/messages`

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

Document retrieval (`retrieve_top_docs`): embeds the query, ranks `document_chunks.embedding` by pgvector `<->` (L2) distance, filters matches to `case_id` (and `proceeding_id` if scoped), then rolls the top-ranked chunks up to their owning documents (top 6). Each hit surfaces the actual matched passage(s), not a whole-document average. If embeddings fail (model unavailable) or the query errors, falls back to the 6 most recent documents by `issued_date`.

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

The AI embeds `[DOC:<id>]` markers in its response. The service extracts them with `_DOC_REF_RE = re.compile(r'\[DOC:(\d+)\]')` and streams the resolved document metadata as the `citations` SSE event. `ChatDrawer` renders `[DOC:n]` tokens as inline `/document/{id}` links and appends the `citations` frame as clickable document badges below the assistant message.

Citations include `#p=<passage_id>` fragments when referring to specific passages; `DocumentPage.tsx` reads the hash on load and scrolls the reader to that passage.

---

## 7. Client — ChatDrawer

`frontend/src/features/chat/ChatDrawer.tsx` is shared by both scopes; the host page passes `scope` (`{scope_type, scope_id}`), `title`, `suggestions`, `onClose`, and optionally `proceeding` (case scope) or `prefill` (document scope). Hooks live in `frontend/src/api/chat.ts`:

- `useOpenConversation(scope)` → `POST /api/v1/chat/conversations` on mount (latest thread, or `force_new` for `[+]`).
- `useConversations(scope)` → `GET /api/v1/chat/conversations?scope_type=&scope_id=` for the History dropdown; `useRenameConversation` / `useDeleteConversation` → `PUT …/{id}/title` / `DELETE …/{id}`.
- `useConversation(id)` → `GET /api/v1/chat/conversations/{id}` for the message list; `useAppendExchange` writes a finished exchange into the TanStack Query cache so a refetch cannot duplicate it.
- `streamMessage(id, body, onFrame)` → `POST /api/v1/chat/conversations/{id}/messages`, reads the `text/event-stream` body frame by frame (`token` / `citations` / `done`).

**Panel sections (top to bottom):**

| Section | Content |
|---|---|
| Header | Scope title + History dropdown + `[+]` (new) + `[×]` (close) |
| Message list | User/Assistant bubbles + citation pills; `<think>` blocks fold into a "Reasoning" disclosure |
| Empty state | Suggested prompts as clickable chips |
| Error banner | Red inline `role="alert"` above the input |
| Input | "Limit search to {proceeding}" checkbox (case scope) + `<textarea>` with Enter-to-send |

While a stream is open the last assistant bubble shows a pulsing cursor.

---

## 9. Case dashboard integration

`CasePage.tsx` mounts `ChatDrawer` with `scope_type='case'` and the active proceeding (for the scope toggle); the top-bar `[✦ Ask AI]` button and the `/` key open it.

---

## 10. Document HUD integration

`DocumentPage.tsx` mounts `ChatDrawer` with `scope_type='document'`; the `[✦ Ask about this document]` button and the `/` key open it, and "Ask about this passage" opens it with the passage text prefilled.

---

## 11. Keyboard-first interaction

| Key | Scope | Action | Source |
|---|---|---|---|
| `/` | Case dashboard | Open the case chat drawer | `CasePage.tsx` |
| `/` | Document HUD | Open the document chat drawer | `DocumentPage.tsx` |
| `Esc` | Any | Close the chat drawer | `CasePage.tsx`, `DocumentPage.tsx` |
| `Enter` | Chat input focused | Submit message (Shift+Enter: newline) | `ChatDrawer.tsx` |

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
