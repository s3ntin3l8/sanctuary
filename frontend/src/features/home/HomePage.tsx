import { Link } from 'react-router'
import { useEffect, useState } from 'react'

import type { Schemas } from '../../api/client'
import { useHome, useReviewAll } from '../../api/home'
import { useWorkerQueue } from '../../api/shell'
import {
  daysUntil,
  formatDueRelative,
  formatEurCompact,
  formatLongDate,
  formatShortDate,
  pluralize,
} from '../../format'
import { ROW_ATTR, useRovingRows } from '../../shell/keys'
import { usePageShortcuts } from '../../shell/shortcuts'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { Panel } from '../../ui/Panel'
import { CaseCardTile } from '../cases/CaseCardTile'
import { CreateCaseModal } from '../cases/CreateCaseModal'
import { ActivityPanel } from './ActivityPanel'
import { BriefingCard } from './BriefingCard'

type Home = Schemas['HomeView']

const HOME_SHORTCUTS = [
  ['j / k', 'Next / previous item across the panels'],
  ['Enter', 'Open the highlighted item'],
] as const

/** Keyboard-reachable rows: `j`/`k` focus them, Enter follows the link. */
const ROW = { [ROW_ATTR]: '' } as const
const ROW_FOCUS = 'focus-visible:bg-accent/10 focus-visible:outline-none'

const ACTION_ICONS: Record<string, string> = {
  court_date: 'event',
  deadline: 'timer',
  response_required: 'flag',
  filing_required: 'flag',
}

export function HomePage() {
  const home = useHome()
  const queue = useWorkerQueue().data
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    document.title = 'Home | The Sanctuary'
  }, [])
  usePageShortcuts('Home', HOME_SHORTCUTS)
  useRovingRows()

  const data = home.data
  const processing = queue ? queue.counts.executing + queue.counts.queued : 0
  const exposure = data?.active_cases.reduce((sum, c) => sum + c.exposure_eur, 0) ?? 0

  return (
    <div className="flex min-h-full flex-col">
      <header className="flex items-center gap-4 border-b border-line bg-panel px-6 py-4">
        <div>
          <p className="font-mono text-[11px] text-muted">
            {data ? formatLongDate(data.now) : ' '}
          </p>
          <h1 className="font-display text-[22px] font-extrabold tracking-tight">
            {data ? `${data.greeting}, ${data.user_name}.` : 'Loading…'}
          </h1>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button
            variant="secondary"
            onClick={() => window.dispatchEvent(new Event('open-command-palette'))}
          >
            <Icon name="search" size={16} /> Search{' '}
            <kbd className="font-mono text-[10px] text-muted">⌘K</kbd>
          </Button>
          <Button onClick={() => setCreating(true)}>
            <Icon name="add" size={16} /> New case
          </Button>
        </div>
      </header>

      {home.error && (
        <p role="alert" className="px-6 py-4 text-[12px] text-danger">
          {home.error.message}
        </p>
      )}

      {data && (
        <div className="space-y-4 px-6 py-4">
          <BriefingCard />

          <div className="grid grid-cols-5 gap-4">
            <Kpi label="Deadlines" value={data.today_items.length} icon="timer" />
            <Kpi label="Triage" value={data.triage_bundles.length} icon="inbox" />
            <Kpi label="Processing" value={processing} icon="sync" />
            <Kpi label="Active cases" value={data.active_cases.length} icon="folder_open" />
            <Kpi label="Exposure" value={formatEurCompact(exposure)} icon="payments" />
          </div>

          <div className="grid grid-cols-5 items-start gap-4">
            <div className="col-span-3 space-y-4">
              {data.caught_up ? (
                <div className="rounded-xl border border-line bg-card px-6 py-10 text-center">
                  <Icon name="check_circle" size={28} className="text-accent" />
                  <p className="mt-2 font-display text-[15px] font-bold">You're caught up.</p>
                  <p className="text-[12px] text-muted">
                    No deadlines, triage or signals need you right now.
                  </p>
                </div>
              ) : (
                <>
                  <DeadlinesPanel items={data.today_items} />
                  <TriagePanel bundles={data.triage_bundles} />
                </>
              )}

              {data.draft_cases.length > 0 && (
                <Panel
                  title="Pending confirmation"
                  icon="pending"
                  meta={pluralize(data.draft_cases.length, 'draft')}
                >
                  <CaseGrid cards={data.draft_cases} items={data.today_items} />
                </Panel>
              )}

              <Panel
                title="Active cases"
                icon="folder_open"
                meta={pluralize(data.active_cases.length, 'case')}
                action={
                  <Link to="/cases" className="text-[11px] text-tealink hover:underline">
                    View all
                  </Link>
                }
              >
                {data.active_cases.length === 0 ? (
                  <p className="py-4 text-center text-[12px] text-muted">No active cases yet.</p>
                ) : (
                  <CaseGrid cards={data.active_cases} items={data.today_items} />
                )}
              </Panel>
            </div>
            <div className="col-span-2 space-y-4">
              <DeltaPanel home={data} />
              <SignalsPanel signals={data.signals} />
              <ActivityPanel events={data.activity} row={ROW} />
            </div>
          </div>
        </div>
      )}
      <CreateCaseModal open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}

