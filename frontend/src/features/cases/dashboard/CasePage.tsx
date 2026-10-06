import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import { type CaseDetail, useCaseDetail, useSetActiveProceeding } from '../../../api/caseDetail'
import { useCloseDecision } from '../../../api/cases'
import { useDraftDecision } from '../../../api/triage'

import { Badge, type Tone } from '../../../ui/Badge'
import { Button } from '../../../ui/Button'
import { Icon } from '../../../ui/Icon'
import { QueryState } from '../../../ui/QueryState'
import { useToast } from '../../../ui/toast'
import { ChatDrawer } from '../../chat/ChatDrawer'
import { CostsTab } from './CostsTab'
import { EditCaseModal } from './EditCaseModal'
import { GraphTab } from './GraphTab'
import { Rail } from './Rail'
import { ReviewTab } from './ReviewTab'
import { SharingModal } from './SharingModal'
import { TimelineTab } from './TimelineTab'
import { TruthMapTab } from './TruthMapTab'

export type View = 'review' | 'graph' | 'truth' | 'timeline' | 'fin'
const VIEWS: [View, string, string][] = [
  ['review', 'Review', 'r'],
  ['graph', 'Graph', 'g'],
  ['truth', 'Truth map', 't'],
  ['timeline', 'Calendar', 'l'],
  ['fin', 'Costs', '$'],
]
const KEY_TO_VIEW: Record<string, View> = {
  g: 'graph',
  t: 'truth',
  l: 'timeline',
  $: 'fin',
  r: 'review',
}

const STATUS_TONE: Record<string, Tone> = {
  intake: 'neutral',
  discovery: 'accent',
  pre_trial: 'warning',
  trial: 'danger',
  post_trial: 'warning',
  closed: 'success',
}

const SUGGESTIONS = [
  'What is the current posture of this case?',
  'Which deadlines are coming up and what do they require?',
  'Summarise what the opposing side has claimed so far.',
  'What is our cost exposure right now?',
]

/** The case dashboard: spine, review/graph/truth/calendar/costs views and the brief rail. */
export function CasePage() {
  const caseId = useParams().caseId ?? ''
  const [params, setParams] = useSearchParams()
  const requestedProceeding = params.get('proceeding') ? Number(params.get('proceeding')) : null
  const view = (params.get('view') as View | null) ?? 'review'
  const query = useCaseDetail(caseId, requestedProceeding)
  const detail = query.data
  useEffect(() => {
    document.title = `${detail ? `${detail.id} · ${detail.title}` : caseId} | The Sanctuary`
  }, [detail, caseId])
  if (!detail) return <QueryState error={query.error} pending={query.isPending} />
  return (
    <Dashboard
      key={caseId}
      detail={detail}
      view={VIEWS.some(([v]) => v === view) ? view : 'review'}
      setView={(v) => {
        params.set('view', v)
        setParams(params, { replace: true })
      }}
    />
  )
}

