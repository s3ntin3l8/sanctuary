import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { bundleOpenParts } from '../documents/reviewReasons'
import { useOpenDocument } from '../documents/useOpenDocument'

import type { Schemas } from '../../api/client'
import {
  type TriageBundle,
  type TriageFilters,
  filtersFromParams,
  paramsWithFilters,
  useBatchRemove,
  useBundleAction,
  useDocumentAction,
  useBatchRetry,
  useRetryBundle,
  useTriage,
} from '../../api/triage'
import { pluralize } from '../../format'
import { formatShortDate } from '../../time'
import { Badge } from '../../ui/Badge'
import { Button, buttonClass } from '../../ui/Button'
import { Chip } from '../../ui/Chip'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Icon } from '../../ui/Icon'
import { Menu } from '../../ui/Menu'
import { GmailImportStatus, useGmailRunBanner } from '../import/GmailImportStatus'
import { NewMailNotice, useNewMailToReview } from '../import/NewMailBanner'
import { BundleStateBar } from '../../ui/PipelineBar'
import { Progress } from '../../ui/Progress'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'
import {
  DocumentReview,
  KeepWholeButton,
  ORIGINATOR_COLOR,
  SplitButton,
} from '../documents/DocumentReview'
import { BundleTree } from './BundleTree'
import { ConfirmBundleModal, type ConfirmTarget } from './ConfirmBundleModal'
import { IngestModal } from './IngestModal'
import { CONFIDENCE_TONE } from './RoutingPickers'

type Status = TriageBundle['status'] | 'all'
const STATUS_LABEL: Record<Status, string> = {
  all: 'All',
  needs_classification: 'Needs classification',
  needs_review: 'Needs review',
  stuck: 'Stuck',
  processing: 'Processing',
}
// A filter list longer than this gets a search box.
const SEARCH_FROM = 8
const GRID = 'grid grid-cols-[24px_128px_198px_1fr_184px_170px_96px] items-center gap-3'

