import { Fragment, useRef, useState } from 'react'
import { Link } from 'react-router'

import { useGmailMessages } from '../../api/gmailImport'
import { formatIsoDate, formatMonthLabel, formatMonthYear } from '../../time'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import {
  type CaseBucket,
  type Group,
  type Sections,
  type TimeAxis,
  UNREFERENCED,
  groupTotals,
  isDone,
  referenceLabel,
  splitTopic,
} from './historyModel'
import { MessageRow } from './MessageRow'
import { TimelineAxis, TimelineBar } from './TimelineBar'

const COLUMNS =
  'grid-cols-[minmax(0,1fr)_120px_96px_150px] md:grid-cols-[minmax(0,1fr)_120px_minmax(140px,240px)_96px_150px]'

function ProgressCell({ ingested, count }: { ingested: number; count: number }) {
  if (ingested >= count) {
    return (
      <span className="flex items-center justify-end gap-1 font-mono text-success">
        <Icon name="check_circle" size={13} filled /> {count}
      </span>
    )
  }
  const pct = count > 0 ? Math.round((ingested / count) * 100) : 0
  return (
    <span className="flex items-center justify-end gap-2 font-mono">
      <span className="h-1 w-10 overflow-hidden rounded-full bg-line2" aria-hidden="true">
        <span className="block h-full bg-accent" style={{ width: `${pct}%` }} />
      </span>
      <span className={ingested === 0 ? 'text-muted' : ''}>
        {ingested}/{count}
      </span>
    </span>
  )
}

function RangeCell({ from, to }: { from: string; to: string }) {
  return (
    <span
      className="text-right font-mono text-[11px] text-muted"
      title={`${formatIsoDate(from)} – ${formatIsoDate(to)}`}
    >
      {formatMonthYear(from)}–{formatMonthYear(to)}
    </span>
  )
}

function Divider({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center gap-3 border-b border-line2 bg-panel2/30 px-4 py-1.5 text-[10px] font-extrabold tracking-[.12em] text-muted uppercase">
      {label}
      <span className="font-mono font-normal tracking-normal">{count}</span>
    </div>
  )
}

