import { Fragment, useEffect, useState } from 'react'
import { Link } from 'react-router'

import {
  useCancelGmailImport,
  useGmailGroups,
  useGmailImportStatus,
  useGmailIndexStatus,
  useGmailMessages,
  useRefreshGmailIndex,
  useStartGmailImport,
} from '../../api/gmailImport'
import type { Schemas } from '../../api/client'
import { useGmail } from '../../api/settings'
import { formatIsoDate, pluralize } from '../../format'
import { Alert } from '../../ui/Alert'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

type Group = Schemas['GmailGroup']

/** "8372-25" is stored as the case id; the lawyer's file number reads "8372/25". */
function referenceLabel(group: Group) {
  if (group.key === 'unreferenced') return 'No reference'
  return group.kind === 'internal_id' ? group.key.replace('-', '/') : group.key
}

function Progress({ value, max, label }: { value: number; max: number; label: string }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={max}
      className="h-1.5 w-full overflow-hidden rounded-full bg-line2"
    >
      <div className="h-full bg-accent transition-[width]" style={{ width: `${pct}%` }} />
    </div>
  )
}

function GroupMessages({
  group,
  selected,
  onToggle,
}: {
  group: Group
  selected: Set<string>
  onToggle: (gmailId: string) => void
}) {
  const query = useGmailMessages(group.key, true)
  if (!query.data) return <QueryState error={query.error} pending={query.isPending} />
  const items = query.data.pages.flatMap((page) => page.items)
  return (
    <div className="border-t border-line2 bg-panel2/40">
      <ul>
        {items.map((m) => (
          <li
            key={m.gmail_id}
            className="grid grid-cols-[24px_88px_200px_1fr_20px_20px] items-center gap-2 px-4 py-1.5"
          >
            <input
              type="checkbox"
              aria-label={`Select ${m.subject ?? m.gmail_id}`}
              checked={selected.has(m.gmail_id)}
              disabled={m.ingested}
              onChange={() => onToggle(m.gmail_id)}
            />
            <span className="font-mono text-[11px] text-muted">{formatIsoDate(m.sent_at)}</span>
            <span className="truncate text-muted">{m.sender}</span>
            <span className="truncate">{m.subject ?? '(no subject)'}</span>
            {m.has_attachments ? (
              <Icon name="attach_file" size={14} className="text-muted" />
            ) : (
              <span />
            )}
            {m.ingested ? (
              <Icon name="check_circle" size={14} filled className="text-success" />
            ) : (
              <span />
            )}
          </li>
        ))}
      </ul>
      {query.hasNextPage && (
        <div className="px-4 py-2">
          <Button
            variant="secondary"
            className="px-2.5 py-1 text-[11px]"
            disabled={query.isFetchingNextPage}
            onClick={() => query.fetchNextPage()}
          >
            Load more
          </Button>
        </div>
      )}
    </div>
  )
}

