import { Link } from 'react-router'
import type { CaseCard } from '../../api/cases'
import type { Schemas } from '../../api/client'
import { daysUntil, formatDueRelative, formatEur, formatShortDate } from '../../format'
import { ROW_ATTR } from '../../shell/keys'
import { Badge } from '../../ui/Badge'
import { Icon } from '../../ui/Icon'

const ACTION_ICONS: Record<string, string> = {
  court_date: 'event',
  deadline: 'timer',
  response_required: 'flag',
  filing_required: 'flag',
}

export function SignificanceDot({ tier }: { tier: CaseCard['max_significance'] }) {
  const color =
    tier === 'critical'
      ? 'bg-danger'
      : tier === 'significant'
        ? 'bg-warning'
        : tier === 'informational'
          ? 'bg-info'
          : 'bg-line3'
  return <span aria-hidden className={`inline-block h-2 w-2 shrink-0 rounded-full ${color}`} />
}

const UPCOMING_LIMIT = 3

/**
 * A case summary card (Home grids). The whole card links to the case.
 * `upcoming` (the case's open action items) switches on the wide layout, used
 * when a lone card would otherwise leave half the panel empty.
 */
export function CaseCardTile({
  card,
  navRow = false,
  upcoming,
}: {
  card: CaseCard
  navRow?: boolean
  upcoming?: Schemas['HomeActionItem'][]
}) {
  const action = card.next_action
  const urgent = action?.due_date ? daysUntil(action.due_date) <= 7 : false
  const wide = upcoming !== undefined
  // The next action is already shown on the left; list the rest here.
  const rest = (upcoming ?? []).filter((i) => i.id !== action?.id)
  return (
    <Link
      // Open work goes straight to the documents that need it.
      to={`/cases/${card.id}${card.to_review_count > 0 ? '?view=review&open=1' : ''}`}
      {...(navRow ? { [ROW_ATTR]: '' } : {})}
      className={`rounded-xl border border-line bg-card2 p-3 transition-colors hover:border-accent/40 focus-visible:border-accent focus-visible:outline-none ${wide ? 'grid grid-cols-2 gap-6' : 'flex flex-col gap-2'}`}
    >
      <div className="flex min-w-0 flex-col gap-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] font-semibold text-tealink">{card.id}</span>
          <SignificanceDot tier={card.max_significance} />
          {card.new_docs > 0 && <Badge tone="accent">+{card.new_docs} new</Badge>}
          {card.to_review_count > 0 && (
            <Badge tone="warning">{card.to_review_count} to review</Badge>
          )}
          <span className="flex-1" />
          {card.is_draft && <Badge tone="warning">Draft</Badge>}
          <Badge>{card.status_label}</Badge>
        </div>
        <div className="truncate text-[13px] font-semibold text-ink">{card.title}</div>
        <div className="truncate text-[11px] text-muted">
          {card.client_name} vs. {card.opposing_party} · {card.proceeding_name}
        </div>
        {action ? (
          <div
            className={`flex items-center gap-1.5 text-[11px] ${urgent ? 'text-danger' : 'text-ink2'}`}
          >
            <Icon name={ACTION_ICONS[action.action_type] ?? 'flag'} size={14} />
            <span className="truncate">{action.title}</span>
            {action.due_date && (
              <span className="ml-auto shrink-0 font-mono text-[10px]">
                {formatShortDate(action.due_date)} · {formatDueRelative(action.due_date)}
              </span>
            )}
          </div>
        ) : (
          <div className="text-[11px] text-muted2">No open action</div>
        )}
        <div className="flex items-center gap-3 border-t border-line2 pt-2 font-mono text-[10px] text-muted">
          <span>{formatEur(card.exposure_eur)}</span>
          <span>{card.doc_count} docs</span>
          <span className={`ml-auto ${card.is_dormant ? 'text-danger' : ''}`}>
            {card.days_since_activity}d {card.is_dormant ? 'quiet' : 'ago'}
          </span>
        </div>
      </div>
      {wide && (
        <div className="flex min-w-0 flex-col gap-1.5 border-l border-line2 pl-6">
          <div className="text-[10px] font-bold tracking-[.1em] text-muted uppercase">
            Coming up
          </div>
          {rest.length === 0 ? (
            <div className="text-[11px] text-muted2">
              {action ? 'Nothing else coming up' : 'Nothing coming up'}
            </div>
          ) : (
            rest.slice(0, UPCOMING_LIMIT).map((item) => {
              const soon = item.due_date ? daysUntil(item.due_date) <= 7 : false
              return (
                <div
                  key={item.id}
                  className={`flex items-center gap-1.5 text-[11px] ${soon ? 'text-danger' : 'text-ink2'}`}
                >
                  <Icon name={ACTION_ICONS[item.action_type] ?? 'flag'} size={14} />
                  <span className="truncate">{item.title}</span>
                  {item.due_date && (
                    <span className="ml-auto shrink-0 font-mono text-[10px]">
                      {formatShortDate(item.due_date)} · {formatDueRelative(item.due_date)}
                    </span>
                  )}
                </div>
              )
            })
          )}
          {rest.length > UPCOMING_LIMIT && (
            <div className="font-mono text-[10px] text-muted">
              +{rest.length - UPCOMING_LIMIT} more
            </div>
          )}
        </div>
      )}
    </Link>
  )
}
