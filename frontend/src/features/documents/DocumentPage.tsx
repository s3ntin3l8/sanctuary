import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router'

import { originalUrl, type Reader, useCreatePin, useDocumentReader } from '../../api/documents'
import { formatShortDate } from '../../format'
import { hasModifier, isTypingTarget, overlayOpen } from '../../shell/keys'
import { usePageShortcuts, useShortcuts } from '../../shell/shortcuts'
import { useReact } from '../../api/triage'
import { Badge, type Tone } from '../../ui/Badge'
import { Drawer } from '../../ui/Drawer'
import { useMediaQuery } from '../../ui/useMediaQuery'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'
import { ChatDrawer } from '../chat/ChatDrawer'
import { ORIGINATOR_COLOR, ReviewSections } from './DocumentReview'
import { PinGutter } from './PinGutter'
import type { HudOrigin } from './useOpenDocument'
import { type Find, useFind } from './useFind'

const REACTION_GLYPH: Record<string, string> = {
  lies: '🚩',
  true: '✅',
  needs_proof: '🔍',
  precedent: '⚖️',
}

const SUGGESTIONS = [
  'Summarise the key arguments in this document.',
  'Which deadlines or dates does this document set?',
  'What does the other side claim here, and what is contested?',
  'Which passages should I verify against our own records?',
]

const SHORTCUTS = [
  ['← / →', 'Previous / next document in the proceeding'],
  ['↑ / ↓', 'Previous / next key passage'],
  ['[ / ]', 'Parent / first attached document'],
  ['{ / }', 'Previous / next document in the bundle'],
  ['f', 'Toggle focus mode (hide the rail)'],
  ['o', 'Open the original file'],
  ['n', 'Pin the active passage'],
  ['r', 'Jump to the reaction bar'],
  ['1 – 4', 'React: lies, true, needs proof, precedent'],
  ['/', 'Ask the AI about this document'],
  ['⌘F / Ctrl+F', 'Find in document'],
  ['Esc', 'Leave focus mode, close find or chat, then go back'],
] as const

/** The full-screen document HUD: rendered body, pins, intelligence rail, chat. */
export function DocumentPage() {
  const docId = Number(useParams().id)
  const query = useDocumentReader(docId)
  const reader = query.data
  useEffect(() => {
    document.title = `${reader?.title ?? 'Document'} | The Sanctuary`
  }, [reader?.title])
  if (!reader) return <QueryState error={query.error} pending={query.isPending} />
  return <Hud key={docId} reader={reader} />
}