/** Index the lawyer's mailbox, browse it by case reference, import the history oldest first. */
export function ImportPage() {
  const gmailQuery = useGmail()
  const indexQuery = useGmailIndexStatus()
  const groupsQuery = useGmailGroups()
  const importQuery = useGmailImportStatus()
  const refresh = useRefreshGmailIndex()
  const start = useStartGmailImport()
  const cancel = useCancelGmailImport()
  const toast = useToast()

  const [expanded, setExpanded] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // Raw text, so clearing the field to retype doesn't fight a clamp on every keystroke.
  const [oldestInput, setOldestInput] = useState('25')
  const [sequential, setSequential] = useState(true)

  useEffect(() => {
    document.title = 'Import history | The Sanctuary'
  }, [])

  const gmail = gmailQuery.data
  const index = indexQuery.data
  const run = importQuery.data
  if (!gmail || !index) {
    return <QueryState error={gmailQuery.error ?? indexQuery.error} pending={true} />
  }

  const groups = groupsQuery.data?.groups ?? []
  const total = groups.reduce((sum, g) => sum + g.count, 0)
  const imported = groups.reduce((sum, g) => sum + g.ingested_count, 0)
  const importing = run?.active ?? false
  const ready = gmail.connected && gmail.allowlist.length > 0
  const scope = groups.find((g) => g.key === expanded)
  const oldestN = Math.min(100, Math.max(1, parseInt(oldestInput, 10) || 1))

  function toggleGroup(key: string) {
    setSelected(new Set())
    setExpanded((current) => (current === key ? null : key))
  }

  function toggleMessage(gmailId: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (!next.delete(gmailId)) next.add(gmailId)
      return next
    })
  }

  function runImport(body: Schemas['GmailImportRequest']) {
    start.mutate(body, {
      onSuccess: (r) => {
        setSelected(new Set())
        toast(
          r.queued === 0 ? 'Nothing left to import' : `Queued ${pluralize(r.queued, 'message')}`,
        )
      },
      onError: (err) => toast(err.message, 'error'),
    })
  }

  return (
    <div className="mx-auto max-w-[1100px] p-6 text-[12px]">
      <header className="mb-4 flex items-center gap-3">
        <div>
          <h1 className="font-display text-[22px] font-extrabold">Import history</h1>
          <p className="text-muted">
            {index.indexed_count} messages indexed
            {index.last_indexed_at && ` · refreshed ${formatIsoDate(index.last_indexed_at)}`}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Link
            to="/settings/gmail"
            className="text-[11px] text-muted underline-offset-2 hover:underline"
          >
            Gmail settings
          </Link>
          <Button
            variant="secondary"
            disabled={!ready || index.running || refresh.isPending}
            onClick={() =>
              refresh.mutate(undefined, { onError: (err) => toast(err.message, 'error') })
            }
          >
            <Icon name="sync" size={16} /> {index.running ? 'Indexing…' : 'Refresh index'}
          </Button>
        </div>
      </header>

      {!ready && (
        <Alert>
          {gmail.connected
            ? 'Add a sender to the allowlist in Gmail settings before indexing.'
            : 'Connect Gmail in Gmail settings first.'}
        </Alert>
      )}
      {index.error && (
        <Alert>
          {index.running ? 'Index refresh hit an error and is retrying' : 'Index refresh failed'} —{' '}
          {index.error}
        </Alert>
      )}
      {!index.running && index.skipped > 0 && (
        <p className="mb-4 text-warning">
          {pluralize(index.skipped, 'message')} couldn&apos;t be read from Gmail last time — refresh
          the index again to retry.
        </p>
      )}

      {index.running && (
        <div className="mb-4 space-y-1.5 rounded-xl border border-line bg-card px-4 py-3">
          <div className="text-muted">
            Reading message headers — {index.done} of {index.total}
          </div>
          <Progress value={index.done} max={index.total} label="Index progress" />
        </div>
      )}

      {run && run.total > 0 && (
        <div className="mb-4 space-y-1.5 rounded-xl border border-line bg-card px-4 py-3">
          <div className="flex items-center gap-3">
            <div className="min-w-0 flex-1 truncate">
              {importing ? (
                <>
                  Importing {run.done}/{run.total}
                  {run.current_subject && (
                    <span className="text-muted">
                      {run.waiting ? ' · waiting for “' : ' · “'}
                      {run.current_subject}
                      {run.waiting ? '” to finish processing' : '”'}
                    </span>
                  )}
                </>
              ) : (
                <>
                  {run.cancelled ? 'Stopped after' : 'Imported'} {run.done} of {run.total}
                  {run.failed_count > 0 && (
                    <span className="text-danger"> · {run.failed_count} failed</span>
                  )}
                </>
              )}
            </div>
            {importing && (
              <Button
                variant="secondary"
                className="px-2.5 py-1 text-[11px]"
                disabled={cancel.isPending}
                onClick={() =>
                  cancel.mutate(undefined, { onError: (err) => toast(err.message, 'error') })
                }
              >
                Stop
              </Button>
            )}
          </div>
          <Progress value={run.done} max={run.total} label="Import progress" />
          {run.error && <Alert>{run.error}</Alert>}
        </div>
      )}

      {index.indexed_count > 0 && (
        <>
          <section
            aria-label="Import controls"
            className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-line bg-card px-4 py-3"
          >
            <span>Import next</span>
            <input
              type="number"
              aria-label="How many"
              min={1}
              max={100}
              value={oldestInput}
              onChange={(e) => setOldestInput(e.target.value)}
              onBlur={() => setOldestInput(String(oldestN))}
              className="w-16 rounded-[9px] border border-line bg-panel2 px-2 py-1 text-[12px]"
            />
            <span>
              oldest from <strong>{scope ? referenceLabel(scope) : 'all references'}</strong>
            </span>
            <label className="ml-2 flex items-center gap-1.5 text-muted">
              <input
                type="checkbox"
                checked={sequential}
                onChange={(e) => setSequential(e.target.checked)}
              />
              Import strictly in order (slower)
            </label>
            <div className="ml-auto flex items-center gap-2">
              {selected.size > 0 && (
                <Button
                  variant="secondary"
                  disabled={importing || start.isPending}
                  onClick={() => runImport({ gmail_ids: [...selected], sequential })}
                >
                  Import selected ({selected.size})
                </Button>
              )}
              <Button
                disabled={importing || start.isPending}
                onClick={() =>
                  runImport({ oldest_n: oldestN, group: scope?.key ?? null, sequential })
                }
              >
                Import
              </Button>
            </div>
          </section>

          <div className="mb-2 space-y-1">
            <div className="flex justify-between text-muted">
              <span>History</span>
              <span className="font-mono">
                {imported} of {total} imported
              </span>
            </div>
            <Progress value={imported} max={total} label="History imported" />
          </div>

          <table className="w-full border-collapse rounded-xl border border-line bg-card">
            <thead>
              <tr className="border-b border-line2 text-left text-[10px] tracking-[.12em] text-muted uppercase">
                <th className="px-4 py-2 font-extrabold">Reference</th>
                <th className="px-2 py-2 font-extrabold">Case</th>
                <th className="px-2 py-2 text-right font-extrabold">Messages</th>
                <th className="px-2 py-2 text-right font-extrabold">Imported</th>
                <th className="px-4 py-2 text-right font-extrabold">Range</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => {
                const open = expanded === g.key
                return (
                  <Fragment key={g.key}>
                    <tr className="border-b border-line2 hover:bg-panel2/40">
                      <td className="px-4 py-2">
                        <button
                          type="button"
                          aria-expanded={open}
                          aria-label={`${open ? 'Collapse' : 'Expand'} ${referenceLabel(g)}`}
                          onClick={() => toggleGroup(g.key)}
                          className="flex items-center gap-1.5 font-mono font-semibold"
                        >
                          <Icon name={open ? 'expand_more' : 'chevron_right'} size={16} />
                          {referenceLabel(g)}
                          {g.kind && (
                            <Badge className="ml-1">
                              {g.kind === 'internal_id' ? 'file no.' : 'court'}
                            </Badge>
                          )}
                        </button>
                      </td>
                      <td className="px-2 py-2">
                        {g.matched_case_id ? (
                          <Link to={`/cases/${g.matched_case_id}`}>
                            <Badge tone="accent" mono>
                              {g.matched_case_id}
                            </Badge>
                          </Link>
                        ) : g.key === 'unreferenced' ? (
                          <span className="text-muted">—</span>
                        ) : (
                          <span className="text-muted">no case yet</span>
                        )}
                      </td>
                      <td className="px-2 py-2 text-right font-mono">{g.count}</td>
                      <td className="px-2 py-2 text-right font-mono">
                        {g.ingested_count}/{g.count}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-muted">
                        {formatIsoDate(g.first_at)} – {formatIsoDate(g.last_at)}
                      </td>
                    </tr>
                    {open && (
                      <tr>
                        <td colSpan={5} className="p-0">
                          {!g.matched_case_id && g.key !== 'unreferenced' && (
                            <p className="px-4 py-2 text-muted">
                              No case for {referenceLabel(g)} yet — create it first and its mail is
                              filed into it automatically on import.
                            </p>
                          )}
                          <GroupMessages group={g} selected={selected} onToggle={toggleMessage} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </>
      )}

      {ready && index.indexed_count === 0 && !index.running && (
        <p className="rounded-xl border border-line bg-card px-4 py-6 text-center text-muted">
          Nothing indexed yet. Refresh the index to read the headers of your lawyer&apos;s mail —
          nothing is imported until you choose.
        </p>
      )}
    </div>
  )
}
