import { type FormEvent, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'

import {
  type ChatScope,
  type Citation,
  citationsFromIds,
  type Conversation,
  streamMessage,
  useAppendExchange,
  useConversation,
  useConversations,
  useDeleteConversation,
  useOpenConversation,
  useRenameConversation,
} from '../../api/chat'
import { ApiError } from '../../api/client'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'

type Message = {
  id: number | string
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
}

type Props = {
  scope: ChatScope
  title: string
  suggestions: string[]
  onClose: () => void
  /** Case scope only: offer to limit retrieval to this proceeding. */
  proceeding?: { id: number; name: string } | null
  /** A passage the user picked in the reader; prefills the composer. */
  prefill?: { text: string; at: number } | null
}

/** The AI chat panel: history, streaming answers, citations that link into the reader. */
export function ChatDrawer({ scope, title, suggestions, onClose, proceeding, prefill }: Props) {
  const toast = useToast()
  const open = useOpenConversation(scope)
  const [conversationId, setConversationId] = useState<number | null>(null)
  const conversation = useConversation(conversationId)
  const history = useConversations(scope)
  const rename = useRenameConversation(scope)
  const remove = useDeleteConversation(scope)
  const appendExchange = useAppendExchange()

  const [draft, setDraft] = useState('')
  // The question in flight, and resolved citations for answers written to the cache.
  const [inFlight, setInFlight] = useState<string | null>(null)
  const [richCitations, setRichCitations] = useState<Record<number, Citation[]>>({})
  const [streaming, setStreaming] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [limitToProceeding, setLimitToProceeding] = useState(false)
  const composer = useRef<HTMLTextAreaElement>(null)
  const bottom = useRef<HTMLDivElement>(null)
  const abort = useRef<AbortController | null>(null)

  // The latest conversation for this scope, created on first open.
  const openMutate = open.mutate
  useEffect(() => {
    openMutate({ force_new: false }, { onSuccess: (c) => setConversationId(c.id) })
  }, [openMutate, scope.scope_type, scope.scope_id])

  // A new passage pick replaces the draft; `at` distinguishes repeated picks.
  const [seenPrefill, setSeenPrefill] = useState<number | null>(null)
  if (prefill && prefill.at !== seenPrefill) {
    setSeenPrefill(prefill.at)
    setDraft(`Regarding the passage: "${prefill.text}"\n\n`)
  }
  useEffect(() => {
    if (prefill) composer.current?.focus()
  }, [prefill])

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [conversation.data?.messages.length, inFlight, streaming])

  useEffect(() => () => abort.current?.abort(), [])

  const messages: Message[] = (conversation.data?.messages ?? []).map((m) => ({
    id: m.id,
    role: m.role,
    content: m.content,
    citations:
      m.role === 'assistant'
        ? (richCitations[m.id] ?? citationsFromIds(m.context_document_ids))
        : [],
  }))
  if (inFlight !== null)
    messages.push({ id: 'in-flight', role: 'user', content: inFlight, citations: [] })

  async function send(text: string) {
    const content = text.trim()
    if (!content || conversationId === null || streaming !== null) return
    setError(null)
    setDraft('')
    setInFlight(content)
    setStreaming('')
    let answer = ''
    let citations: Citation[] = []
    const controller = new AbortController()
    abort.current = controller
    const isFirst = messages.length === 0
    try {
      await streamMessage(
        conversationId,
        { content, proceeding_id: limitToProceeding && proceeding ? proceeding.id : null },
        (frame) => {
          if (frame.type === 'token') {
            answer += frame.t
            setStreaming(answer)
          } else if (frame.type === 'citations') {
            citations = frame.docs
          }
        },
        controller.signal,
      )
      const answerId = appendExchange(conversationId, {
        question: content,
        answer,
        citedDocIds: [...new Set(citations.map((c) => c.doc_id))],
      })
      if (citations.length) setRichCitations((r) => ({ ...r, [answerId]: citations }))
      setInFlight(null)
      if (isFirst) {
        const auto = content.length > 50 ? `${content.slice(0, 47)}…` : content
        rename.mutate({ id: conversationId, title: auto })
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'The answer could not be streamed.')
    } finally {
      setStreaming(null)
      abort.current = null
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    void send(draft)
  }

  function switchTo(c: Conversation | { id: number }) {
    setInFlight(null)
    setStreaming(null)
    setConversationId(c.id)
    setShowHistory(false)
  }

  return (
    <aside
      aria-label={title}
      className="flex h-full w-[400px] shrink-0 flex-col border-l border-line bg-panel"
    >
      <header className="flex items-center gap-2 border-b border-line2 px-3 py-2">
        <Icon name="forum" size={16} className="text-accent" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-[12px] font-bold">{title}</h3>
          {conversation.data && (
            <TitleEditor
              conversationId={conversation.data.id}
              value={conversation.data.title}
              onSave={(id, t) => rename.mutate({ id, title: t })}
            />
          )}
        </div>
        <button
          type="button"
          aria-label="Conversation history"
          aria-expanded={showHistory}
          onClick={() => setShowHistory((v) => !v)}
          className="rounded-md p-1 text-muted hover:bg-accent/7 hover:text-ink"
        >
          <Icon name="history" size={16} />
        </button>
        <button
          type="button"
          aria-label="New conversation"
          onClick={() => open.mutate({ force_new: true }, { onSuccess: (c) => switchTo(c) })}
          className="rounded-md p-1 text-muted hover:bg-accent/7 hover:text-ink"
        >
          <Icon name="add_comment" size={16} />
        </button>
        <button
          type="button"
          aria-label="Close chat"
          onClick={onClose}
          className="rounded-md p-1 text-muted hover:bg-accent/7 hover:text-ink"
        >
          <Icon name="close" size={16} />
        </button>
      </header>

      {showHistory && (
        <ul className="max-h-56 overflow-y-auto border-b border-line2 bg-panel2 text-[11.5px]">
          {(history.data ?? []).map((h) => (
            <li key={h.id} className="group flex items-center gap-2 px-3 py-1.5 hover:bg-accent/5">
              <button
                type="button"
                onClick={() => switchTo(h)}
                className={`min-w-0 flex-1 truncate text-left ${h.id === conversationId ? 'text-accent' : ''}`}
              >
                {h.title ?? 'Untitled conversation'}
                <span className="ml-2 font-mono text-[10px] text-muted">
                  {new Date(h.created_at).toLocaleDateString()}
                </span>
              </button>
              <button
                type="button"
                aria-label={`Delete conversation ${h.title ?? h.id}`}
                onClick={() =>
                  remove.mutate(h.id, {
                    onSuccess: () => {
                      if (h.id === conversationId) {
                        const next = (history.data ?? []).find((x) => x.id !== h.id)
                        if (next) switchTo(next)
                        else open.mutate({ force_new: true }, { onSuccess: (c) => switchTo(c) })
                      }
                    },
                    onError: (e) => toast(e.message, 'error'),
                  })
                }
                className="text-muted opacity-0 group-hover:opacity-100 hover:text-danger"
              >
                <Icon name="delete" size={14} />
              </button>
            </li>
          ))}
          {history.data?.length === 0 && (
            <li className="px-3 py-2 text-muted">No conversations yet.</li>
          )}
        </ul>
      )}

      <div className="flex-1 space-y-3 overflow-y-auto px-3 py-3 text-[12px]">
        {messages.length === 0 && streaming === null && (
          <div className="space-y-2">
            <p className="text-muted">Ask anything about this {scope.scope_type}.</p>
            {suggestions.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => void send(s)}
                className="block w-full rounded-lg border border-line bg-card2 px-3 py-2 text-left hover:border-accent"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m) => (
          <Bubble key={m.id} message={m} streaming={false} />
        ))}
        {streaming !== null && (
          <Bubble
            message={{ id: 'stream', role: 'assistant', content: streaming, citations: [] }}
            streaming
          />
        )}
        {error && (
          <p
            role="alert"
            className="rounded-md border border-danger/40 bg-danger/10 px-2 py-1 text-danger"
          >
            {error}
          </p>
        )}
        <div ref={bottom} />
      </div>

      <form onSubmit={onSubmit} className="border-t border-line2 p-2">
        {proceeding && (
          <label className="mb-1 flex items-center gap-2 text-[11px] text-muted">
            <input
              type="checkbox"
              checked={limitToProceeding}
              onChange={(e) => setLimitToProceeding(e.target.checked)}
            />
            Limit search to {proceeding.name}
          </label>
        )}
        <div className="flex items-end gap-2">
          <textarea
            ref={composer}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send(draft)
              }
            }}
            rows={2}
            placeholder="Ask… (Enter to send, Shift+Enter for a new line)"
            aria-label="Message"
            className="max-h-[120px] min-h-[40px] flex-1 resize-none rounded-lg border border-line bg-card px-2.5 py-1.5 text-[12px] outline-none focus:border-accent"
          />
          <Button
            type="submit"
            disabled={streaming !== null || !draft.trim() || conversationId === null}
          >
            Send
          </Button>
        </div>
      </form>
    </aside>
  )
}

