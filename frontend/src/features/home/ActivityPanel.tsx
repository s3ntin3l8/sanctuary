import { Link } from 'react-router'

import type { Schemas } from '../../api/client'
import { formatAgo } from '../../format'
import { Icon } from '../../ui/Icon'
import { Panel } from '../../ui/Panel'

type Event = Schemas['HomeActivityEvent']

const KIND: Record<Event['kind'], { icon: string; tone: string }> = {
  document_ingested: { icon: 'description', tone: 'text-muted' },
  document_enriched: { icon: 'auto_awesome', tone: 'text-accent' },
  pipeline_failed: { icon: 'error', tone: 'text-danger' },
  deadline_extracted: { icon: 'timer', tone: 'text-warning' },
  hearing_scheduled: { icon: 'event', tone: 'text-info' },
  cost_paid: { icon: 'payments', tone: 'text-success' },
  case_closed: { icon: 'folder_off', tone: 'text-muted' },
  brief_refreshed: { icon: 'summarize', tone: 'text-accent' },
  case_shared: { icon: 'group', tone: 'text-tealink' },
}

const LABEL: Record<Event['kind'], string> = {
  document_ingested: 'ingested',
  document_enriched: 'enriched',
  pipeline_failed: 'failed',
  deadline_extracted: 'deadline extracted',
  hearing_scheduled: 'hearing scheduled',
  cost_paid: 'paid',
  case_closed: 'closed',
  brief_refreshed: 'brief refreshed',
  case_shared: 'shared',
}

/**
 * The Home end of the vision's "Activity Log": a bounded strip of what the
 * pipeline and the user did across the visible cases, newest first.
 */
export function ActivityPanel({ events, row }: { events: Event[]; row?: Record<string, ''> }) {
  return (
    <Panel title="Recent activity" icon="history" meta={events.length ? `${events.length}` : ''}>
      {events.length === 0 ? (
        <p className="py-2 text-[12px] text-muted">Nothing has happened yet.</p>
      ) : (
        <ul className="divide-y divide-line2">
          {events.map((e) => (
            <li key={`${e.kind}-${e.link}-${e.occurred_at}`}>
              <Link
                to={e.link}
                {...row}
                className="flex items-start gap-2 py-2 text-[12px] hover:bg-accent/5 focus-visible:bg-accent/10 focus-visible:outline-none"
              >
                <Icon name={KIND[e.kind].icon} size={16} className={KIND[e.kind].tone} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate">
                    <span className="font-semibold">{e.title}</span>{' '}
                    <span className="text-muted">{LABEL[e.kind]}</span>
                  </span>
                  {e.detail && (
                    <span className="block truncate text-[11px] text-muted">{e.detail}</span>
                  )}
                </span>
                <span className="shrink-0 font-mono text-[10px] text-muted">
                  {formatAgo(e.occurred_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