function GroupMessages({
  group,
  selected,
  onToggle,
  onSetMany,
}: {
  group: Group
  selected: Set<string>
  onToggle: (gmailId: string) => void
  /** Select or clear several rows at once; the page enforces the import cap. */
  onSetMany: (gmailIds: string[], on: boolean) => void
}) {
  const query = useGmailMessages(group.key, true)
  const lastClicked = useRef<string | null>(null)
  if (!query.data) return <QueryState error={query.error} pending={query.isPending} />
  const items = query.data.pages.flatMap((page) => page.items)
  // Rows already ingested can't be picked again.
  const pickable = items.filter((m) => !m.ingested).map((m) => m.gmail_id)
  const pickedCount = pickable.filter((id) => selected.has(id)).length
  const allPicked = pickable.length > 0 && pickedCount === pickable.length
  const withAttachments = items.filter((m) => m.has_attachments).length

  function onRowToggle(gmailId: string, range: boolean) {
    const anchor = lastClicked.current
    lastClicked.current = gmailId
    const from = anchor ? pickable.indexOf(anchor) : -1
    const to = pickable.indexOf(gmailId)
    if (range && from >= 0 && to >= 0) {
      // The clicked row's new state applies to the whole swept range.
      const ids = pickable.slice(Math.min(from, to), Math.max(from, to) + 1)
      onSetMany(ids, !selected.has(gmailId))
    } else {
      onToggle(gmailId)
    }
  }

  return (
    <div className="border-t border-line2 bg-panel2/40">
      <div className="flex items-center gap-3 px-4 py-1.5 text-muted">
        {pickable.length > 0 && (
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={allPicked}
              ref={(el) => {
                if (el) el.indeterminate = pickedCount > 0 && !allPicked
              }}
              onChange={() => onSetMany(pickable, !allPicked)}
            />
            Select all {pickable.length}
            {query.hasNextPage && ' loaded'}
          </label>
        )}
        <span className="ml-auto font-mono text-[11px]">
          {formatIsoDate(group.first_at)} – {formatIsoDate(group.last_at)} · {group.count}{' '}
          {group.count === 1 ? 'message' : 'messages'}
          {withAttachments > 0 && ` · ${withAttachments} with attachments`}
        </span>
      </div>
      <ul>
        {items.map((m, i) => {
          const label = formatMonthLabel(m.sent_at)
          const prev = items[i - 1]
          const divider = !prev || formatMonthLabel(prev.sent_at) !== label
          return (
            <Fragment key={m.gmail_id}>
              {divider && (
                <li className="px-4 pt-2 pb-0.5 text-[10px] font-extrabold tracking-[.12em] text-muted uppercase">
                  {label}
                </li>
              )}
              <MessageRow message={m} selected={selected.has(m.gmail_id)} onToggle={onRowToggle} />
            </Fragment>
          )
        })}
      </ul>
      {query.hasNextPage && (
        <div className="px-4 py-2">
          <Button
            size="sm"
            variant="secondary"
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

type Handlers = {
  axis: TimeAxis
  expanded: string | null
  selected: Set<string>
  /** Import is blocked while a run is active or a request is in flight. */
  busy: boolean
  oldestN: number
  onToggleGroup: (key: string) => void
  onToggleMessage: (gmailId: string) => void
  onSetMany: (gmailIds: string[], on: boolean) => void
  onImportGroup: (group: Group) => void
  onCreateCase: (group: Group) => void
}

function RefRow({ g, nested, h }: { g: Group; nested: boolean; h: Handlers }) {
  const open = h.expanded === g.key
  const noCase = !g.matched_case_id && g.key !== UNREFERENCED
  const { parties, matter } = splitTopic(g.topic)
  const done = isDone(g)
  return (
    <div className={`border-b border-line2 ${nested ? 'ml-6 border-l border-l-line' : ''}`}>
      <div
        className={`group grid ${COLUMNS} items-center gap-3 px-4 py-2 hover:bg-panel2/40 ${done ? 'opacity-70' : ''}`}
      >
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            aria-expanded={open}
            aria-label={`${open ? 'Collapse' : 'Expand'} ${referenceLabel(g)}`}
            onClick={() => h.onToggleGroup(g.key)}
            className="flex shrink-0 items-center gap-1 font-mono font-semibold"
          >
            <Icon name={open ? 'expand_more' : 'chevron_right'} size={16} />
            {referenceLabel(g)}
          </button>
          {g.kind && (
            <span title={g.kind === 'internal_id' ? 'File no.' : 'Court'} className="text-muted">
              <Icon name={g.kind === 'internal_id' ? 'tag' : 'gavel'} size={13} />
              <span className="sr-only">{g.kind === 'internal_id' ? 'File no.' : 'Court'}</span>
            </span>
          )}
          {(matter || parties) && (
            <span className="min-w-0 truncate" title={g.topic ?? undefined}>
              {matter}
              {parties && matter && <span className="text-muted"> · {parties}</span>}
              {parties && !matter && <span className="text-muted">{parties}</span>}
            </span>
          )}
          {noCase && !g.topic && <span className="text-muted">no case yet</span>}
        </div>
        <ProgressCell ingested={g.ingested_count} count={g.count} />
        <div className="hidden md:block">
          <TimelineBar
            axis={h.axis}
            from={g.first_at}
            to={g.last_at}
            tone={done ? 'success' : 'muted'}
          />
        </div>
        <RangeCell from={g.first_at} to={g.last_at} />
        <div className="flex justify-end gap-1.5 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
          {noCase && (
            <Button
              size="sm"
              variant="secondary"
              aria-label={`Create case for ${referenceLabel(g)}`}
              onClick={() => h.onCreateCase(g)}
            >
              Create case
            </Button>
          )}
          {!done && (
            <Button
              size="sm"
              variant="secondary"
              disabled={h.busy}
              aria-label={`Import oldest ${h.oldestN} from ${referenceLabel(g)}`}
              onClick={() => h.onImportGroup(g)}
            >
              Import {h.oldestN}
            </Button>
          )}
        </div>
      </div>
      {open && (
        <>
          {noCase && (
            <p className="px-4 py-2 text-muted">
              No case for {referenceLabel(g)} yet — create it first and its mail is filed into it
              automatically on import.
            </p>
          )}
          <GroupMessages
            group={g}
            selected={h.selected}
            onToggle={h.onToggleMessage}
            onSetMany={h.onSetMany}
          />
        </>
      )}
    </div>
  )
}

function CaseBlock({ bucket, h }: { bucket: CaseBucket; h: Handlers }) {
  const [collapsed, setCollapsed] = useState(false)
  const totals = groupTotals(bucket.groups)
  const done = totals.ingested >= totals.count
  return (
    <div>
      <div
        className={`grid ${COLUMNS} items-center gap-3 border-b border-line2 bg-accent/5 px-4 py-2`}
      >
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            aria-expanded={!collapsed}
            aria-label={`${collapsed ? 'Expand' : 'Collapse'} case ${bucket.id}`}
            onClick={() => setCollapsed((v) => !v)}
            className="text-muted"
          >
            <Icon name={collapsed ? 'chevron_right' : 'expand_more'} size={16} />
          </button>
          <Link to={`/cases/${bucket.id}`} className="flex min-w-0 items-center gap-2">
            <Badge tone="accent" mono>
              {bucket.id}
            </Badge>
            <span className="truncate font-semibold">{bucket.title}</span>
          </Link>
          {collapsed && (
            <span className="shrink-0 text-muted">
              {bucket.groups.length} {bucket.groups.length === 1 ? 'reference' : 'references'}
            </span>
          )}
        </div>
        <ProgressCell ingested={totals.ingested} count={totals.count} />
        <div className="hidden md:block">
          <TimelineBar
            axis={h.axis}
            from={totals.first}
            to={totals.last}
            tone={done ? 'success' : 'accent'}
          />
        </div>
        <RangeCell from={totals.first} to={totals.last} />
        <span />
      </div>
      {!collapsed && bucket.groups.map((g) => <RefRow key={g.key} g={g} nested h={h} />)}
    </div>
  )
}

/** Case buckets, then references without a case, then mail without a reference. */
export function HistoryTable({ sections, ...h }: { sections: Sections } & Handlers) {
  return (
    <div className="overflow-clip rounded-xl border border-line bg-card">
      <div
        className={`sticky top-0 z-10 grid ${COLUMNS} items-end gap-3 border-b border-line2 bg-card px-4 py-2 text-[10px] font-extrabold tracking-[.12em] text-muted uppercase`}
      >
        <span>Reference / matter</span>
        <span className="text-right">Imported</span>
        <div className="hidden md:block">
          <TimelineAxis axis={h.axis} />
        </div>
        <span className="text-right">Range</span>
        <span />
      </div>
      {sections.cases.length > 0 && <Divider label="In a case" count={sections.cases.length} />}
      {sections.cases.map((bucket) => (
        <CaseBlock key={bucket.id} bucket={bucket} h={h} />
      ))}
      {sections.noCase.length > 0 && <Divider label="No case yet" count={sections.noCase.length} />}
      {sections.noCase.map((g) => (
        <RefRow key={g.key} g={g} nested={false} h={h} />
      ))}
      {sections.unreferenced && (
        <>
          <Divider label="Without a reference" count={sections.unreferenced.count} />
          <RefRow g={sections.unreferenced} nested={false} h={h} />
        </>
      )}
    </div>
  )
}
