import { Link } from 'react-router'
import { useEffect, useMemo, useRef, useState } from 'react'

import { type CaseCard, useCasesDirectory, useCloseDecision } from '../../api/cases'
import { daysUntil, formatEur, formatIsoDate, formatShortDate } from '../../format'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Chip } from '../../ui/Chip'
import { Icon } from '../../ui/Icon'
import { caseHref, SignificanceDot } from './CaseCardTile'
import { CreateCaseModal } from './CreateCaseModal'

type Filter = 'active' | 'dormant' | 'closed' | 'all'
type Sort = 'id' | 'activity' | 'exposure'

const GRID = 'grid grid-cols-[14px_118px_2fr_1.4fr_1fr_110px_90px_60px_24px] items-center gap-3'

function matches(card: CaseCard, filter: Filter) {
  if (filter === 'all') return true
  if (filter === 'closed') return card.status === 'closed'
  if (filter === 'dormant') return card.is_dormant && card.status !== 'closed'
  return card.status !== 'closed' && !card.is_dormant
}

function compare(a: CaseCard, b: CaseCard, sort: Sort) {
  if (sort === 'activity') return (b.last_activity_at ?? '').localeCompare(a.last_activity_at ?? '')
  if (sort === 'exposure') return b.exposure_eur - a.exposure_eur
  return a.id.localeCompare(b.id)
}

