import { useEffect } from 'react'
import { Link, useSearchParams } from 'react-router'

import { useSearch } from '../../api/shell'
import { Badge } from '../../ui/Badge'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'

/** Full search results (the palette shows the first few of each kind). */
export function SearchPage() {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const query = useSearch(q, 120)
  useEffect(() => {
    document.title = `${q ? `“${q}” · ` : ''}Search | The Sanctuary`
  }, [q])
  const r = query.data
  return (
    <div className="mx-auto max-w-[900px] p-6 text-[12px]">
      <form
        className="mb-4 flex items-center gap-2 rounded-xl border border-line bg-card px-3 py-2"
        onSubmit={(e) => {
          e.preventDefault()
          const next = String(new FormData(e.currentTarget).get('q') ?? '').trim()
          setParams(next ? { q: next } : {})
        }}
      >
        <Icon name="search" size={16} className="text-muted" />
        <input
          name="q"
          defaultValue={q}
          aria-label="Search"
          placeholder="Search cases, documents, contacts…"
          className="flex-1 bg-transparent text-[14px] outline-none"
          autoFocus
        />
      </form>
      {q.trim().length < 2 && <p className="text-muted">Type at least two characters.</p>}
      {q.trim().length >= 2 && !r && <QueryState error={query.error} pending={query.isPending} />}
      {r && (
        <>
          <p className="mb-3 font-mono text-[11px] text-muted">
            {r.total + r.contacts.length} results for “{q}”
          </p>
          <Section title="Documents" count={r.documents.length}>
            {r.documents.map((d) => (
              <li key={d.id}>
                <Link
                  to={`/document/${d.id}`}
                  className="flex items-center gap-2 py-1.5 hover:bg-accent/5"
                >
                  <Icon name="description" size={14} className="text-muted" />
                  <span className="min-w-0 flex-1 truncate">{d.title}</span>
                  {d.case_id && (
                    <span className="font-mono text-[10.5px] text-muted">{d.case_id}</span>
                  )}
                </Link>
              </li>
            ))}
          </Section>
          <Section title="Cases" count={r.cases.length}>
            {r.cases.map((c) => (
              <li key={c.id}>
                <Link
                  to={`/cases/${c.id}`}
                  className="flex items-center gap-2 py-1.5 hover:bg-accent/5"
                >
                  <Icon name="folder" size={14} className="text-muted" />
                  <span className="font-mono text-[10.5px] text-tealink">{c.id}</span>
                  <span className="min-w-0 flex-1 truncate">{c.title}</span>
                  <Badge>{c.status.replace('_', ' ')}</Badge>
                </Link>
              </li>
            ))}
          </Section>
          <Section title="Contacts" count={r.contacts.length}>
            {r.contacts.map((c) => (
              <li key={c.name}>
                <Link
                  to={`/contacts?name=${encodeURIComponent(c.name)}`}
                  className="flex items-center gap-2 py-1.5 hover:bg-accent/5"
                >
                  <Icon name="person" size={14} className="text-muted" />
                  <span className="truncate">{c.name}</span>
                </Link>
              </li>
            ))}
          </Section>
        </>
      )}
    </div>
  )
}

function Section({
  title,
  count,
  children,
}: {
  title: string
  count: number
  children: React.ReactNode
}) {
  if (count === 0) return null
  return (
    <section className="mb-4">
      <h2 className="mb-1 text-[10px] font-bold tracking-[.12em] text-muted uppercase">
        {title} <span className="font-mono normal-case">{count}</span>
      </h2>
      <ul className="divide-y divide-line2 rounded-xl border border-line bg-card px-3">
        {children}
      </ul>
    </section>
  )
}