function Dashboard({
  detail,
  view,
  setView,
}: {
  detail: CaseDetail
  view: View
  setView: (v: View) => void
}) {
  const navigate = useNavigate()
  const toast = useToast()
  const [params, setParams] = useSearchParams()
  const setActive = useSetActiveProceeding(detail.id)
  const closeDecision = useCloseDecision()
  const draftDecision = useDraftDecision()
  const [selectedDoc, setSelectedDoc] = useState<number | null>(detail.documents[0]?.id ?? null)
  const [chatOpen, setChatOpen] = useState(false)
  const [editing, setEditing] = useState(false)
  const [sharing, setSharing] = useState(false)
  const active = detail.proceedings.find((p) => p.id === detail.active_proceeding_id) ?? null

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable) return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === '/') {
        e.preventDefault()
        setChatOpen(true)
      } else if (e.key === 'Escape') {
        if (chatOpen) setChatOpen(false)
      } else {
        const target = KEY_TO_VIEW[e.key]
        if (target) setView(target)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [chatOpen, setView])

  function switchProceeding(id: number) {
    setActive.mutate(id, { onError: (e) => toast(e.message, 'error') })
    params.set('proceeding', String(id))
    setParams(params, { replace: true })
  }

  return (
    <div className="flex h-full flex-col overflow-hidden" data-testid="case-dashboard">
      <header className="shrink-0 border-b border-line bg-panel px-7 pt-4">
        <div className="mb-2 flex items-center gap-2 font-mono text-[11px] text-muted">
          <Link to="/cases" className="hover:text-ink">
            Cases
          </Link>
          <Icon name="chevron_right" size={14} />
          <span className="text-ink">{detail.id}</span>
          <span>·</span>
          <span className="truncate">{detail.title}</span>
          <span className="ml-auto flex items-center gap-1">
            {detail.can_manage_sharing && (
              <Button
                variant="secondary"
                className="px-2 py-1 text-[11px]"
                onClick={() => setSharing(true)}
              >
                <Icon name="group" size={13} /> Share
              </Button>
            )}
            {detail.can_edit && (
              <Button
                variant="secondary"
                className="px-2 py-1 text-[11px]"
                onClick={() => setEditing(true)}
              >
                <Icon name="edit" size={13} /> Edit
              </Button>
            )}
            <Link
              to={`/triage?upload=1&case_id=${encodeURIComponent(detail.id)}`}
              className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] hover:border-accent"
            >
              <Icon name="upload_file" size={13} /> Add documents
            </Link>
            <Button className="px-2.5 py-1 text-[11px]" onClick={() => setChatOpen(true)}>
              <Icon name="forum" size={13} /> Ask AI{' '}
              <kbd className="font-mono text-[9px] opacity-70">/</kbd>
            </Button>
          </span>
        </div>
        <div className="flex items-center gap-3">
          <h1 className="font-display text-[22px] font-extrabold">{detail.title}</h1>
          <Badge tone={STATUS_TONE[detail.status] ?? 'neutral'}>
            {detail.status.replace('_', ' ')}
          </Badge>
          {detail.is_draft && <Badge tone="warning">AI draft</Badge>}
          {detail.proceedings.length > 0 && (
            <label className="ml-auto flex items-center gap-2 text-[11.5px] text-muted">
              Proceeding
              <select
                aria-label="Proceeding"
                value={active?.id ?? ''}
                onChange={(e) => switchProceeding(Number(e.target.value))}
                className="rounded-md border border-line bg-card px-2 py-1 text-[11.5px] text-ink"
              >
                {detail.proceedings.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.court_name}
                    {p.az_court ? ` · ${p.az_court}` : ''} ({p.doc_count})
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <nav aria-label="Case views" className="mt-3 flex gap-6">
          {VIEWS.map(([key, label, k]) => (
            <button
              key={key}
              type="button"
              onClick={() => setView(key)}
              aria-current={view === key ? 'page' : undefined}
              className={`border-b-2 pb-2.5 text-[12.5px] ${view === key ? 'border-accent font-semibold text-ink' : 'border-transparent text-muted hover:text-ink'}`}
            >
              {label}
              {key === 'truth' && detail.open_claim_count > 0 && (
                <span className="ml-1.5 rounded-full bg-accent/15 px-1.5 font-mono text-[10px] text-accent">
                  {detail.open_claim_count}
                </span>
              )}
              <kbd aria-hidden className="ml-1.5 font-mono text-[9px] text-muted2">
                {k}
              </kbd>
            </button>
          ))}
        </nav>
      </header>

      {detail.is_draft && (
        <Banner tone="warning">
          <span>This case was drafted by the AI from incoming documents.</span>
          <Button
            className="px-2.5 py-1 text-[11px]"
            onClick={() =>
              draftDecision.mutate(
                { caseId: detail.id, decision: 'confirm' },
                {
                  onSuccess: () => toast('Case ratified'),
                  onError: (e) => toast(e.message, 'error'),
                },
              )
            }
          >
            Ratify
          </Button>
          <Button
            variant="secondary"
            className="px-2.5 py-1 text-[11px]"
            onClick={() =>
              draftDecision.mutate(
                { caseId: detail.id, decision: 'reject' },
                { onSuccess: () => navigate('/triage'), onError: (e) => toast(e.message, 'error') },
              )
            }
          >
            Reject
          </Button>
        </Banner>
      )}
      {detail.pending_close && (
        <Banner tone="info">
          <span>
            The AI suggests closing this case.
            {detail.close_suggestion_rationale ? ` ${detail.close_suggestion_rationale}` : ''}
          </span>
          <Button
            className="px-2.5 py-1 text-[11px]"
            onClick={() => closeDecision.mutate({ caseId: detail.id, decision: 'confirm' })}
          >
            Close case
          </Button>
          <Button
            variant="secondary"
            className="px-2.5 py-1 text-[11px]"
            onClick={() => closeDecision.mutate({ caseId: detail.id, decision: 'dismiss' })}
          >
            Keep open
          </Button>
        </Banner>
      )}
      {detail.dormancy_alert && <Banner tone="warning">{detail.dormancy_alert}</Banner>}

      <div className="flex min-h-0 flex-1">
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-card2">
          {view === 'review' && (
            <ReviewTab detail={detail} selectedDoc={selectedDoc} onSelect={setSelectedDoc} />
          )}
          {view === 'graph' && (
            <GraphTab detail={detail} onOpen={setSelectedDoc} selectedDoc={selectedDoc} />
          )}
          {view === 'truth' && <TruthMapTab detail={detail} />}
          {view === 'timeline' && <TimelineTab detail={detail} />}
          {view === 'fin' && <CostsTab detail={detail} />}
        </main>
        <Rail
          detail={detail}
          onOpenDoc={(id) => {
            setSelectedDoc(id)
            setView('review')
          }}
        />
        {chatOpen && (
          <ChatDrawer
            scope={{ scope_type: 'case', scope_id: detail.id }}
            title={`Ask about ${detail.id}`}
            suggestions={SUGGESTIONS}
            onClose={() => setChatOpen(false)}
            proceeding={active ? { id: active.id, name: active.court_name } : null}
          />
        )}
      </div>

      <EditCaseModal open={editing} onClose={() => setEditing(false)} detail={detail} />
      <SharingModal open={sharing} onClose={() => setSharing(false)} caseId={detail.id} />
    </div>
  )
}

function Banner({ tone, children }: { tone: 'warning' | 'info'; children: React.ReactNode }) {
  const cls = tone === 'warning' ? 'border-warning/40 bg-warning/10' : 'border-info/40 bg-info/10'
  return (
    <div role="status" className={`flex items-center gap-3 border-b px-7 py-2 text-[12px] ${cls}`}>
      {children}
    </div>
  )
}