function Kpi({ label, value, icon }: { label: string; value: number | string; icon: string }) {
  return (
    <div className="rounded-xl border border-line bg-card px-4 py-3">
      <div className="flex items-center gap-1.5 text-[9.5px] font-extrabold tracking-[.12em] text-muted uppercase">
        <Icon name={icon} size={13} /> {label}
      </div>
      <div className="mt-1 font-mono text-[22px] font-semibold text-ink">{value}</div>
    </div>
  )
}

function DeadlinesPanel({ items }: { items: Home['today_items'] }) {
  return (
    <Panel title="Needs you" icon="timer" meta={pluralize(items.length, 'item')}>
      {items.length === 0 ? (
        <p className="py-2 text-[12px] text-muted">Nothing due in the next 30 days.</p>
      ) : (
        <ul className="divide-y divide-line2">
          {items.map((item) => {
            const urgent = item.due_date ? daysUntil(item.due_date) < 7 : false
            return (
              <li key={item.id}>
                <Link
                  to={`/cases/${item.case_id}`}
                  {...ROW}
                  className={`grid grid-cols-[18px_1.4fr_1fr_110px] items-center gap-3 py-2 text-[12px] hover:bg-accent/5 ${ROW_FOCUS}`}
                >
                  <Icon
                    name={ACTION_ICONS[item.action_type] ?? 'flag'}
                    size={16}
                    className={urgent ? 'text-danger' : 'text-muted'}
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-semibold">{item.title}</span>
                    <span className="block truncate text-[11px] text-muted">
                      {item.description ?? 'No description'}
                    </span>
                  </span>
                  <span className="truncate text-[11px] text-muted">
                    <span className="font-mono text-tealink">{item.case_id}</span> ·{' '}
                    {item.case_title}
                  </span>
                  <span
                    className={`text-right font-mono text-[10px] ${urgent ? 'text-danger' : 'text-muted'}`}
                  >
                    {item.due_date
                      ? `${formatShortDate(item.due_date)} · ${formatDueRelative(item.due_date)}`
                      : 'no date'}
                  </span>
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}

function TriagePanel({ bundles }: { bundles: Home['triage_bundles'] }) {
  return (
    <Panel
      title="Awaiting triage"
      icon="inbox"
      meta={pluralize(bundles.length, 'bundle')}
      action={
        <Link to="/triage" className="text-[11px] text-tealink hover:underline">
          Open triage
        </Link>
      }
    >
      {bundles.length === 0 ? (
        <p className="py-2 text-[12px] text-muted">Inbox is clear.</p>
      ) : (
        <ul className="divide-y divide-line2">
          {bundles.map((b) => {
            const p = b.pipeline
            const busy = p.running + p.pending > 0
            return (
              <li key={b.id}>
                <Link
                  to={`/triage?bundle=${b.key}`}
                  aria-label={`Open ${b.title} in triage`}
                  {...ROW}
                  className={`grid grid-cols-[52px_52px_1fr_auto] items-center gap-3 py-2 text-[12px] hover:bg-accent/5 ${ROW_FOCUS}`}
                >
                  <Badge tone="warning" mono>
                    {b.doc_count} docs
                  </Badge>
                  <span className="font-mono text-[10px] text-muted">
                    {formatShortDate(b.received_at)}
                  </span>
                  <span className="min-w-0">
                    <span className="block truncate font-semibold">{b.title}</span>
                    <span className="block truncate text-[11px] text-muted">
                      {b.sender_email ?? 'Unknown source'}
                      {b.case_id && (
                        <>
                          {' '}
                          → <span className="font-mono text-tealink">{b.case_id}</span>
                        </>
                      )}
                      {!b.case_id && b.suggested_case_id && (
                        <>
                          {' '}
                          → <span className="font-mono text-tealink">
                            {b.suggested_case_id}
                          </span>{' '}
                          (AI suggested)
                        </>
                      )}
                    </span>
                  </span>
                  {p.failed > 0 ? (
                    <Badge tone="danger">{p.failed} failed</Badge>
                  ) : busy ? (
                    <Badge tone="warning">
                      {p.running} processing · {p.pending} queued
                    </Badge>
                  ) : (
                    <Badge tone="success">ready</Badge>
                  )}
                </Link>
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}

/** Two-column card grid; a lone card goes full width with its upcoming actions. */
function CaseGrid({ cards, items }: { cards: Home['active_cases']; items: Home['today_items'] }) {
  const [card] = cards
  if (card && cards.length === 1) {
    return <CaseCardTile card={card} navRow upcoming={items.filter((i) => i.case_id === card.id)} />
  }
  return (
    <div className="grid grid-cols-2 gap-4">
      {cards.map((c) => (
        <CaseCardTile key={c.id} card={c} navRow />
      ))}
    </div>
  )
}

function DeltaPanel({ home }: { home: Home }) {
  const reviewAll = useReviewAll()
  if (!home.last_home_visit || home.delta_cases.length === 0) return null
  return (
    <Panel
      title="Since your last visit"
      icon="update"
      meta={formatShortDate(home.last_home_visit)}
      action={
        <Button
          size="sm"
          variant="secondary"
          disabled={reviewAll.isPending}
          onClick={() => reviewAll.mutate()}
        >
          Review all
        </Button>
      }
    >
      <ul className="divide-y divide-line2">
        {home.delta_cases.map((d) => (
          <li key={d.case_id} className="py-2 text-[12px]">
            <div className="flex items-center gap-2">
              <Link
                to={`/cases/${d.case_id}`}
                {...ROW}
                className={`font-mono text-[11px] text-tealink hover:underline ${ROW_FOCUS}`}
              >
                {d.case_id}
              </Link>
              <span className="truncate font-semibold">{d.case_title}</span>
              <span className="ml-auto flex items-center gap-1">
                <Badge tone="accent">+{pluralize(d.new_doc_count, 'doc')}</Badge>
                {d.new_actions > 0 && (
                  <Badge tone="warning">{pluralize(d.new_actions, 'action')}</Badge>
                )}
                {(d.max_significance === 'critical' || d.max_significance === 'significant') && (
                  <Badge tone={d.max_significance === 'critical' ? 'danger' : 'warning'}>
                    {d.max_significance}
                  </Badge>
                )}
              </span>
            </div>
            <div className="truncate text-[11px] text-muted">{d.doc_titles.join(' · ')}</div>
          </li>
        ))}
      </ul>
    </Panel>
  )
}

function SignalsPanel({ signals }: { signals: Home['signals'] }) {
  if (signals.length === 0) return null
  return (
    <Panel title="Signals" icon="sensors" meta={pluralize(signals.length, 'signal')}>
      <ul className="divide-y divide-line2">
        {signals.map((s) => (
          <li key={s.id}>
            <Link
              to={s.link}
              {...ROW}
              className={`flex items-start gap-2 py-2 text-[12px] hover:bg-accent/5 ${ROW_FOCUS}`}
            >
              <Icon
                name={s.severity === 'warn' ? 'warning' : 'info'}
                size={16}
                className={s.severity === 'warn' ? 'text-warning' : 'text-info'}
              />
              <span className="min-w-0 flex-1">
                <span className="block font-semibold">{s.title}</span>
                <span className="block text-[11px] text-muted">{s.detail}</span>
              </span>
              <Badge>{s.action}</Badge>
            </Link>
          </li>
        ))}
      </ul>
    </Panel>
  )
}