function Hud({ reader }: { reader: Reader }) {
  const navigate = useNavigate()
  const location = useLocation()
  const toast = useToast()
  const createPin = useCreatePin(reader.id)
  const react = useReact(reader.id)
  const [reactionFocus, setReactionFocus] = useState(0)
  const article = useRef<HTMLDivElement>(null)
  const [focusMode, setFocusMode] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  const [intelligenceOpen, setIntelligenceOpen] = useState(false)
  const inlineRail = useMediaQuery('(min-width: 1280px)')
  const inlineChat = useMediaQuery('(min-width: 1536px)')
  const [prefill, setPrefill] = useState<{ text: string; at: number } | null>(null)
  const shortcuts = useShortcuts()
  usePageShortcuts('Document', SHORTCUTS)
  const [activePassageId, setActivePassageId] = useState<string | null>(null)
  const [zoom, setZoom] = useState(1)
  const find = useFind(article, reader.body_html)

  const passageIds = useMemo(() => reader.key_passages.map((p) => p.id), [reader.key_passages])
  const origin = location.state as HudOrigin | null
  const backHref =
    origin?.from ??
    (reader.case_id && reader.case_id !== '_TRIAGE' ? `/cases/${reader.case_id}` : '/triage')

  const focusPassage = useCallback(
    (passageId: string, push = true) => {
      const mark = article.current?.querySelector<HTMLElement>(`#p-${CSS.escape(passageId)}`)
      if (mark && mark.tagName === 'MARK') {
        mark.scrollIntoView({ block: 'center', behavior: 'smooth' })
        mark.classList.remove('hud-mark-flash')
        void mark.offsetWidth
        mark.classList.add('hud-mark-flash')
      } else {
        article.current?.scrollIntoView({ block: 'start' })
        toast('This passage could not be located in the text.', 'error')
      }
      setActivePassageId(passageId)
      if (push) navigate({ hash: `p=${passageId}` }, { replace: true })
    },
    [toast, navigate],
  )

  // Deep link `#p=<id>` (chat citations, bookmarks).
  useEffect(() => {
    const m = /^#p=(.+)$/.exec(location.hash)
    if (m) {
      const id = decodeURIComponent(m[1] ?? '')
      const t = window.setTimeout(() => focusPassage(id, false), 150)
      return () => window.clearTimeout(t)
    }
  }, [location.hash, focusPassage])

  // Clicking a highlight selects it; scrolling keeps the spine in sync.
  useEffect(() => {
    const root = article.current
    if (!root) return
    const onClick = (e: MouseEvent) => {
      const mark = (e.target as HTMLElement).closest<HTMLElement>('mark[data-passage-id]')
      if (mark?.dataset.passageId) focusPassage(mark.dataset.passageId)
    }
    root.addEventListener('click', onClick)
    const marks = Array.from(root.querySelectorAll<HTMLElement>('mark[data-passage-id]'))
    const observer = new IntersectionObserver(
      (entries) => {
        const hit = entries.find((x) => x.isIntersecting)
        const id = (hit?.target as HTMLElement | undefined)?.dataset.passageId
        if (id) setActivePassageId(id)
      },
      { threshold: 0.5 },
    )
    marks.forEach((m) => observer.observe(m))
    return () => {
      root.removeEventListener('click', onClick)
      observer.disconnect()
    }
  }, [reader.body_html, focusPassage])

  useLayoutEffect(() => {
    article.current
      ?.querySelectorAll<HTMLElement>('mark.is-active')
      .forEach((m) => m.classList.remove('is-active'))
    if (activePassageId) {
      article.current
        ?.querySelector(`#p-${CSS.escape(activePassageId)}`)
        ?.classList.add('is-active')
      document
        .querySelector(`[data-spine-passage="${CSS.escape(activePassageId)}"]`)
        ?.scrollIntoView({ block: 'nearest' })
    }
  }, [activePassageId, reader.body_html])

  function movePassage(delta: 1 | -1) {
    if (passageIds.length === 0) return
    const i = activePassageId ? passageIds.indexOf(activePassageId) : -1
    const next = passageIds[(i + delta + passageIds.length) % passageIds.length]
    if (next) focusPassage(next)
  }

  function pinPassage(passageId: string | null) {
    if (!passageId) {
      toast('Select a passage first (↑/↓ or click a highlight).', 'error')
      return
    }
    createPin.mutate(
      { passage_id: passageId, note: null },
      { onError: (e) => toast(e.message, 'error') },
    )
  }

  function askAbout(text: string) {
    setChatOpen(true)
    setPrefill({ text: text.length > 200 ? `${text.slice(0, 200)}…` : text, at: Date.now() })
  }

  function go(target: number | null | undefined) {
    if (target) navigate(`/document/${target}`, { state: location.state })
  }

  function toggleFocusMode() {
    setIntelligenceOpen(false)
    setFocusMode((v) => !v)
  }
  const rail = useRef<HTMLElement>(null)
  useLayoutEffect(() => {
    if (!inlineRail && rail.current?.querySelector('[role="dialog"]')) setIntelligenceOpen(true)
  }, [inlineRail])
  useEffect(() => {
    if (!reactionFocus) return
    const bar = rail.current?.querySelector<HTMLElement>('[data-reaction-bar]')
    bar?.querySelector<HTMLElement>('button[aria-pressed]')?.focus()
    bar?.scrollIntoView({ block: 'center' })
  }, [reactionFocus])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || overlayOpen()) return
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
        e.preventDefault()
        find.open()
        return
      }
      if (isTypingTarget(e) || hasModifier(e)) return
      // The shortcuts overview owns the keyboard while open (its own Esc closes it).
      if (shortcuts.open) return
      const nav = reader.nav
      switch (e.key) {
        case 'ArrowLeft':
          go(nav.prev_doc_id)
          break
        case 'ArrowRight':
          go(nav.next_doc_id)
          break
        case 'ArrowUp':
          e.preventDefault()
          movePassage(-1)
          break
        case 'ArrowDown':
          e.preventDefault()
          movePassage(1)
          break
        case '[':
          go(nav.parent_id)
          break
        case ']':
          go(nav.first_child_id)
          break
        case '{':
          go(nav.bundle_prev_id)
          break
        case '}':
          go(nav.bundle_next_id)
          break
        case 'f':
          toggleFocusMode()
          break
        case 'o':
          if (reader.has_original) window.open(originalUrl(reader.id), '_blank', 'noopener')
          break
        case 'n':
          pinPassage(activePassageId)
          break
        case 'r':
          setFocusMode(false)
          if (!inlineRail) setIntelligenceOpen(true)
          setReactionFocus((n) => n + 1)
          break
        case '1':
        case '2':
        case '3':
        case '4':
          {
            const reaction = (['lies', 'true', 'needs_proof', 'precedent'] as const)[
              Number(e.key) - 1
            ]
            if (reaction) react.mutate({ reaction }, { onError: (e) => toast(e.message, 'error') })
          }
          break
        case '/':
          e.preventDefault()
          setChatOpen(true)
          break
        case 'Escape':
          if (find.isOpen) find.close()
          else if (chatOpen) setChatOpen(false)
          else if (focusMode) setFocusMode(false)
          else navigate(backHref)
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const tierTone: Tone =
    reader.significance_tier === 'critical'
      ? 'danger'
      : reader.significance_tier === 'significant'
        ? 'warning'
        : 'neutral'
  const firstReaction = reader.reactions[0]?.reaction

  return (
    <div className="flex h-full flex-col overflow-hidden" data-testid="document-hud">
      <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-line bg-panel px-4 py-2 text-[12px]">
        <Link
          to={backHref}
          className="flex items-center gap-1 text-muted hover:text-ink"
          title="Back (Esc)"
        >
          <Icon name="arrow_back" size={16} />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 font-mono text-[10.5px] text-muted">
            <span>
              {reader.case_id && reader.case_id !== '_TRIAGE' ? reader.case_id : 'Triage'}
            </span>
            {reader.proceeding && (
              <span>
                › {reader.proceeding.court_name}
                {reader.proceeding.az_court ? ` · ${reader.proceeding.az_court}` : ''}
              </span>
            )}
          </div>
          <div className="flex min-w-0 items-center gap-2">
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${ORIGINATOR_COLOR[reader.originator_type]}`}
            />
            <h1 className="truncate font-display text-[14px] font-bold">{reader.title}</h1>
            {reader.significance_tier && <Badge tone={tierTone}>{reader.significance_tier}</Badge>}
            {reader.issued_date && (
              <span className="font-mono text-[10.5px] text-muted">
                {formatShortDate(reader.issued_date)}
              </span>
            )}
            {reader.thread_open && <Badge tone="warning">thread open</Badge>}
            {reader.context_strategy === 'windowed' && <Badge>split view</Badge>}
            {reader.context_strategy === 'chunked' && (
              <Badge tone={reader.context_truncated ? 'warning' : 'neutral'}>
                read in {reader.context_windows ?? 'several'} parts
                {reader.context_truncated && ', the rest not read'}
              </Badge>
            )}
            {firstReaction && <span title="Your reaction">{REACTION_GLYPH[firstReaction]}</span>}
          </div>
        </div>

        <nav aria-label="Document navigation" className="flex items-center gap-1">
          <IconLink
            to={reader.nav.prev_doc_id}
            icon="chevron_left"
            label="Previous document (←)"
            state={location.state}
          />
          <span className="font-mono text-[10.5px] text-muted">
            {reader.nav.total ? `${reader.nav.position} / ${reader.nav.total}` : '—'}
          </span>
          <IconLink
            to={reader.nav.next_doc_id}
            icon="chevron_right"
            label="Next document (→)"
            state={location.state}
          />
        </nav>

        <div className="flex flex-wrap items-center gap-1">
          {!inlineRail && (
            <ToolButton
              label="Document intelligence"
              icon="info"
              pressed={intelligenceOpen}
              onClick={() => setIntelligenceOpen(true)}
            />
          )}
          <FindBox find={find} />
          <ToolButton
            label="Zoom out"
            icon="zoom_out"
            onClick={() => setZoom((z) => Math.max(0.7, +(z - 0.1).toFixed(2)))}
          />
          <ToolButton
            label="Zoom in"
            icon="zoom_in"
            onClick={() => setZoom((z) => Math.min(1.8, +(z + 0.1).toFixed(2)))}
          />
          {reader.has_original && (
            <a
              href={originalUrl(reader.id)}
              target="_blank"
              rel="noopener"
              title="Open original (o)"
              aria-label="Open original"
              className="rounded-md p-1 text-muted hover:bg-accent/7 hover:text-ink"
            >
              <Icon name="open_in_new" size={16} />
            </a>
          )}
          <ToolButton
            label={focusMode ? 'Show the rail (f)' : 'Focus mode (f)'}
            icon={focusMode ? 'view_sidebar' : 'center_focus_strong'}
            pressed={focusMode}
            onClick={toggleFocusMode}
          />
          <ToolButton
            label="Ask the AI (/)"
            icon="forum"
            pressed={chatOpen}
            onClick={() => setChatOpen((v) => !v)}
          />
          <ToolButton label="Keyboard shortcuts (?)" icon="keyboard" onClick={shortcuts.show} />
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="relative flex min-w-0 flex-1 overflow-y-auto bg-bg">
          <div
            className={`mx-auto min-w-0 w-full max-w-[760px] px-8 py-6 ${reader.pins.length ? 'xl:ml-56 xl:w-[calc(100%-14rem)]' : ''}`}
          >
            {reader.body_html ? (
              <div
                ref={article}
                className="reader"
                style={{ ['--reader-zoom' as string]: zoom }}
                // Server-rendered markdown (html disabled) with <mark> highlights.
                dangerouslySetInnerHTML={{ __html: reader.body_html }}
              />
            ) : (
              <p ref={article} className="text-muted">
                No text content available for this document yet.
              </p>
            )}
            <PinGutter docId={reader.id} pins={reader.pins} article={article} />
          </div>
        </div>

        <Drawer
          keepMounted
          open={intelligenceOpen}
          inline={inlineRail && !focusMode}
          inlineWidth={340}
          onClose={() => setIntelligenceOpen(false)}
          title="Document intelligence"
        >
          <aside
            ref={rail}
            aria-label="Document intelligence"
            className="h-full overflow-y-auto border-l border-line bg-card2 text-[12px]"
          >
            <ReviewSections
              review={reader}
              singleColumn
              passages={{
                activePassageId,
                onFocus: (id) => {
                  focusPassage(id)
                  setIntelligenceOpen(false)
                },
                onPin: pinPassage,
                onAskAi: (text) => {
                  setIntelligenceOpen(false)
                  askAbout(text)
                },
              }}
            />
            {!chatOpen && (
              <button
                type="button"
                onClick={() => {
                  setIntelligenceOpen(false)
                  setChatOpen(true)
                }}
                className="m-3 flex w-[calc(100%-1.5rem)] items-center justify-center gap-2 rounded-xl border border-dashed border-line px-3 py-2 text-[11.5px] text-muted hover:border-accent hover:text-ink"
              >
                <Icon name="forum" size={14} /> Ask about this document{' '}
                <kbd className="font-mono text-[9px]">/</kbd>
              </button>
            )}
          </aside>
        </Drawer>
        <Drawer
          open={chatOpen}
          inline={inlineChat && chatOpen}
          onClose={() => setChatOpen(false)}
          title="Ask about this document"
          header={false}
        >
          <ChatDrawer
            scope={{ scope_type: 'document', scope_id: String(reader.id) }}
            title="Ask about this document"
            suggestions={SUGGESTIONS}
            onClose={() => setChatOpen(false)}
            prefill={prefill}
          />
        </Drawer>
      </div>
    </div>
  )
}

function IconLink({
  to,
  icon,
  label,
  state,
}: {
  to: number | null
  icon: string
  label: string
  state: unknown
}) {
  if (!to) {
    return (
      <span aria-disabled className="rounded-md p-1 text-line3">
        <Icon name={icon} size={16} />
      </span>
    )
  }
  return (
    <Link
      to={`/document/${to}`}
      state={state}
      aria-label={label}
      title={label}
      className="rounded-md p-1 text-muted hover:bg-accent/7 hover:text-ink"
    >
      <Icon name={icon} size={16} />
    </Link>
  )
}

function ToolButton({
  label,
  icon,
  onClick,
  pressed,
}: {
  label: string
  icon: string
  onClick: () => void
  pressed?: boolean
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={pressed}
      onClick={onClick}
      className={`rounded-md p-1 hover:bg-accent/7 hover:text-ink ${pressed ? 'text-accent' : 'text-muted'}`}
    >
      <Icon name={icon} size={16} />
    </button>
  )
}

function FindBox({ find }: { find: Find }) {
  if (!find.isOpen) {
    return <ToolButton label="Find in document (⌘F)" icon="search" onClick={find.open} />
  }
  return (
    <div className="flex items-center gap-1 rounded-md border border-line bg-card px-1.5">
      <Icon name="search" size={14} className="text-muted" />
      <input
        autoFocus
        value={find.query}
        onChange={(e) => find.setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            find.close()
          } else if (e.key === 'Enter') {
            e.preventDefault()
            find.next(e.shiftKey ? -1 : 1)
          }
        }}
        aria-label="Find in document"
        placeholder="Find…"
        className="w-32 bg-transparent py-0.5 text-[11.5px] outline-none"
      />
      <span className="font-mono text-[10px] text-muted">
        {find.total ? `${find.index + 1}/${find.total}` : find.query ? '0' : ''}
      </span>
      <button
        type="button"
        aria-label="Previous match"
        onClick={() => find.next(-1)}
        className="text-muted hover:text-ink"
      >
        <Icon name="expand_less" size={14} />
      </button>
      <button
        type="button"
        aria-label="Next match"
        onClick={() => find.next(1)}
        className="text-muted hover:text-ink"
      >
        <Icon name="expand_more" size={14} />
      </button>
      <button
        type="button"
        aria-label="Close find"
        onClick={find.close}
        className="text-muted hover:text-ink"
      >
        <Icon name="close" size={14} />
      </button>
    </div>
  )
}
