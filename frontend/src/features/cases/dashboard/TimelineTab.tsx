import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'

import { type CaseDetail, type TimelineView, useCaseTimeline } from '../../../api/caseDetail'
import { formatEur, formatShortDate } from '../../../format'
import { Chip } from '../../../ui/Chip'
import { Icon } from '../../../ui/Icon'
import { QueryState } from '../../../ui/QueryState'
import { ORIGINATOR_COLOR } from '../../documents/DocumentReview'

const KIND_ICON: Record<string, string> = {
  filing: 'description',
  order: 'gavel',
  statement: 'chat',
  report: 'science',
  relay: 'forward_to_inbox',
  payment: 'payments',
  deadline: 'event',
  hearing: 'gavel',
  pending: 'hourglass_top',
  milestone: 'flag',
}
const ACTORS = ['own', 'court', 'opposing', 'third'] as const
const KINDS = [
  'filing',
  'order',
  'statement',
  'report',
  'relay',
  'payment',
  'deadline',
  'hearing',
  'milestone',
] as const

/** The case calendar: every dated event, month ribbon, actor and kind filters. */
export function TimelineTab({ detail }: { detail: CaseDetail }) {
  const navigate = useNavigate()
  const query = useCaseTimeline(detail.id, true)
  const [actor, setActor] = useState<string | null>(null)
  const [kind, setKind] = useState<string | null>(null)
  const [future, setFuture] = useState(true)
  const t = query.data
  const events = useMemo(
    () =>
      (t?.events ?? []).filter(
        (e) =>
          (!actor || e.actor === actor) && (!kind || e.kind === kind) && (future || !e.is_future),
      ),
    [t, actor, kind, future],
  )
  if (!t) return <QueryState error={query.error} pending={query.isPending} />
  const today = t.today.slice(0, 10)
  const rows = annotate(events, today)
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-5 text-[12px]">
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        {ACTORS.map((a) => (
          <Chip key={a} active={actor === a} onClick={() => setActor((v) => (v === a ? null : a))}>
            <span
              className={`h-2 w-2 rounded-full ${ORIGINATOR_COLOR[a === 'third' ? 'third_party' : a]}`}
            />
            {a}
          </Chip>
        ))}
        <span className="mx-1 text-line3">|</span>
        {KINDS.map((k) => (
          <Chip key={k} active={kind === k} onClick={() => setKind((v) => (v === k ? null : k))}>
            {k}
          </Chip>
        ))}
        <Chip active={future} onClick={() => setFuture((v) => !v)}>
          future
        </Chip>
        <span className="ml-auto font-mono text-[11px] text-muted">
          {events.length} / {t.total_count}
        </span>
      </div>
      <ol className="flex gap-1 overflow-x-auto pb-3" aria-label="Months">
        {t.month_buckets.map((m) => (
          <li key={m.key} className="flex w-9 shrink-0 flex-col items-center gap-0.5">
            <span
              className="w-full rounded-sm bg-accent/40"
              style={{ height: 4 + (28 * m.total) / Math.max(m.max_total, 1) }}
              title={`${m.label}: ${m.total}`}
            />
            <a href={`#tl-${m.key}`} className="font-mono text-[9px] text-muted hover:text-ink">
              {m.label}
            </a>
          </li>
        ))}
      </ol>
      <ol className="space-y-1">
        {rows.map(({ e, m, header, marker }) => {
          return (
            <li key={e.id}>
              {header && (
                <h3
                  id={`tl-${m}`}
                  className="sticky top-0 bg-card2 py-1 font-mono text-[10px] text-muted"
                >
                  {m}
                </h3>
              )}
              {marker && (
                <div className="my-1 border-t border-dashed border-accent text-[10px] text-accent">
                  today
                </div>
              )}
              {e.quiet_gap_days && (
                <p className="py-1 text-center font-mono text-[10px] text-muted2">
                  · {e.quiet_gap_days} quiet days ·
                </p>
              )}
              <Row
                docId={e.source_document_id}
                onOpen={(id) => navigate(`/document/${id}`)}
                className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left ${e.source_document_id ? 'cursor-pointer hover:bg-accent/5' : ''} ${e.is_future ? 'opacity-70' : ''}`}
              >
                <span className="w-16 shrink-0 font-mono text-[10.5px] text-muted">
                  {formatShortDate(e.date)}
                </span>
                <span
                  className={`h-2 w-2 shrink-0 rounded-full ${ORIGINATOR_COLOR[e.actor === 'third' ? 'third_party' : e.actor] ?? 'bg-line3'}`}
                />
                <Icon name={KIND_ICON[e.kind] ?? 'circle'} size={14} className="text-muted" />
                <span className="min-w-0 flex-1 truncate">{e.title}</span>
                {e.sig === 'critical' && <span className="text-danger">⚑</span>}
                {e.is_overdue && <span className="font-mono text-[10px] text-danger">OVERDUE</span>}
                {e.amount_eur !== null && (
                  <span
                    className={`font-mono text-[10.5px] ${e.direction === 'credit' ? 'text-success' : 'text-ink2'}`}
                  >
                    {formatEur(e.amount_eur)}
                  </span>
                )}
                {e.claim_count > 0 && (
                  <span className="font-mono text-[10px] text-muted">⚖ {e.claim_count}</span>
                )}
                {e.note && <span className="truncate text-[11px] text-muted">{e.note}</span>}
              </Row>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function annotate(events: TimelineView['events'], today: string) {
  let month = ''
  let todayShown = false
  return events.map((e) => {
    const m = e.date.slice(0, 7)
    const header = m !== month
    month = m
    const marker = !todayShown && e.date.slice(0, 10) > today
    if (marker) todayShown = true
    return { e, m, header, marker }
  })
}

/** A document-backed event is a button; others are plain rows. */
function Row({
  docId,
  onOpen,
  className,
  children,
}: {
  docId: number | null
  onOpen: (id: number) => void
  className: string
  children: React.ReactNode
}) {
  if (docId === null) return <div className={className}>{children}</div>
  return (
    <button type="button" onClick={() => onOpen(docId)} className={className}>
      {children}
    </button>
  )
}
