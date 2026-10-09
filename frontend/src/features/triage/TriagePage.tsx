import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'

import { reviewReasonLabel } from '../documents/reviewReasons'
import { useOpenDocument } from '../documents/useOpenDocument'

import type { Schemas } from '../../api/client'
import {
  type TriageBundle,
  type TriageFilters,
  defaultFilters,
  useBatchConfirm,
  useBundleAction,
  useRetryAll,
  useRetryBundle,
  useTriage,
} from '../../api/triage'
import { formatShortDate, pluralize } from '../../format'
import { Badge } from '../../ui/Badge'
import { Button, buttonClass } from '../../ui/Button'
import { Chip } from '../../ui/Chip'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Icon } from '../../ui/Icon'
import { GmailImportStatus, useGmailRunBanner } from '../import/GmailImportStatus'
import { NewMailNotice, useNewMailToReview } from '../import/NewMailBanner'
import { BundleStateBar } from '../../ui/PipelineBar'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'
import { DocumentReview, ORIGINATOR_COLOR } from '../documents/DocumentReview'
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
const GRID = 'grid grid-cols-[24px_128px_198px_1fr_148px_150px_96px] items-center gap-3'

export function TriagePage() {
  const [params, setParams] = useSearchParams()
  const [filters, setFilters] = useState<TriageFilters>(defaultFilters)
  const [status, setStatus] = useState<Status>('all')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  // In the URL so coming back from the HUD reopens the same bundle.
  const expanded = params.get('bundle')
  const toggleExpanded = (key: string) =>
    setParams(
      (p) => {
        const next = new URLSearchParams(p)
        if (next.get('bundle') === key) next.delete('bundle')
        else next.set('bundle', key)
        return next
      },
      { replace: true },
    )
  const [confirmTarget, setConfirmTarget] = useState<ConfirmTarget | null>(null)
  const [retryTarget, setRetryTarget] = useState<TriageBundle | null>(null)
  const [retryAllOpen, setRetryAllOpen] = useState(false)
  const query = useTriage(filters)
  const retryAll = useRetryAll()
  const batchConfirm = useBatchConfirm()
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
  // Deep links (Home → ?bundle=) land on the bundle once the feed has loaded.
  const scrolledTo = useRef<string | null>(null)
  useEffect(() => {
    if (!expanded || scrolledTo.current === expanded) return
    const row = document.querySelector(`[data-bundle-key="${CSS.escape(expanded)}"]`)
    if (!row) return
    scrolledTo.current = expanded
    row.scrollIntoView({ block: 'start' })
  }, [expanded, bundles])
  const visible = bundles.filter((b) => status === 'all' || b.status === status)
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
    setFilters((f) => ({ ...f, sort, dir: f.sort === sort && f.dir === 'desc' ? 'asc' : 'desc' }))
  }
  function toggleFilter(key: 'case_id' | 'proceeding_id' | 'pipeline_filter', value: string) {
    setFilters((f) => ({
      ...f,
      [key]: f[key].includes(value) ? f[key].filter((v) => v !== value) : [...f[key], value],
    }))
  }

  const stats = query.data?.stats
  const gmailRun = useGmailRunBanner()
  const newMail = useNewMailToReview()
  return (
    <div className="flex min-h-full flex-col">
      <header className="flex items-center gap-4 border-b border-line bg-panel px-6 py-4">
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
        <div className="ml-auto flex items-center gap-2">
          <Link to="/settings/gmail/import" className={buttonClass('secondary')}>
            <Icon name="mail" size={16} /> Import from Gmail
            {newMail && (
              <Badge tone="accent" mono>
                {newMail.count}
              </Badge>
            )}
          </Link>
          <Button
            variant="secondary"
            disabled={retryAll.isPending || bundles.length === 0}
            onClick={() => setRetryAllOpen(true)}
          >
            <Icon name="replay" size={16} /> Re-Analyze all
          </Button>
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

      <div className="flex items-center gap-2 px-6 py-3">
        {(Object.keys(STATUS_LABEL) as Status[]).map((s) => (
          <Chip key={s} active={status === s} count={counts[s]} onClick={() => setStatus(s)}>
            {STATUS_LABEL[s]}
          </Chip>
        ))}
        {query.data && query.data.slicing_queue.length > 0 && (
          <span className="ml-auto flex items-center gap-2 text-[11px] text-muted">
            <Icon name="content_cut" size={14} />{' '}
            {pluralize(query.data.slicing_queue.length, 'scan')} awaiting slicing:
            {query.data.slicing_queue.map((s) => (
              <a
                key={s.batch_id}
                href={`/ingest/slice/${s.batch_id}`}
                className="font-mono text-tealink hover:underline"
              >
                #{s.batch_id}
                {s.status !== 'ready' && ` (${s.status})`}
              </a>
            ))}
          </span>
        )}
      </div>

      {selected.size > 0 && (
        <div className="mx-6 mb-2 flex items-center gap-2 rounded-xl border border-accent/30 bg-accent/8 px-3 py-2 text-[12px]">
          <span className="font-semibold">{selected.size} selected</span>
          <Button
            size="sm"
            disabled={batchConfirm.isPending}
            onClick={() =>
              batchConfirm.mutate([...selected], {
                onSuccess: (r) => {
                  toast(`${r.confirmed} confirmed, ${r.skipped} skipped (no suggestion)`)
                  setSelected(new Set())
                },
                onError: (e) => toast(e.message, 'error'),
              })
            }
          >
            Confirm ({selected.size})
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => setConfirmTarget({ mode: 'batch', keys: [...selected] })}
          >
            Assign to…
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
            options={query.data?.filter_options.pipeline ?? []}
            selected={filters.pipeline_filter}
            onToggle={(v) => toggleFilter('pipeline_filter', v)}
            sortActive={filters.sort === 'docs'}
            onSort={() => toggleSort('docs')}
          />
          <span>Preview</span>
          <FilterHeader
            label="Case / status"
            options={query.data?.filter_options.cases ?? []}
            selected={filters.case_id}
            onToggle={(v) => toggleFilter('case_id', v)}
            sortActive={filters.sort === 'status'}
            onSort={() => toggleSort('status')}
          />
          <FilterHeader
            label="Proceeding"
            options={query.data?.filter_options.proceedings ?? []}
            selected={filters.proceeding_id}
            onToggle={(v) => toggleFilter('proceeding_id', v)}
          />
          <span className="text-right">Actions</span>
        </div>
        {!query.data && (
          <div className="border border-t-0 border-line px-3">
            <QueryState error={query.error} pending={query.isPending} />
          </div>
        )}
        {query.data && visible.length === 0 && (
          <p className="border border-t-0 border-line px-3 py-8 text-center text-[12px] text-muted">
            {bundles.length === 0
              ? 'Inbox empty — nothing awaits triage.'
              : 'No bundles match this filter.'}
          </p>
        )}
        <ul className="divide-y divide-line2 border border-t-0 border-line">
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

      <ConfirmBundleModal
        target={confirmTarget}
        onClose={() => setConfirmTarget(null)}
        cases={query.data?.cases ?? []}
        proceedings={query.data?.proceedings ?? []}
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
        open={retryAllOpen}
        onClose={() => setRetryAllOpen(false)}
        onConfirm={() => {
          setRetryAllOpen(false)
          retryAll.mutate(undefined, {
            onSuccess: (r) => toast(`${r.retried} bundle(s) re-queued`),
            onError: (e) => toast(e.message, 'error'),
          })
        }}
        title="Re-analyze every bundle?"
        body="All AI stages run again for every bundle in your inbox. Bundles that are currently processing are skipped."
        label="Re-analyze all"
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
}: {
  label: string
  options: Schemas['PickerOption'][]
  selected: string[]
  onToggle: (v: string) => void
  sortActive?: boolean
  onSort?: () => void
}) {
  const [open, setOpen] = useState(false)
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
        type="button"
        aria-label={`Filter ${label}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className={selected.length ? 'text-accent' : 'text-muted hover:text-ink'}
      >
        <Icon name={selected.length ? 'filter_alt' : 'filter_list'} size={13} />
      </button>
      {open && (
        <>
          <button
            type="button"
            aria-label="Close filter"
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-190 cursor-default"
          />
          <div className="absolute top-full left-0 z-200 mt-1 w-56 rounded-xl border border-line bg-card p-1.5 text-[11px] normal-case shadow-[0_16px_40px_rgba(0,0,0,.5)]">
            {options.length === 0 && <p className="px-2 py-1 text-muted">No options</p>}
            {options.map((o) => (
              <label
                key={o.value}
                className="flex items-center gap-2 rounded-md px-2 py-1 hover:bg-accent/5"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(o.value)}
                  onChange={() => onToggle(o.value)}
                  className="h-3.5 w-3.5 accent-accent"
                />
                <span className="truncate font-normal tracking-normal">{o.label}</span>
              </label>
            ))}
          </div>
        </>
      )}
    </span>
  )
}

const STATUS_STRIPE: Record<TriageBundle['status'], string> = {
  stuck: 'border-l-danger',
  processing: 'border-l-warning',
  needs_classification: 'border-l-info',
  needs_review: 'border-l-accent',
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
  const openDocument = useOpenDocument()
  const toast = useToast()
  const [menu, setMenu] = useState(false)
  const [activeDoc, setActiveDoc] = useState<number | null>(b.lead_doc_id)
  const [confirmDanger, setConfirmDanger] = useState<'dismiss' | 'delete' | null>(null)
  const processing = b.status === 'processing'
  const sourceIcon = { email: 'mail', scan: 'scanner', manual: 'upload_file' }[b.source_type]
  const confidence = b.sub_groups[0]?.case_confidence ?? null
  const confirmable = !processing && b.status !== 'stuck'

  return (
    <li
      className={`border-l-2 ${STATUS_STRIPE[b.status]} ${b.status === 'needs_review' && b.confirmed_case_id ? 'opacity-90' : ''}`}
      data-bundle-key={b.key}
    >
      <div
        className={`${GRID} cursor-pointer px-3 py-2.5 text-[12px] hover:bg-accent/5`}
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
          aria-label={`Select ${b.subject ?? b.key}`}
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
          <span className="block truncate font-semibold">
            {b.subject ?? b.documents[0]?.title ?? 'Untitled bundle'}
          </span>
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
            {!b.pipeline.failed_error && b.open_review_reasons.length > 0 && (
              <span className="truncate text-warning">
                {b.open_review_reasons.length <= 2
                  ? b.open_review_reasons.map(reviewReasonLabel).join(' · ')
                  : `${b.open_review_reasons.length} open items`}
              </span>
            )}
          </span>
        </span>
        <span className="min-w-0">
          {b.confirmed_case_id ? (
            <span className="font-mono text-[11px] text-tealink">{b.confirmed_case_id}</span>
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
            </>
          ) : (
            <span className="text-muted">no suggestion — route</span>
          )}
          <span className="block text-[10px] text-muted">{STATUS_LABEL[b.status]}</span>
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
        <span className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
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
          {processing && <Icon name="hourglass_top" size={16} className="text-warning" />}
          {!processing && (
            <span className="relative">
              <button
                type="button"
                aria-label="More actions"
                aria-expanded={menu}
                onClick={() => setMenu((m) => !m)}
                title="More actions"
                className="flex h-7 w-7 items-center justify-center rounded-[7px] border border-line text-muted hover:bg-accent/7 hover:text-ink"
              >
                <Icon name="more_horiz" size={16} />
              </button>
              {menu && (
                <>
                  <button
                    type="button"
                    aria-label="Close menu"
                    onClick={() => setMenu(false)}
                    className="fixed inset-0 z-190 cursor-default"
                  />
                  <div
                    role="menu"
                    className="absolute top-full right-0 z-200 mt-1 w-44 rounded-xl border border-line bg-card p-1 text-[11.5px] shadow-[0_16px_40px_rgba(0,0,0,.5)]"
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
                  </div>
                </>
              )}
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
                  onCreateCase: () => onConfirm('assign_case'),
                }}
              />
            ) : (
              <p className="p-4 text-[12px] text-muted">Select a document.</p>
            )}
          </div>
        </div>
      )}
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