export function CasesPage() {
  const directory = useCasesDirectory()
  const [filter, setFilter] = useState<Filter>('active')
  const [sort, setSort] = useState<Sort>('id')
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const search = useRef<HTMLInputElement>(null)

  useEffect(() => {
    document.title = 'Cases | The Sanctuary'
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault()
        search.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const cards = useMemo(() => directory.data?.cases ?? [], [directory.data])
  const counts = useMemo(
    () => ({
      active: cards.filter((c) => matches(c, 'active')).length,
      dormant: cards.filter((c) => matches(c, 'dormant')).length,
      closed: cards.filter((c) => matches(c, 'closed')).length,
      all: cards.length,
    }),
    [cards],
  )
  const q = query.trim().toLowerCase()
  const rows = cards
    .filter((c) => matches(c, filter))
    .filter(
      (c) =>
        !q ||
        [c.id, c.title, c.client_name, c.opposing_party].some((v) => v.toLowerCase().includes(q)),
    )
    .sort((a, b) => compare(a, b, sort))

  return (
    <div className="flex min-h-full flex-col">
      <header className="flex items-center gap-4 border-b border-line bg-panel px-6 py-4">
        <div>
          <h1 className="font-display text-[22px] font-extrabold tracking-tight">Cases</h1>
          <p className="font-mono text-[11px] text-muted">
            {counts.all} total · {counts.active} active
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-2 rounded-[9px] border border-line bg-panel2 px-3 py-1.5">
            <Icon name="filter_list" size={16} className="text-muted" />
            <input
              ref={search}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter cases…"
              aria-label="Filter cases"
              className="w-44 bg-transparent text-[12px] outline-none placeholder:text-muted2"
            />
            <kbd className="font-mono text-[9px] text-muted">⌘F</kbd>
          </label>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as Sort)}
            aria-label="Sort cases"
            className="rounded-[9px] border border-line bg-panel2 px-2 py-1.5 text-[12px]"
          >
            <option value="id">Sort: ID</option>
            <option value="activity">Sort: Last activity</option>
            <option value="exposure">Sort: Exposure</option>
          </select>
          <Button onClick={() => setCreating(true)}>
            <Icon name="add" size={16} /> New case
          </Button>
        </div>
      </header>

      <div className="flex gap-2 px-6 py-3">
        {(['active', 'dormant', 'closed', 'all'] as const).map((f) => (
          <Chip key={f} active={filter === f} count={counts[f]} onClick={() => setFilter(f)}>
            {f[0]?.toUpperCase() + f.slice(1)}
          </Chip>
        ))}
      </div>

      <div className="px-6 pb-6">
        <div
          className={`${GRID} rounded-t-lg border border-line bg-panel2 px-3 py-2 text-[9.5px] font-extrabold tracking-[.1em] text-muted uppercase`}
        >
          <span />
          <span>Case</span>
          <span>Title</span>
          <span>Forum</span>
          <span>Status</span>
          <span>Next deadline</span>
          <span className="text-right">Exposure</span>
          <span className="text-right">Docs</span>
          <span />
        </div>
        {directory.isPending && <p className="px-3 py-6 text-[12px] text-muted">Loading cases…</p>}
        {directory.error && (
          <p role="alert" className="px-3 py-6 text-[12px] text-danger">
            {directory.error.message}
          </p>
        )}
        {directory.data && rows.length === 0 && (
          <p className="border border-t-0 border-line px-3 py-8 text-center text-[12px] text-muted">
            {cards.length === 0 ? 'No cases yet. Create one to get started.' : 'No cases match.'}
          </p>
        )}
        <ul className="divide-y divide-line2 border border-t-0 border-line">
          {rows.map((card) => (
            <CaseRow key={card.id} card={card} />
          ))}
        </ul>
      </div>
      <CreateCaseModal open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}

function CaseRow({ card }: { card: CaseCard }) {
  const decide = useCloseDecision()
  const due = card.next_action?.due_date ?? null
  const urgent = due ? daysUntil(due) <= 7 : false
  return (
    <li>
      <Link to={caseHref(card)} className={`${GRID} px-3 py-2.5 text-[12px] hover:bg-accent/5`}>
        <SignificanceDot tier={card.max_significance} />
        <span className="truncate font-mono text-[11px] font-semibold text-tealink">{card.id}</span>
        <span className="min-w-0">
          <span className="block truncate font-semibold text-ink">{card.title}</span>
          <span className="block truncate text-[11px] text-muted">
            {card.client_name} vs. {card.opposing_party}
          </span>
        </span>
        <span className="min-w-0">
          <span className="block truncate">{card.proceeding_name}</span>
          <span className="block truncate text-[11px] text-muted">{card.matter_type}</span>
        </span>
        <span className="flex flex-wrap items-center gap-1">
          {card.is_draft && <Badge tone="warning">Draft</Badge>}
          <Badge>{card.status_label}</Badge>
          <span className="block w-full font-mono text-[10px] text-muted">
            {card.last_activity_at ? formatIsoDate(card.last_activity_at) : 'Unknown'}
          </span>
        </span>
        <span>
          {due ? (
            <Badge tone={urgent ? 'danger' : 'neutral'} mono>
              ⚑ {formatShortDate(due)}
            </Badge>
          ) : (
            <span className="text-muted2">—</span>
          )}
        </span>
        <span className="text-right font-mono">{formatEur(card.exposure_eur)}</span>
        <span className="text-right font-mono text-muted">
          {card.doc_count}
          {card.new_docs > 0 && <span className="text-tealink"> +{card.new_docs}</span>}
          {card.to_review_count > 0 && (
            <span className="text-warning" title="documents to review">
              {' '}
              ⚑{card.to_review_count}
            </span>
          )}
        </span>
        <Icon name="chevron_right" size={16} className="text-muted" />
      </Link>
      {card.pending_close && (
        <div className="flex items-center gap-3 border-t border-warning/30 bg-warning/8 px-3 py-2 text-[11px]">
          <Icon name="auto_awesome" size={14} className="text-warning" />
          <span className="flex-1">AI suggests closing this case.</span>
          <Button
            size="sm"
            variant="secondary"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ caseId: card.id, decision: 'dismiss' })}
          >
            Keep open
          </Button>
          <Button
            size="sm"
            disabled={decide.isPending}
            onClick={() => decide.mutate({ caseId: card.id, decision: 'confirm' })}
          >
            Close case
          </Button>
        </div>
      )}
    </li>
  )
}
