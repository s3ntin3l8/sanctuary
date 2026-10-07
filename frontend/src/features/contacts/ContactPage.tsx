import { useEffect } from 'react'
import { Link, useSearchParams } from 'react-router'

import { useContact } from '../../api/costs'
import { formatShortDate, initials } from '../../format'
import { Badge } from '../../ui/Badge'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import { ORIGINATOR_COLOR } from '../documents/DocumentReview'

/** A correspondent and everything they sent across the caller's cases. */
export function ContactPage() {
  const name = useSearchParams()[0].get('name') ?? ''
  const query = useContact(name)
  useEffect(() => {
    document.title = `${name || 'Contact'} | The Sanctuary`
  }, [name])
  if (!name) return <p className="p-6 text-[12px] text-muted">No contact selected.</p>
  const c = query.data
  if (!c) return <QueryState error={query.error} pending={query.isPending} />
  return (
    <div className="mx-auto max-w-[900px] p-6 text-[12px]">
      <header className="mb-4 flex items-center gap-3">
        <span className="flex h-11 w-11 items-center justify-center rounded-full bg-accent/15 font-display text-[15px] font-bold text-accent">
          {initials(c.name)}
        </span>
        <div className="min-w-0">
          <h1 className="truncate font-display text-[22px] font-extrabold">{c.name}</h1>
          <Link
            to={`/search?q=${encodeURIComponent(c.name)}`}
            className="text-[11.5px] text-tealink hover:underline"
          >
            Search all references
          </Link>
        </div>
      </header>
      <dl className="mb-5 grid grid-cols-3 gap-3">
        {(
          [
            ['Documents', String(c.document_count)],
            ['Cases', String(c.case_count)],
            ['Last contact', c.last_contact ? formatShortDate(c.last_contact) : '—'],
          ] as const
        ).map(([k, v]) => (
          <div key={k} className="rounded-xl border border-line bg-card p-3">
            <dt className="text-[10px] font-bold tracking-[.12em] text-muted uppercase">{k}</dt>
            <dd className="font-display text-[17px] font-bold">{v}</dd>
          </div>
        ))}
      </dl>
      {c.cases.length > 0 && (
        <ul className="mb-5 flex flex-wrap gap-1.5" aria-label="Cases">
          {c.cases.map((k) => (
            <li key={k.id}>
              <Link
                to={`/cases/${k.id}`}
                className="inline-flex items-center gap-1 rounded-full border border-line px-2.5 py-1 hover:border-accent"
              >
                <span className="font-mono text-[10.5px] text-tealink">{k.id}</span>
                <span className="truncate">{k.title}</span>
                <Badge>{k.status.replace('_', ' ')}</Badge>
              </Link>
            </li>
          ))}
        </ul>
      )}
      <h2 className="mb-2 text-[10px] font-bold tracking-[.12em] text-muted uppercase">
        Communication history
      </h2>
      {c.documents.length === 0 && <p className="text-muted">No documents from this sender.</p>}
      <ol className="divide-y divide-line2">
        {c.documents.map((d) => (
          <li key={d.id}>
            <Link
              to={`/document/${d.id}`}
              className="flex items-start gap-3 py-2.5 hover:bg-accent/5"
            >
              <span
                className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${ORIGINATOR_COLOR[d.originator_type]}`}
              />
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="font-mono text-[10.5px] text-muted">
                    {formatShortDate(d.issued_date ?? d.ingest_date)}
                  </span>
                  {d.case_id && d.case_id !== '_TRIAGE' && (
                    <span className="truncate text-[10.5px] text-muted">
                      {d.case_title ?? d.case_id}
                    </span>
                  )}
                </span>
                <span className="block truncate font-medium">{d.title}</span>
                {d.legal_significance && (
                  <span className="block truncate text-[11px] text-muted">
                    {d.legal_significance}
                  </span>
                )}
              </span>
              <Icon name="chevron_right" size={16} className="text-muted" />
            </Link>
          </li>
        ))}
      </ol>
    </div>
  )
}
