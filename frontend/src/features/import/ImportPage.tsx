import { useEffect, useRef, useState } from 'react'

import {
  useClearGmailCache,
  useGmailGroups,
  useGmailImportStatus,
  useGmailIndexStatus,
  useRefreshGmailIndex,
  useStartGmailImport,
} from '../../api/gmailImport'
import type { Schemas } from '../../api/client'
import { useGmail } from '../../api/settings'
import { formatIsoDate, pluralize } from '../../format'
import { Alert } from '../../ui/Alert'
import { Button, buttonClass } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Icon } from '../../ui/Icon'
import { Menu } from '../../ui/Menu'
import { Progress } from '../../ui/Progress'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'
import { CreateCaseModal } from '../cases/CreateCaseModal'
import { GmailImportStatus } from './GmailImportStatus'
import { HistoryTable } from './HistoryTable'
import { HistoryToolbar } from './HistoryToolbar'
import {
  type Filter,
  type Group,
  type Sort,
  filterGroups,
  isDone,
  sectionGroups,
  sortSections,
  timeAxis,
} from './historyModel'
import { NewMailBanner } from './NewMailBanner'

/** GmailImportRequest.gmail_ids accepts at most this many ids. */
const MAX_SELECTED = 500

function formatSize(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Index the lawyer's mailbox, browse it by case reference, import the history oldest first. */
export function ImportPage() {
  const gmailQuery = useGmail()
  const indexQuery = useGmailIndexStatus()
  const groupsQuery = useGmailGroups()
  const importQuery = useGmailImportStatus()
  const refresh = useRefreshGmailIndex()
  const start = useStartGmailImport()
  const clearCache = useClearGmailCache()
  const toast = useToast()

  const [expanded, setExpanded] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // Raw text, so clearing the field to retype doesn't fight a clamp on every keystroke.
  const [oldestInput, setOldestInput] = useState('25')
  const [confirmClear, setConfirmClear] = useState(false)
  const [creatingFor, setCreatingFor] = useState<Group | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<Sort>('oldest')
  const [moreOpen, setMoreOpen] = useState(false)
  const moreRef = useRef<HTMLButtonElement>(null)

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
  const busy = importing || start.isPending
  const ready = gmail.connected && (gmail.allowlist.length > 0 || gmail.label_filter.trim() !== '')
  const oldestN = Math.min(100, Math.max(1, parseInt(oldestInput, 10) || 1))
  const counts: Record<Filter, number> = {
    all: groups.length,
    case: groups.filter((g) => g.matched_case_id).length,
    nocase: groups.filter((g) => !g.matched_case_id).length,
    pending: groups.filter((g) => !isDone(g)).length,
    done: groups.filter(isDone).length,
  }
  const visible = filterGroups(groups, { query, filter })
  const sections = sortSections(sectionGroups(visible), sort)
  const axis = timeAxis(groups)

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

  function setMany(gmailIds: string[], on: boolean) {
    const next = new Set(selected)
    if (!on) {
      for (const id of gmailIds) next.delete(id)
    } else {
      // Rows arrive oldest first, so a capped selection keeps the oldest.
      for (const id of gmailIds) {
        if (next.size >= MAX_SELECTED && !next.has(id)) {
          toast(`Selection capped at ${MAX_SELECTED} messages — import these first`)
          break
        }
        next.add(id)
      }
    }
    setSelected(next)
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
    <div className="text-[12px]">
      <header className="mb-4 space-y-2">
        <div className="flex items-center gap-3">
          <h1 className="font-display text-[22px] font-extrabold">Import history</h1>
          <div className="ml-auto flex items-center gap-2">
            <Button
              variant="secondary"
              disabled={!ready || index.running || refresh.isPending}
              onClick={() =>
                refresh.mutate(undefined, { onError: (err) => toast(err.message, 'error') })
              }
            >
              <Icon name="sync" size={16} /> {index.running ? 'Indexing…' : 'Refresh index'}
            </Button>
            {index.cached_count > 0 && (
              <>
                <button
                  ref={moreRef}
                  type="button"
                  aria-label="More actions"
                  aria-haspopup="menu"
                  aria-expanded={moreOpen}
                  className={buttonClass('secondary', '', 'icon')}
                  onClick={() => setMoreOpen((v) => !v)}
                >
                  <Icon name="more_horiz" size={16} />
                </button>
                <Menu
                  open={moreOpen}
                  onClose={() => setMoreOpen(false)}
                  anchorRef={moreRef}
                  label="More actions"
                >
                  <button
                    type="button"
                    role="menuitem"
                    disabled={importing || clearCache.isPending}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] hover:bg-panel2 disabled:opacity-50"
                    onClick={() => {
                      setMoreOpen(false)
                      setConfirmClear(true)
                    }}
                  >
                    <Icon name="delete_sweep" size={16} /> Clear cache
                  </button>
                </Menu>
              </>
            )}
          </div>
        </div>
        <p className="text-muted">
          {index.indexed_count} messages indexed
          {index.last_indexed_at && ` · refreshed ${formatIsoDate(index.last_indexed_at)}`}
          {index.cached_count > 0 &&
            ` · ${index.cached_count} cached (${formatSize(index.cached_bytes)})`}
        </p>
        {index.indexed_count > 0 && (
          <div className="space-y-1">
            <Progress value={imported} max={total} label="History imported" />
            <p className="font-mono text-muted">
              {imported} of {total} imported · {total - imported} pending
            </p>
          </div>
        )}
      </header>

      {!ready && (
        <Alert>
          {gmail.connected
            ? 'Set a sender allowlist or a label on the Gmail page before indexing.'
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

      {run && run.total > 0 && <GmailImportStatus run={run} variant="full" />}

      <NewMailBanner busy={busy} />

      {index.indexed_count > 0 && (
        <>
          <HistoryToolbar
            query={query}
            onQuery={setQuery}
            filter={filter}
            onFilter={setFilter}
            counts={counts}
            sort={sort}
            onSort={setSort}
            oldestInput={oldestInput}
            onOldestInput={setOldestInput}
            onOldestBlur={() => setOldestInput(String(oldestN))}
            busy={busy}
            onImport={() => runImport({ oldest_n: oldestN, group: null, new: false })}
          />

          {visible.length === 0 ? (
            <div className="rounded-xl border border-line bg-card px-4 py-6 text-center text-muted">
              <p>No references match.</p>
              <Button
                className="mt-2"
                size="sm"
                variant="secondary"
                onClick={() => {
                  setQuery('')
                  setFilter('all')
                }}
              >
                Clear filters
              </Button>
            </div>
          ) : (
            <HistoryTable
              sections={sections}
              axis={axis}
              expanded={expanded}
              selected={selected}
              busy={busy}
              oldestN={oldestN}
              onToggleGroup={toggleGroup}
              onToggleMessage={toggleMessage}
              onSetMany={setMany}
              onImportGroup={(g) => runImport({ oldest_n: oldestN, group: g.key, new: false })}
              onCreateCase={setCreatingFor}
            />
          )}
        </>
      )}

      {ready && index.indexed_count === 0 && !index.running && (
        <p className="rounded-xl border border-line bg-card px-4 py-6 text-center text-muted">
          Nothing indexed yet. Refresh the index to read the headers of your lawyer&apos;s mail —
          nothing is imported until you choose.
        </p>
      )}

      {selected.size > 0 && (
        <div
          role="region"
          aria-label="Selection"
          className="sticky bottom-4 z-20 mt-4 flex items-center gap-3 rounded-xl border border-accent/30 bg-card px-4 py-2 shadow-[0_8px_24px_rgba(0,0,0,.4)]"
        >
          <span className="font-semibold">{selected.size} selected</span>
          <Button
            className="ml-auto"
            disabled={busy}
            onClick={() => runImport({ gmail_ids: [...selected], new: false })}
          >
            Import selected ({selected.size})
          </Button>
          <Button variant="secondary" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
        </div>
      )}

      {creatingFor && (
        <CreateCaseModal
          open
          onClose={() => setCreatingFor(null)}
          defaults={
            creatingFor.kind === 'internal_id'
              ? { caseId: creatingFor.key, title: creatingFor.topic ?? undefined }
              : { azCourt: creatingFor.key, title: creatingFor.topic ?? undefined }
          }
          onCreated={(id) => {
            setCreatingFor(null)
            groupsQuery.refetch()
            toast(`Created case ${id}`)
          }}
        />
      )}

      <ConfirmDialog
        open={confirmClear}
        onClose={() => setConfirmClear(false)}
        onConfirm={() => {
          setConfirmClear(false)
          clearCache.mutate(undefined, {
            onSuccess: () => toast('Local mail cache cleared'),
            onError: (err) => toast(err.message, 'error'),
          })
        }}
        title="Clear the local mail cache?"
        body="Deletes the raw copies of fetched emails kept on this machine. Nothing in Gmail and no already-imported bundle is touched; importing these messages again will fetch them from Gmail."
        label="Clear cache"
        danger
        pending={clearCache.isPending}
      />
    </div>
  )
}