function TitleEditor({
  conversationId,
  value,
  onSave,
}: {
  conversationId: number
  value: string | null
  onSave: (id: number, t: string) => void
}) {
  const [editing, setEditing] = useState(false)
  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        title="Rename conversation"
        className="block max-w-full truncate text-left text-[10.5px] text-muted hover:text-ink"
      >
        {value ?? 'Untitled conversation'}
      </button>
    )
  }
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        const t = String(new FormData(e.currentTarget).get('title')).trim()
        if (t) onSave(conversationId, t)
        setEditing(false)
      }}
    >
      <input
        name="title"
        defaultValue={value ?? ''}
        aria-label="Conversation title"
        autoFocus
        onBlur={(e) => e.currentTarget.form?.requestSubmit()}
        className="w-full rounded border border-line bg-card px-1 text-[10.5px]"
      />
    </form>
  )
}

const DOC_REF = /\[DOC:(\d+)(?:#p=\d+)?\]/g
const THINK = /<think>([\s\S]*?)(?:<\/think>|$)/g

function Bubble({ message, streaming }: { message: Message; streaming: boolean }) {
  const mine = message.role === 'user'
  return (
    <div className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[92%] rounded-xl px-3 py-2 leading-relaxed whitespace-pre-wrap ${mine ? 'bg-accent/15 text-ink' : 'bg-aibg text-ink'}`}
      >
        <Content text={message.content} streaming={streaming} />
        {streaming && <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-accent" />}
        {message.citations.length > 0 && (
          <ul className="mt-2 flex flex-wrap gap-1">
            {message.citations.map((c) => (
              <li key={`${c.doc_id}-${c.passage_id ?? ''}`}>
                <Link
                  to={`/document/${c.doc_id}${c.passage_id ? `#p=${c.passage_id}` : ''}`}
                  className="rounded border border-line bg-card px-1.5 py-0.5 font-mono text-[9.5px] text-tealink hover:underline"
                  title={c.title}
                >
                  {c.case_id ? `${c.case_id} · ` : ''}#{c.doc_id}
                  {c.passage_id ? ' · passage' : ''}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

/** Folds `<think>` blocks and turns `[DOC:n]` references into links. */
function Content({ text, streaming }: { text: string; streaming: boolean }) {
  const parts: React.ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(THINK)) {
    if (m.index > last) parts.push(<Refs key={`t${last}`} text={text.slice(last, m.index)} />)
    const closed = m[0].endsWith('</think>')
    parts.push(
      <details
        key={`k${m.index}`}
        open={!closed && streaming}
        className="my-1 text-[11px] text-muted"
      >
        <summary className="cursor-pointer">Reasoning</summary>
        <div className="whitespace-pre-wrap">{m[1]}</div>
      </details>,
    )
    last = m.index + m[0].length
  }
  if (last < text.length) parts.push(<Refs key={`t${last}`} text={text.slice(last)} />)
  return <>{parts}</>
}

function Refs({ text }: { text: string }) {
  const out: React.ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(DOC_REF)) {
    if (m.index > last) out.push(text.slice(last, m.index))
    out.push(
      <Link
        key={m.index}
        to={`/document/${m[1]}`}
        className="rounded bg-card px-1 font-mono text-[10px] text-tealink hover:underline"
      >
        {m[0]}
      </Link>,
    )
    last = m.index + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return <>{out}</>
}