export function TriagePage() {
  const [params, setParams] = useSearchParams()
  const filters = useMemo(() => filtersFromParams(params), [params])
  const updateFilters = (change: (f: TriageFilters) => TriageFilters) =>
    setParams((p) => paramsWithFilters(p, change(filtersFromParams(p))), { replace: true })
  const [status, setStatus] = useState<Status>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // In the URL so coming back from the HUD reopens the same bundle.
  const expanded = params.get('bundle')
  const toggleExpanded = (key: string) =>
    setParams(
      (p) => {
        const next = new URLSearchParams(p)
        next.delete('doc')
        if (next.get('bundle') === key) next.delete('bundle')
        else next.set('bundle', key)
        return next
      },
      { replace: true },
    )
  const [confirmTarget, setConfirmTarget] = useState<ConfirmTarget | null>(null)
  const [retryTarget, setRetryTarget] = useState<TriageBundle | null>(null)
  const [bulkRetryOpen, setBulkRetryOpen] = useState(false)
  const [bulkDanger, setBulkDanger] = useState<'dismiss' | 'delete' | null>(null)
  const batchRemove = useBatchRemove()
  const query = useTriage(filters)
  const batchRetry = useBatchRetry()
  const retry = useRetryBundle()
  const toast = useToast()

  useEffect(() => {
    document.title = 'Triage | The Sanctuary'
  }, [])
  const uploadOpen = params.get('upload') === '1'
  const uploadCase = params.get('case_id')

  const bundles = useMemo(() => query.data?.bundles ?? [], [query.data])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.key !== 'Enter' || !expanded) return
      // Not while typing, and not on top of another dialog (Edit metadata, the confirm modal).
      const el = e.target instanceof HTMLElement ? e.target : null
      if (
        el?.closest(
          'input:not([type="checkbox"]):not([type="radio"]), textarea, select, [contenteditable="true"]',
        )
      )
        return
      if (document.querySelector('[role="dialog"]')) return
      const b = bundles.find((x) => x.key === expanded)
      if (!b || b.status === 'processing' || b.status === 'stuck') return
      e.preventDefault()
      setConfirmTarget({
        mode: 'single',
        bundle: b,
        action: b.suggestion || b.confirmed_case_id ? 'confirm_bundle' : 'assign_case',
      })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded, bundles])
  // The expanded bundle leaves the feed once it is filed (or a stale link points nowhere).
  const feedLoaded = query.data !== undefined
  const expandedGone = feedLoaded && expanded !== null && !bundles.some((b) => b.key === expanded)
  useEffect(() => {
    if (!expandedGone) return
    setParams(
      (p) => {
        const next = new URLSearchParams(p)
        next.delete('bundle')
        next.delete('doc')
        return next
      },
      { replace: true },
    )
  }, [expandedGone, setParams])
  // Selected bundles that are still in the feed (a filed one drops out by itself).
  const picked = useMemo(() => bundles.filter((b) => selected.has(b.key)), [bundles, selected])
  // A stuck bundle can be retried, archived or deleted, but not confirmed.
  const confirmablePicked = picked.filter((b) => b.status !== 'stuck')
  const visible = bundles.filter((b) => status === 'all' || b.status === status)
  // Scans waiting for a split decision are not bundles yet; they sit above the inbox.
  const slicingRows = status === 'all' ? (query.data?.slicing_queue ?? []) : []
  const rowVisible = visible.some((b) => b.key === expanded)
  // Deep links (Home → ?bundle=) land on the bundle once the feed has loaded.
  const scrolledTo = useRef<string | null>(null)
  useEffect(() => {
    if (!expanded) {
      scrolledTo.current = null
      return
    }
    if (scrolledTo.current === expanded) return
    const row = document.querySelector(`[data-bundle-key="${CSS.escape(expanded)}"]`)
    if (!row) return
    scrolledTo.current = expanded
    row.scrollIntoView({ block: 'nearest' })
  }, [expanded, rowVisible])
  const counts = useMemo(() => {
    const c: Record<Status, number> = {
      all: bundles.length,
      needs_classification: 0,
      needs_review: 0,
      stuck: 0,
      processing: 0,
    }
    bundles.forEach((b) => {
      c[b.status] += 1
    })
    return c
  }, [bundles])
  const selectable = visible.filter((b) => b.status !== 'processing')
  const allSelected = selectable.length > 0 && selectable.every((b) => selected.has(b.key))

  function toggleSort(sort: TriageFilters['sort']) {
    updateFilters((f) => ({
      ...f,
      sort,
      dir: f.sort === sort && f.dir === 'desc' ? 'asc' : 'desc',
    }))
  }
  function toggleFilter(key: 'case_id' | 'proceeding_id' | 'pipeline_filter', value: string) {
    updateFilters((f) => ({
      ...f,
      [key]: f[key].includes(value) ? f[key].filter((v) => v !== value) : [...f[key], value],
    }))
  }

  const filterOptions = query.data?.filter_options
  const activeFilters = (
    [
      ['case_id', filterOptions?.cases],
      ['proceeding_id', filterOptions?.proceedings],
      ['pipeline_filter', filterOptions?.pipeline],
    ] as const
  ).flatMap(([key, options]) =>
    filters[key].map((value) => ({
      key,
      value,
      label: options?.find((o) => o.value === value)?.label ?? value,
    })),
  )

  const stats = query.data?.stats
  const gmailRun = useGmailRunBanner()
  const newMail = useNewMailToReview()
  return (
    <div className="flex min-h-full flex-col">
      <header className="flex flex-wrap items-center gap-4 border-b border-line bg-panel px-6 py-4">
        <div>
          <h1 className="font-display text-[22px] font-extrabold tracking-tight">Triage</h1>
          <p className="font-mono text-[11px] text-muted">
            {stats ? `${stats.pending} pending` : ' '}
            {stats && stats.failed_docs > 0 && (
              <span className="text-danger"> · {stats.failed_docs} failed</span>
            )}
            {stats && stats.drafts_pending > 0 && (
              <span className="text-warning"> · {stats.drafts_pending} draft cases</span>
            )}
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Link to="/settings/gmail/import" className={buttonClass('secondary')}>
            <Icon name="mail" size={16} /> Import from Gmail
            {newMail && (
              <Badge tone="accent" mono>
                {newMail.count}
              </Badge>
            )}
          </Link>
          <Button onClick={() => setParams({ upload: '1' })}>
            <Icon name="upload_file" size={16} /> Ingest
          </Button>
        </div>
      </header>

      {(gmailRun.run || newMail) && (
        <div className="px-6 pt-3">
          {gmailRun.run ? (
            <GmailImportStatus run={gmailRun.run} variant="compact" onDismiss={gmailRun.dismiss} />
          ) : (
            <NewMailNotice />
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 px-6 py-3">
        {(Object.keys(STATUS_LABEL) as Status[]).map((s) => (
          <Chip key={s} active={status === s} count={counts[s]} onClick={() => setStatus(s)}>
            {STATUS_LABEL[s]}
          </Chip>
        ))}
      </div>

      {activeFilters.length > 0 && (
        <div
          role="group"
          aria-label="Active filters"
          className="mx-6 mb-2 flex flex-wrap items-center gap-1.5 text-[11px]"
        >
          <span className="text-muted">Filtered by</span>
          {activeFilters.map((f) => (
            <button
              key={`${f.key}:${f.value}`}
              type="button"
              aria-label={`Remove filter ${f.label}`}
              onClick={() => toggleFilter(f.key, f.value)}
              className="inline-flex items-center gap-1 rounded-full border border-accent/30 bg-accent/12 px-2.5 py-0.5 font-mono text-[11px] text-ink hover:bg-accent/20"
            >
              {f.label}
              <Icon name="close" size={12} className="text-muted" />
            </button>
          ))}
          <button
            type="button"
            onClick={() =>
              updateFilters((f) => ({
                ...f,
                case_id: [],
                proceeding_id: [],
                pipeline_filter: [],
              }))
            }
            className="ml-1 text-muted hover:underline"
          >
            Clear filters
          </button>
        </div>
      )}

      {picked.length > 0 && (
        <div className="mx-6 mb-2 flex flex-wrap items-center gap-2 rounded-xl border border-accent/30 bg-accent/8 px-3 py-2 text-[12px]">
          <span className="font-semibold">{picked.length} selected</span>
          {confirmablePicked.length > 0 && (
            <Button
              size="sm"
              onClick={() =>
                setConfirmTarget({
                  mode: 'batch_confirm',
                  bundles: confirmablePicked,
                })
              }
            >
              Confirm ({confirmablePicked.length})
            </Button>
          )}
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setConfirmTarget({ mode: 'batch', keys: picked.map((b) => b.key) })}
          >
            Assign to…
          </Button>
          <Button size="sm" variant="secondary" onClick={() => setBulkRetryOpen(true)}>
            Retry ({picked.length})
          </Button>
          <Button size="sm" variant="secondary" onClick={() => setBulkDanger('dismiss')}>
            Archive ({picked.length})
          </Button>
          <Button size="sm" variant="danger" onClick={() => setBulkDanger('delete')}>
            Delete ({picked.length})
          </Button>
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className="ml-auto text-[11px] text-muted hover:underline"
          >
            Clear
          </button>
        </div>
      )}

      <div className="px-6 pb-6">
        <div
          role="region"
          aria-label="Triage table"
          tabIndex={0}
          className="overflow-x-auto rounded-lg"
        >
          <div className="min-w-[1200px]">
            <div
              className={`${GRID} rounded-t-lg border border-line bg-panel2 px-3 py-2 text-[9.5px] font-extrabold tracking-[.1em] text-muted uppercase`}
            >
              <input
                type="checkbox"
                aria-label="Select all visible"
                checked={allSelected}
                onChange={(e) =>
                  setSelected(e.target.checked ? new Set(selectable.map((b) => b.key)) : new Set())
                }
                className="h-3.5 w-3.5 accent-accent"
              />
              <SortHeader
                label="Received"
                active={filters.sort === 'received'}
                dir={filters.dir}
                onClick={() => toggleSort('received')}
              />
              <FilterHeader
                label="Docs"
                options={filterOptions?.pipeline ?? []}
                selected={filters.pipeline_filter}
                onToggle={(v) => toggleFilter('pipeline_filter', v)}
                sortActive={filters.sort === 'docs'}
                onSort={() => toggleSort('docs')}
              />
              <span>Preview</span>
              <FilterHeader
                label="Case / status"
                options={filterOptions?.cases ?? []}
                optionMono
                selected={filters.case_id}
                onToggle={(v) => toggleFilter('case_id', v)}
                sortActive={filters.sort === 'status'}
                onSort={() => toggleSort('status')}
              />
              <FilterHeader
                label="Proceeding"
                options={filterOptions?.proceedings ?? []}
                optionMono
                align="right"
                selected={filters.proceeding_id}
                onToggle={(v) => toggleFilter('proceeding_id', v)}
              />
              <span className="text-right">Actions</span>
            </div>
            {status !== 'all' && (query.data?.slicing_queue.length ?? 0) > 0 && (
              <p className="border border-t-0 border-line px-3 py-1.5 text-[11px] text-muted">
                <Icon name="content_cut" size={12} />{' '}
                {pluralize(query.data?.slicing_queue.length ?? 0, 'scan')} awaiting slicing —{' '}
                <button
                  type="button"
                  onClick={() => setStatus('all')}
                  className="text-tealink hover:underline"
                >
                  show in All
                </button>
              </p>
            )}
            {!query.data && (
              <div className="border border-t-0 border-line px-3">
                <QueryState error={query.error} pending={query.isPending} />
              </div>
            )}
            {query.data && visible.length === 0 && slicingRows.length === 0 && (
              <p className="border border-t-0 border-line px-3 py-8 text-center text-[12px] text-muted">
                {bundles.length === 0
                  ? 'Inbox empty — nothing awaits triage.'
                  : 'No bundles match this filter.'}
              </p>
            )}
            <ul className="divide-y divide-line2 border border-t-0 border-line">
              {slicingRows.map((s) => (
                <SlicingRow key={`slice-${s.batch_id}`} item={s} />
              ))}
              {visible.map((b) => (
                <BundleRow
                  key={b.key}
                  bundle={b}
                  selected={selected.has(b.key)}
                  onSelect={(on) =>
                    setSelected((s) => {
                      const n = new Set(s)
                      if (on) n.add(b.key)
                      else n.delete(b.key)
                      return n
                    })
                  }
                  expanded={expanded === b.key}
                  onToggle={() => toggleExpanded(b.key)}
                  onConfirm={(action) => setConfirmTarget({ mode: 'single', bundle: b, action })}
                  onRetry={() => setRetryTarget(b)}
                  cases={query.data?.cases ?? []}
                  proceedings={query.data?.proceedings ?? []}
                />
              ))}
            </ul>
          </div>
        </div>
      </div>

      <ConfirmBundleModal
        target={confirmTarget}
        onClose={() => setConfirmTarget(null)}
        cases={query.data?.cases ?? []}
        proceedings={query.data?.proceedings ?? []}
        onReview={(key, docId) =>
          setParams(
            (p) => {
              const next = new URLSearchParams(p)
              next.set('bundle', key)
              next.set('doc', String(docId))
              return next
            },
            { replace: true },
          )
        }
        onBatchConfirmed={() => setSelected(new Set())}
        onFiled={(key) => {
          // Keep working: open the bundle that follows the filed one.
          if (expanded !== key) return
          const next = visible[visible.findIndex((b) => b.key === key) + 1]
          if (!next) toast('No more bundles in the inbox')
          setParams(
            (p) => {
              const nextParams = new URLSearchParams(p)
              nextParams.delete('doc')
              if (next) nextParams.set('bundle', next.key)
              else nextParams.delete('bundle')
              return nextParams
            },
            { replace: true },
          )
        }}
      />
      <ConfirmDialog
        open={retryTarget !== null}
        onClose={() => setRetryTarget(null)}
        onConfirm={() => {
          const t = retryTarget
          setRetryTarget(null)
          if (t && t.batch_id !== null)
            retry.mutate(t.batch_id, {
              onSuccess: () => toast('Pipeline re-queued'),
              onError: (e) => toast(e.message, 'error'),
            })
        }}
        title="Retry bundle?"
        body="Extraction and every AI stage run again from the original files. Manual metadata edits are kept; AI suggestions are regenerated."
        label="Retry"
      />
      <ConfirmDialog
        open={bulkDanger !== null}
        onClose={() => setBulkDanger(null)}
        onConfirm={() => {
          const kind = bulkDanger
          setBulkDanger(null)
          if (!kind) return
          batchRemove.mutate(
            { keys: picked.map((b) => b.key), action: kind },
            {
              onSuccess: (r) => {
                const done = kind === 'delete' ? 'deleted' : 'archived'
                toast(
                  r.skipped.length > 0
                    ? `${r.removed_keys.length} ${done}, ${r.skipped.length} skipped`
                    : `${r.removed_keys.length} ${done}`,
                )
                setSelected(new Set())
              },
              onError: (e) => toast(e.message, 'error'),
            },
          )
        }}
        title={
          bulkDanger === 'delete'
            ? `Delete ${picked.length} bundle(s)?`
            : `Archive ${picked.length} bundle(s)?`
        }
        body={
          bulkDanger === 'delete'
            ? 'The documents, their files, reactions, pins and relationships are removed permanently. Bundles still being processed are skipped.'
            : 'The bundles leave the inbox; their documents are kept as dismissed.'
        }
        label={bulkDanger === 'delete' ? 'Delete' : 'Archive'}
        danger={bulkDanger === 'delete'}
        pending={batchRemove.isPending}
      />
      <ConfirmDialog
        open={bulkRetryOpen}
        onClose={() => setBulkRetryOpen(false)}
        onConfirm={() => {
          setBulkRetryOpen(false)
          batchRetry.mutate(
            picked.map((b) => b.key),
            {
              onSuccess: (r) => {
                toast(
                  r.skipped.length > 0
                    ? `${r.retried_keys.length} re-queued, ${r.skipped.length} skipped`
                    : `${r.retried_keys.length} re-queued`,
                )
                setSelected(new Set())
              },
              onError: (e) => toast(e.message, 'error'),
            },
          )
        }}
        title={`Retry ${picked.length} bundle(s)?`}
        body="Extraction and every AI stage run again from the original files. Bundles with a stage running now, and loose documents, are skipped."
        label="Retry"
        pending={batchRetry.isPending}
      />
      <IngestModal open={uploadOpen} caseId={uploadCase} onClose={() => setParams({})} />
    </div>
  )
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
}: {
  label: string
  active: boolean
  dir: 'asc' | 'desc'
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1 text-left uppercase ${active ? 'text-ink' : ''}`}
    >
      {label}{' '}
      <Icon
        name={active ? (dir === 'desc' ? 'arrow_downward' : 'arrow_upward') : 'swap_vert'}
        size={12}
      />
    </button>
  )
}

function FilterHeader({
  label,
  options,
  selected,
  onToggle,
  sortActive,
  onSort,
  optionMono = false,
  align = 'left',
}: {
  label: string
  options: Schemas['PickerOption'][]
  selected: string[]
  onToggle: (v: string) => void
  sortActive?: boolean
  onSort?: () => void
  // Case IDs and Aktenzeichen read in the mono face, like everywhere else in the table.
  optionMono?: boolean
  // The last column's panel opens toward the table, not off the viewport edge.
  align?: 'left' | 'right'
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')
  const needle = search.trim().toLowerCase()
  const shown = needle
    ? options.filter((o) => `${o.label} ${o.detail ?? ''}`.toLowerCase().includes(needle))
    : options
  const trigger = useRef<HTMLButtonElement>(null)
  return (
    <span className="relative flex items-center gap-1">
      {onSort ? (
        <button
          type="button"
          onClick={onSort}
          className={`uppercase ${sortActive ? 'text-ink' : ''}`}
        >
          {label}
        </button>
      ) : (
        <span>{label}</span>
      )}
      <button
        ref={trigger}
        type="button"
        aria-label={`Filter ${label}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={selected.length ? 'text-accent' : 'text-muted hover:text-ink'}
      >
        <Icon name={selected.length ? 'filter_alt' : 'filter_list'} size={13} />
      </button>
      <Menu
        open={open}
        onClose={() => {
          setOpen(false)
          setSearch('')
        }}
        anchorRef={trigger}
        align={align}
        role="dialog"
        label={`${label} filter`}
        className="max-h-80 w-72 overflow-y-auto p-1.5 text-[11px] normal-case"
      >
        {options.length > SEARCH_FROM && (
          <input
            type="search"
            autoFocus
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label={`Search ${label}`}
            placeholder="Search…"
            className="mb-1 w-full rounded-md border border-line bg-panel2 px-2 py-1 font-normal tracking-normal"
          />
        )}
        {shown.length === 0 && (
          <p className="px-2 py-1 text-muted">{options.length === 0 ? 'No options' : 'No match'}</p>
        )}
        {shown.map((o) => (
          <label
            key={o.value}
            title={o.detail ? `${o.label} · ${o.detail}` : o.label}
            className="flex items-center gap-2 rounded-md px-2 py-1 hover:bg-accent/5"
          >
            <input
              type="checkbox"
              checked={selected.includes(o.value)}
              onChange={() => onToggle(o.value)}
              className="h-3.5 w-3.5 shrink-0 accent-accent"
            />
            <span className="min-w-0 font-normal tracking-normal">
              <span className={`block truncate ${optionMono ? 'font-mono' : ''}`}>{o.label}</span>
              {o.detail && (
                <span className="block truncate text-[10px] text-muted">{o.detail}</span>
              )}
            </span>
          </label>
        ))}
      </Menu>
    </span>
  )
}

const STATUS_STRIPE: Record<TriageBundle['status'], string> = {
  stuck: 'border-l-danger',
  processing: 'border-l-warning',
  needs_classification: 'border-l-info',
  needs_review: 'border-l-accent',
}

function CaseTitle({ title }: { title: string }) {
  return (
    <span className="block truncate text-[10.5px] text-muted" title={title}>
      {title}
    </span>
  )
}

function SlicingRow({ item: s }: { item: Schemas['SlicingQueueItem'] }) {
  const preparing = s.status === 'preparing'
  const failed = s.status === 'failed'
  const done = s.progress_done ?? 0
  const total = s.progress_total ?? s.page_count ?? 0
  return (
    <li className={`border-l-2 ${failed ? 'border-l-danger' : 'border-l-accent'}`}>
      <div className={`${GRID} px-3 py-2.5 text-[12px]`}>
        <span />
        <span>
          <span className="flex items-center gap-1 font-mono text-[11px] text-tealink">
            <Icon name="scanner" size={12} className="text-muted" /> ib-
            {String(s.batch_id).padStart(4, '0')}
          </span>
          {s.received_at && (
            <span className="block font-mono text-[10px] text-muted">
              {formatShortDate(s.received_at)} · scan
            </span>
          )}
        </span>
        <span className="text-[11px]">
          {s.page_count ? `${s.page_count} pp` : ''}
          {preparing && total > 0 && (
            <span className="mt-1 block">
              <Progress value={done} max={total} label="Preparing scan" />
            </span>
          )}
        </span>
        <span className="min-w-0">
          <span className="block truncate font-semibold">{s.subject ?? 'Scanned stack'}</span>
          <span className={`block text-[10.5px] ${failed ? 'text-danger' : 'text-muted'}`}>
            {failed
              ? 'Preparation failed — open to retry'
              : preparing
                ? s.progress_phase === 'ai'
                  ? 'Asking the model where letters end…'
                  : total > 0
                    ? `Reading page ${done} of ${total}…`
                    : 'Preparing…'
                : s.proposed_cut_count
                  ? `${pluralize(s.proposed_cut_count, 'cut')} proposed — review before processing`
                  : 'Ready — no cuts proposed, confirm or add cuts'}
          </span>
        </span>
        <span>
          <Badge tone={failed ? 'danger' : 'warning'}>awaiting slicing</Badge>
        </span>
        <span />
        <span className="text-right">
          <Link to={`/ingest/slice/${s.batch_id}`} className={buttonClass('primary')}>
            <Icon name="content_cut" size={14} /> Slice
          </Link>
        </span>
      </div>
    </li>
  )
}

function BundleRow({
  bundle: b,
  selected,
  onSelect,
  expanded,
  onToggle,
  onConfirm,
  onRetry,
  cases,
  proceedings,
}: {
  bundle: TriageBundle
  selected: boolean
  onSelect: (on: boolean) => void
  expanded: boolean
  onToggle: () => void
  onConfirm: (action: 'confirm_bundle' | 'assign_case') => void
  onRetry: () => void
  cases: Schemas['PickerCase'][]
  proceedings: Schemas['PickerProceeding'][]
}) {
  const action = useBundleAction()
  const docAction = useDocumentAction()
  const openDocument = useOpenDocument()
  const toast = useToast()
  const [menu, setMenu] = useState(false)
  const menuTrigger = useRef<HTMLButtonElement>(null)
  // The selected document lives in the URL (`?doc=`), like the expanded bundle, so the
  // confirm modal can jump to one and a round-trip through the HUD reopens it.
  const [params, setParams] = useSearchParams()
  const docParam = Number(params.get('doc'))
  const activeDoc = b.documents.some((d) => d.id === docParam) ? docParam : b.lead_doc_id
  const setActiveDoc = (id: number) =>
    setParams(
      (p) => {
        const next = new URLSearchParams(p)
        next.set('doc', String(id))
        return next
      },
      { replace: true },
    )
  const [confirmDanger, setConfirmDanger] = useState<'dismiss' | 'delete' | null>(null)
  const [docDanger, setDocDanger] = useState<{
    id: number
    title: string
    action: 'dismiss' | 'delete'
  } | null>(null)
  const processing = b.status === 'processing'
  // A packet-like PDF waits for the user to split it or keep it whole; the email waits with it.
  const heldDocs = b.documents.filter((d) => d.held_for_split)
  const soleHeld = heldDocs.length === 1 ? heldDocs[0] : undefined
  const sourceIcon = { email: 'mail', scan: 'scanner', manual: 'upload_file' }[b.source_type]
  const confidence = b.sub_groups[0]?.case_confidence ?? null
  const confirmable = b.doc_count > 0 && !processing && b.status !== 'stuck'
  const openParts = bundleOpenParts(b)
  const caseTitle = b.confirmed_case_title ?? b.suggestion?.title ?? null

  return (
    <li className={`border-l-2 ${STATUS_STRIPE[b.status]}`} data-bundle-key={b.key}>
      <div
        className={`${GRID} ${b.status === 'needs_review' && b.confirmed_case_id ? 'opacity-90' : ''} cursor-pointer px-3 py-2.5 text-[12px] hover:bg-accent/5`}
        onClick={onToggle}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onToggle()
          }
        }}
        aria-expanded={expanded}
      >
        <input
          type="checkbox"
          aria-label={`Select ${b.title}`}
          checked={selected}
          disabled={processing}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => onSelect(e.target.checked)}
          className="h-3.5 w-3.5 accent-accent"
        />
        <span>
          <span className="flex items-center gap-1 font-mono text-[11px] text-tealink">
            <Icon name={sourceIcon} size={12} className="text-muted" /> ib-
            {String(b.batch_id ?? b.lead_doc_id).padStart(4, '0')}
          </span>
          <span className="block font-mono text-[10px] text-muted">
            {formatShortDate(b.received_at)} · {b.source_type}
          </span>
        </span>
        <span>
          <span className="flex items-center gap-1.5 text-[11px]">
            {pluralize(b.doc_count, 'doc')} · {b.total_pages} pp
            {b.has_manual_groups && <Icon name="lock" size={11} className="text-muted" />}
            <span className="flex gap-0.5">
              {b.originator_types.map((o) => (
                <span key={o} className={`h-2 w-2 rounded-sm ${ORIGINATOR_COLOR[o]}`} title={o} />
              ))}
            </span>
          </span>
          <span className="mt-1 flex items-center gap-2">
            <BundleStateBar pipeline={b.pipeline} />
            <span className="font-mono text-[10px] text-muted">
              {b.pipeline.active_label ??
                (b.status === 'stuck'
                  ? 'failed'
                  : `${b.pipeline.counts.completed ?? 0}/${b.pipeline.total}`)}
            </span>
          </span>
        </span>
        <span className="min-w-0">
          <span className="block truncate font-semibold">{b.title}</span>
          <span className="flex flex-wrap items-center gap-1 text-[10.5px] text-muted">
            {b.action_dates.map(
              (a) =>
                a.due_date && (
                  <Badge key={a.title} tone="danger" mono>
                    ⚑ {formatShortDate(a.due_date)}
                  </Badge>
                ),
            )}
            {b.pipeline.failed_error && (
              <span className="truncate text-danger" title={b.pipeline.failed_error}>
                {b.pipeline.failed_error}
              </span>
            )}
            {!b.pipeline.failed_error && openParts.length > 0 && (
              <span className="truncate text-warning" title={openParts.join(' · ')}>
                {openParts.length <= 2 ? openParts.join(' · ') : `${openParts.length} open items`}
              </span>
            )}
          </span>
        </span>
        <span className="min-w-0">
          {b.confirmed_case_id ? (
            <>
              <span className="block font-mono text-[11px] text-tealink">
                {b.confirmed_case_id}
              </span>
              {caseTitle && <CaseTitle title={caseTitle} />}
            </>
          ) : b.suggestion ? (
            <>
              {(confidence || b.suggestion.is_draft) && (
                <span className="mb-1 flex items-center gap-1">
                  {confidence && (
                    <Badge tone={CONFIDENCE_TONE[confidence] ?? 'neutral'} pill>
                      {confidence}
                    </Badge>
                  )}
                  {b.suggestion.is_draft && (
                    <Badge tone="warning" pill>
                      draft
                    </Badge>
                  )}
                </span>
              )}
              <span className="block font-mono text-[11px] text-tealink">
                {b.suggestion.case_id}
              </span>
              {caseTitle && <CaseTitle title={caseTitle} />}
            </>
          ) : (
            <span className="text-muted">no suggestion — route</span>
          )}
        </span>
        <span className="min-w-0">
          {b.proceeding ? (
            <>
              <span className="flex items-center gap-1.5 font-mono text-[11px]">
                {b.proceeding.az_court ?? '—'}
                <Badge tone="accent" pill>
                  {b.proceeding.court_level.toUpperCase()}
                </Badge>
              </span>
              <span className="block truncate text-[10px] text-muted">
                {b.proceeding.court_name}
              </span>
            </>
          ) : (
            <span className="text-muted2">—</span>
          )}
        </span>
        <span
          className="flex flex-nowrap items-center justify-end gap-1"
          onClick={(e) => e.stopPropagation()}
        >
          {b.status === 'stuck' && (
            <Button
              size="icon"
              variant="secondary"
              onClick={onRetry}
              aria-label="Retry"
              title="Retry"
            >
              <Icon name="replay" size={16} />
            </Button>
          )}
          {confirmable && (b.suggestion || b.confirmed_case_id) && (
            <Button
              size="icon"
              onClick={() => onConfirm('confirm_bundle')}
              aria-label="Confirm"
              title="Confirm bundle"
            >
              <Icon name="check" size={16} />
            </Button>
          )}
          {confirmable && !b.suggestion && !b.confirmed_case_id && (
            <Button
              size="icon"
              variant="secondary"
              onClick={() => onConfirm('assign_case')}
              aria-label="Route"
              title="Route to a case"
            >
              <Icon name="alt_route" size={16} />
            </Button>
          )}
          {soleHeld ? (
            <>
              <SplitButton docId={soleHeld.id} compact />
              <KeepWholeButton docId={soleHeld.id} compact />
            </>
          ) : (
            processing &&
            heldDocs.length === 0 && (
              <Icon name="hourglass_top" size={16} className="text-warning" />
            )
          )}
          {(!processing || heldDocs.length > 0) && (
            <span className="relative">
              <button
                ref={menuTrigger}
                type="button"
                aria-label="More actions"
                aria-expanded={menu}
                onClick={() => setMenu((m) => !m)}
                title="More actions"
                className="flex h-7 w-7 items-center justify-center rounded-[7px] border border-line text-muted hover:bg-accent/7 hover:text-ink"
              >
                <Icon name="more_horiz" size={16} />
              </button>
              <Menu
                open={menu}
                onClose={() => setMenu(false)}
                anchorRef={menuTrigger}
                label="Bundle actions"
                className="w-44 p-1 text-[11.5px]"
              >
                {b.batch_id !== null && (
                  <MenuItem
                    icon="replay"
                    label="Retry"
                    onClick={() => {
                      setMenu(false)
                      onRetry()
                    }}
                  />
                )}
                <MenuItem
                  icon="archive"
                  label="Archive"
                  onClick={() => {
                    setMenu(false)
                    setConfirmDanger('dismiss')
                  }}
                />
                <MenuItem
                  icon="delete"
                  label="Delete"
                  danger
                  onClick={() => {
                    setMenu(false)
                    setConfirmDanger('delete')
                  }}
                />
              </Menu>
            </span>
          )}
        </span>
      </div>
      {expanded && (
        <div className="flex border-t border-line bg-card2">
          <div className="w-[300px] shrink-0 border-r border-line p-3.5">
            <BundleTree
              bundle={b}
              activeDocId={activeDoc}
              onSelect={setActiveDoc}
              onDocAction={(d, kind) => setDocDanger({ id: d.id, title: d.title, action: kind })}
              footer={
                <div className="mt-3.5 flex flex-col gap-1.5">
                  {confirmable && (
                    <>
                      <Button
                        className="w-full"
                        onClick={() =>
                          onConfirm(
                            b.suggestion || b.confirmed_case_id ? 'confirm_bundle' : 'assign_case',
                          )
                        }
                      >
                        Confirm bundle{' '}
                        <kbd className="rounded-[3px] bg-on-accent/25 px-[5px] py-px font-mono text-[9px]">
                          ⌘↵
                        </kbd>
                      </Button>
                      <span className="text-center text-[9px] text-muted2 italic">
                        {pluralize(b.doc_count, 'doc')} · cascades to case
                      </span>
                    </>
                  )}
                  {processing && (
                    <button
                      type="button"
                      onClick={() => setConfirmDanger('dismiss')}
                      className="text-center text-[10px] text-muted hover:underline"
                    >
                      Archive bundle
                    </button>
                  )}
                </div>
              }
            />
          </div>
          <div className="min-w-0 flex-1">
            {activeDoc !== null ? (
              <DocumentReview
                docId={activeDoc}
                onOpenHud={openDocument}
                routing={{
                  bundle: b,
                  cases,
                  proceedings,
                }}
              />
            ) : (
              <p className="p-4 text-[12px] text-muted">Select a document.</p>
            )}
          </div>
        </div>
      )}
      <ConfirmDialog
        open={docDanger !== null}
        onClose={() => setDocDanger(null)}
        onConfirm={() => {
          const target = docDanger
          setDocDanger(null)
          if (target)
            docAction.mutate(
              { docId: target.id, action: target.action },
              {
                onSuccess: () => {
                  toast(target.action === 'delete' ? 'Document deleted' : 'Document archived')
                  // A stale ?doc= falls back to the lead, but should not linger in the URL.
                  if (docParam === target.id)
                    setParams(
                      (p) => {
                        const next = new URLSearchParams(p)
                        next.delete('doc')
                        return next
                      },
                      { replace: true },
                    )
                },
                onError: (e) => toast(e.message, 'error'),
              },
            )
        }}
        title={
          docDanger?.action === 'delete'
            ? 'Delete this document permanently?'
            : 'Archive this document?'
        }
        body={
          docDanger?.action === 'delete'
            ? `"${docDanger.title}" and its file are removed permanently.`
            : `"${docDanger?.title}" leaves the bundle and is kept as dismissed; it is not filed into the case.`
        }
        label={docDanger?.action === 'delete' ? 'Delete' : 'Archive'}
        danger={docDanger?.action === 'delete'}
        pending={docAction.isPending}
      />
      <ConfirmDialog
        open={confirmDanger !== null}
        onClose={() => setConfirmDanger(null)}
        onConfirm={() => {
          const kind = confirmDanger
          setConfirmDanger(null)
          if (kind)
            action.mutate(
              { bundle: b, action: kind },
              {
                onSuccess: () => toast(kind === 'delete' ? 'Bundle deleted' : 'Bundle archived'),
                onError: (e) => toast(e.message, 'error'),
              },
            )
        }}
        title={confirmDanger === 'delete' ? 'Delete bundle?' : 'Archive bundle?'}
        body={
          confirmDanger === 'delete'
            ? 'The documents and their files are removed permanently.'
            : 'The bundle leaves the inbox; its documents are kept as dismissed.'
        }
        label={confirmDanger === 'delete' ? 'Delete' : 'Archive'}
        danger={confirmDanger === 'delete'}
      />
    </li>
  )
}

function MenuItem({
  icon,
  label,
  onClick,
  danger,
}: {
  icon: string
  label: string
  onClick: () => void
  danger?: boolean
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className={`flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left hover:bg-accent/7 ${danger ? 'text-danger' : 'text-ink2'}`}
    >
      <Icon name={icon} size={14} /> {label}
    </button>
  )
}
