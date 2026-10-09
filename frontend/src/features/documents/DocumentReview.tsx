import { type FormEvent, Fragment, useState } from 'react'
import { Link } from 'react-router'

import type { Schemas } from '../../api/client'
import {
  useActionStatus,
  useDocumentReview,
  useDraftDecision,
  useReact,
  useEvidenceDecision,
  useRelationshipDecision,
  useSetTitle,
  useStageRetry,
  useSummaryAction,
  useUpdateMetadata,
} from '../../api/triage'
import { formatIsoDate, formatShortDate } from '../../format'
import { Badge, type Tone } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { Modal } from '../../ui/Modal'
import { QueryState } from '../../ui/QueryState'
import { TextField } from '../../ui/TextField'
import { useToast } from '../../ui/toast'
import { ReviewChecklist } from '../triage/ReviewChecklist'
import { type Routing, RoutingCards } from '../triage/RoutingPickers'

type Review = Schemas['DocumentReview']

export const ORIGINATOR_COLOR: Record<string, string> = {
  court: 'bg-info',
  opposing: 'bg-danger',
  own: 'bg-success',
  third_party: 'bg-warning',
  unknown: 'bg-line3',
}

const REACTIONS: [Schemas['UserReactionType'], string, string][] = [
  ['lies', '🚩', 'Lies'],
  ['true', '✅', 'True'],
  ['needs_proof', '🔍', 'Needs proof'],
  ['precedent', '⚖️', 'Precedent'],
]

type Props = {
  docId: number
  onOpenHud?: (docId: number) => void
  /** Triage only: lets the case and proceeding cards re-route the bundle. */
  routing?: Routing
}

/** What the full-screen HUD wires into the passage spine. */
export type PassageHooks = {
  activePassageId: string | null
  onFocus: (passageId: string) => void
  onPin: (passageId: string) => void
  onAskAi: (passageText: string) => void
}

/** The intelligence panel for one document in the triage inline review. */
export function DocumentReview({ docId, onOpenHud, routing }: Props) {
  const query = useDocumentReview(docId)
  const review = query.data
  if (!review) return <QueryState error={query.error} pending={query.isPending} />
  return (
    <div className="text-[12px]">
      <Header review={review} onOpenHud={onOpenHud} />
      <ReviewSections review={review} routing={routing} />
    </div>
  )
}

/** The rail sections shared by the triage review and the HUD (`passages` adds spine behaviour). */
export function ReviewSections({
  review,
  routing,
  passages,
  singleColumn = false,
}: {
  review: Review
  routing?: Routing
  passages?: PassageHooks
  singleColumn?: boolean
}) {
  return (
    <>
      <ReviewChecklist review={review} bundle={routing?.bundle} />
      <Pipeline review={review} />
      <CaseAndProceeding review={review} routing={routing} />
      <Metadata review={review} singleColumn={singleColumn} />
      <Summary review={review} />
      <Passages review={review} hooks={passages} />
      <div className="border-b border-line2 px-4.5 py-3">
        <div className={singleColumn ? 'space-y-3' : 'grid grid-cols-2 gap-3'}>
          <Relationships review={review} />
          <Grounds review={review} />
          <Actions review={review} />
          <CostSignals review={review} />
        </div>
      </div>
      <Reactions review={review} />
    </>
  )
}

const SECTION_TITLE = 'text-[9px] font-bold tracking-[.1em] uppercase'

function Section({
  title,
  meta,
  icon,
  accent = false,
  boxed = false,
  className = '',
  children,
  action,
  dataAttr,
  id,
}: {
  title: string
  meta?: React.ReactNode
  icon?: string
  accent?: boolean
  /** A card inside a grid cell instead of a full-width ruled section. */
  boxed?: boolean
  className?: string
  children: React.ReactNode
  action?: React.ReactNode
  dataAttr?: Record<string, string>
  /** Anchor the review checklist jumps to. */
  id?: string
}) {
  return (
    <section
      id={id}
      className={`${boxed ? 'rounded-[11px] border border-line bg-card p-3' : 'border-b border-line2 px-4.5 py-3'} ${className}`}
      {...dataAttr}
    >
      <header className="mb-2.5 flex items-center gap-2">
        {icon && <Icon name={icon} size={13} className="text-accent" />}
        <h4 className={`${SECTION_TITLE} ${accent ? 'text-accent' : 'text-muted'}`}>{title}</h4>
        {typeof meta === 'string' ? (
          <span className="font-mono text-[10px] text-muted">{meta}</span>
        ) : (
          meta
        )}
        {action && <span className="ml-auto">{action}</span>}
      </header>
      {children}
    </section>
  )
}

function Header({ review, onOpenHud }: { review: Review; onOpenHud?: (id: number) => void }) {
  const setTitle = useSetTitle()
  const [editing, setEditing] = useState(false)
  const tierTone: Tone =
    review.significance_tier === 'critical'
      ? 'danger'
      : review.significance_tier === 'significant'
        ? 'warning'
        : 'neutral'
  return (
    <header className="flex items-center gap-2.5 border-b border-line2 px-4.5 py-3">
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${ORIGINATOR_COLOR[review.originator_type]}`}
      />
      <div className="min-w-0 flex-1">
        {editing ? (
          <form
            onSubmit={(e: FormEvent<HTMLFormElement>) => {
              e.preventDefault()
              const title = String(new FormData(e.currentTarget).get('title'))
              setTitle.mutate({ docId: review.id, title }, { onSuccess: () => setEditing(false) })
            }}
          >
            <input
              name="title"
              defaultValue={review.title}
              autoFocus
              className={inputClass}
              aria-label="Document title"
              onBlur={(e) => e.currentTarget.form?.requestSubmit()}
            />
          </form>
        ) : (
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="text-left font-display text-[14px] font-bold hover:underline"
            title="Rename"
          >
            {review.title}
          </button>
        )}
        <div className="mt-0.5 font-mono text-[10px] text-muted">
          #d-{review.id} · {review.originator_type.replace('_', ' ')}
          {review.document_type && ` · ${review.document_type}`}
          {review.issued_date && ` · ${formatIsoDate(review.issued_date)}`}
          {review.page_count > 0 && ` · ${review.page_count} pp`}
          {review.court_relay &&
            review.attributed_originator &&
            ` · relayed for ${review.attributed_originator}`}
        </div>
      </div>
      {review.significance_tier && <Badge tone={tierTone}>{review.significance_tier}</Badge>}
      {onOpenHud && (
        <Button variant="secondary" size="sm" onClick={() => onOpenHud(review.id)}>
          <Icon name="open_in_full" size={12} /> HUD
        </Button>
      )}
    </header>
  )
}

const SEGMENT: Record<string, string> = {
  completed: 'bg-success',
  skipped: 'bg-success',
  running: 'animate-pulse bg-warning',
  retrying: 'animate-pulse bg-warning',
  failed: 'bg-danger',
}

/** One line: a segment per stage, then a verdict. Failures surface their retry controls inline. */
function Pipeline({ review }: { review: Review }) {
  const retry = useStageRetry(review.id)
  const toast = useToast()
  const [errorOpen, setErrorOpen] = useState(false)
  const stages = review.pipeline.stages
  const done = stages.filter((s) => s.status === 'completed' || s.status === 'skipped').length
  const failed = stages.filter((s) => s.status === 'failed')
  const running = stages.find((s) => s.status === 'running' || s.status === 'retrying')
  const complete = stages.length > 0 && done === stages.length
  const ocrFailures = review.pipeline.ocr_page_failures
  return (
    <section className="border-b border-line2 px-4.5 py-2.5" aria-label="Pipeline">
      <div className="flex flex-wrap items-center gap-2.5">
        <h4 className={`${SECTION_TITLE} text-muted`}>Pipeline</h4>
        <div className="flex gap-0.5" role="img" aria-label={`${done} of ${stages.length} stages`}>
          {stages.map((s) => (
            <span
              key={s.key}
              title={`${s.label}: ${s.status ?? 'queued'}${s.error ? `\n${s.error}` : ''}`}
              className={`h-[5px] w-[18px] rounded-sm ${
                s.key === 'extract' && ocrFailures.length > 0 && s.status === 'completed'
                  ? 'bg-warning'
                  : (SEGMENT[s.status ?? ''] ?? 'bg-line3')
              }`}
            />
          ))}
        </div>
        {complete ? (
          <span className="flex items-center gap-1 text-[10px] text-success">
            <Icon name="check_circle" size={13} /> All {stages.length} stages complete
          </span>
        ) : failed.length > 0 ? (
          <span className="text-[10px] text-danger">{failed.length} failed</span>
        ) : running ? (
          <span className="text-[10px] text-warning">Running: {running.label}</span>
        ) : (
          <span className="font-mono text-[10px] text-muted">
            {done}/{stages.length} · {review.pipeline.state}
          </span>
        )}
        {failed.length > 0 && (
          <span className="ml-auto flex flex-wrap items-center gap-1">
            {failed.map((s) => (
              <button
                key={s.key}
                type="button"
                aria-label={`Retry ${s.label}`}
                onClick={() => retry.mutate(s.key, { onError: (e) => toast(e.message, 'error') })}
                className="inline-flex items-center gap-1 rounded border border-danger/40 px-1.5 py-0.5 font-mono text-[10px] text-danger hover:bg-danger/10"
              >
                <Icon name="replay" size={11} /> {s.label}
              </button>
            ))}
            <Button
              variant="secondary"
              size="sm"
              disabled={retry.isPending}
              onClick={() => retry.mutate('all', { onError: (e) => toast(e.message, 'error') })}
            >
              Retry all
            </Button>
          </span>
        )}
      </div>
      {ocrFailures.length > 0 && (
        <div
          role="alert"
          className="mt-1.5 flex items-center gap-2 text-[10px] text-warning"
          data-testid="ocr-page-failures"
        >
          <Icon name="warning" size={13} />
          <span className="min-w-0 flex-1">
            OCR failed on {ocrFailures.length === 1 ? 'page' : 'pages'} {ocrFailures.join(', ')};
            the text is incomplete.
          </span>
          <Button
            variant="secondary"
            size="sm"
            disabled={retry.isPending}
            onClick={() => retry.mutate('extract', { onError: (e) => toast(e.message, 'error') })}
          >
            Re-extract
          </Button>
        </div>
      )}
      {failed[0]?.error && (
        <button
          type="button"
          aria-expanded={errorOpen}
          onClick={() => setErrorOpen((open) => !open)}
          className={`mt-1.5 flex w-full items-start gap-1 text-left font-mono text-[10px] text-danger hover:underline ${
            errorOpen ? 'break-words whitespace-pre-wrap' : ''
          }`}
        >
          <span aria-hidden="true">{errorOpen ? '▾' : '▸'}</span>
          <span className={errorOpen ? 'min-w-0 break-words' : 'min-w-0 truncate'}>
            {failed[0].error}
          </span>
        </button>
      )}
    </section>
  )
}

function CaseAndProceeding({ review, routing }: { review: Review; routing?: Routing }) {
  const draft = useDraftDecision()
  const toast = useToast()
  return (
    <section id="review-case" className="border-b border-line2 px-4.5 py-3">
      <h4 className={`${SECTION_TITLE} mb-2 text-muted`}>
        Case &amp; proceeding{' '}
        {routing && (
          <span className="font-normal tracking-normal text-muted2 normal-case italic">
            · click to reassign
          </span>
        )}
      </h4>
      {routing ? (
        <RoutingCards review={review} routing={routing} />
      ) : (
        <div className="grid grid-cols-2 gap-2.5">
          <div className="rounded-[9px] border border-line bg-card px-[11px] py-[9px]">
            <div className="mb-1.5 text-[8px] font-bold tracking-[.1em] text-muted2 uppercase">
              Case
            </div>
            {review.case ? (
              <>
                <div className="flex items-center gap-[7px]">
                  <Link
                    to={`/cases/${review.case.id}`}
                    className="font-mono text-[13px] font-semibold hover:underline"
                  >
                    {review.case.id}
                  </Link>
                  {review.case.is_draft && (
                    <Badge tone="warning" pill>
                      draft
                    </Badge>
                  )}
                </div>
                <div className="mt-1 truncate text-[10px] text-muted">{review.case.title}</div>
              </>
            ) : (
              <span className="text-[11px] text-muted">Unassigned · in triage</span>
            )}
          </div>
          <div className="rounded-[9px] border border-line bg-card px-[11px] py-[9px]">
            <div className="mb-1.5 text-[8px] font-bold tracking-[.1em] text-muted2 uppercase">
              Proceeding
            </div>
            {review.proceeding ? (
              <>
                <div className="flex items-center gap-[7px]">
                  <span className="font-mono text-[12px]">{review.proceeding.az_court ?? '—'}</span>
                  <Badge tone="accent" pill>
                    {review.proceeding.court_level.toUpperCase()}
                  </Badge>
                </div>
                <div className="mt-1 truncate text-[10px] text-muted">
                  {review.proceeding.court_name}
                </div>
              </>
            ) : (
              <span className="text-[11px] text-muted">
                {review.az_court ? `AZ ${review.az_court} · no proceeding yet` : 'No proceeding'}
              </span>
            )}
          </div>
        </div>
      )}
      {review.case?.is_draft && (
        <div className="mt-2 flex gap-1.5">
          <Button
            size="sm"
            disabled={draft.isPending}
            onClick={() =>
              draft.mutate(
                { caseId: review.case?.id ?? '', decision: 'confirm' },
                {
                  onSuccess: () => toast('Draft case ratified'),
                  onError: (e) => toast(e.message, 'error'),
                },
              )
            }
          >
            Ratify
          </Button>
          <Button
            size="sm"
            variant="danger-outline"
            disabled={draft.isPending}
            onClick={() =>
              draft.mutate(
                { caseId: review.case?.id ?? '', decision: 'reject' },
                {
                  onSuccess: () => toast('Draft case rejected'),
                  onError: (e) => toast(e.message, 'error'),
                },
              )
            }
          >
            Reject
          </Button>
        </div>
      )}
    </section>
  )
}

const CONF_ROW: Record<string, { row: string; label: string; dot: string }> = {
  high: { row: 'border-l-success bg-card', label: 'text-muted2', dot: 'bg-success' },
  medium: {
    row: 'border-l-warning bg-warning/6 ring-1 ring-warning/16',
    label: 'text-warning',
    dot: 'bg-warning',
  },
  low: {
    row: 'border-l-danger bg-danger/6 ring-1 ring-danger/18',
    label: 'text-danger',
    dot: 'bg-danger',
  },
}
const CONF_NONE = { row: 'border-l-line3 bg-card', label: 'text-muted2', dot: '' }
const MONO_FIELD = /_date$|^internal_id$|^az_court$/

function Metadata({ review, singleColumn }: { review: Review; singleColumn?: boolean }) {
  const [editing, setEditing] = useState(false)
  const flagged = review.metadata.filter(
    (f) => f.confidence === 'low' || f.confidence === 'medium',
  ).length
  const graded = review.metadata.some((f) => f.confidence)
  return (
    <Section
      id="review-metadata"
      title="Metadata review"
      icon="edit_note"
      meta={
        flagged > 0 ? (
          <Badge tone="warning" pill>
            {flagged} need review
          </Badge>
        ) : (
          'all confirmed'
        )
      }
      action={
        <Button
          variant="secondary"
          size="sm"
          className="tracking-wide uppercase"
          onClick={() => setEditing(true)}
        >
          Edit
        </Button>
      }
    >
      <dl className={`grid gap-[7px] ${singleColumn ? 'grid-cols-1' : 'grid-cols-2'}`}>
        {review.metadata.map((f) => {
          const c = CONF_ROW[f.confidence ?? ''] ?? CONF_NONE
          return (
            <div
              key={f.field}
              className={`flex items-center gap-2 rounded-md border-l-2 px-[9px] py-1.5 ${c.row}`}
            >
              <dt
                className={`w-[62px] shrink-0 text-[8px] font-bold tracking-[.08em] uppercase ${c.label}`}
              >
                {f.label}
              </dt>
              <dd
                className={`min-w-0 flex-1 truncate text-[11px] ${MONO_FIELD.test(f.field) ? 'font-mono' : ''}`}
                title={f.value ?? ''}
              >
                {f.value ?? <span className="text-muted2">—</span>}
              </dd>
              {c.dot && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${c.dot}`} />}
            </div>
          )
        })}
      </dl>
      {graded && (
        <p className="mt-2 flex items-center gap-1.5 text-[9.5px] text-muted2 italic">
          <span className="h-1.5 w-1.5 rounded-full bg-success" />
          high
          <span className="ml-1.5 h-1.5 w-1.5 rounded-full bg-warning" />
          medium
          <span className="ml-1.5 h-1.5 w-1.5 rounded-full bg-danger" />
          low — only flagged fields pull the eye
        </p>
      )}
      <MetadataModal open={editing} onClose={() => setEditing(false)} review={review} />
    </Section>
  )
}

const ORIGINATORS = ['court', 'opposing', 'own', 'third_party', 'unknown'] as const
const TIERS = ['critical', 'significant', 'informational', 'administrative'] as const
const DOC_TYPES = [
  'ruling',
  'motion',
  'statement',
  'annex',
  'relay',
  'correspondence',
  'report',
  'invoice',
  'other',
] as const

function MetadataModal({
  open,
  onClose,
  review,
}: {
  open: boolean
  onClose: () => void
  review: Review
}) {
  const update = useUpdateMetadata(review.id)
  const toast = useToast()
  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const d = new FormData(e.currentTarget)
    const str = (k: string) => String(d.get(k) ?? '')
    const date = (k: string) => (str(k) ? `${str(k)}T00:00:00Z` : null)
    update.mutate(
      {
        originator_type: str('originator_type') as Schemas['OriginatorType'],
        sender: str('sender'),
        internal_id: str('internal_id'),
        issued_date: date('issued_date'),
        received_date: date('received_date'),
        significance_tier: (str('significance_tier') || null) as Schemas['SignificanceTier'] | null,
        document_type: (str('document_type') || null) as Schemas['DocumentType'] | null,
      },
      {
        onSuccess: () => {
          toast('Metadata saved')
          onClose()
        },
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }
  const value = (field: string) => review.metadata.find((f) => f.field === field)?.value ?? ''
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Edit metadata"
      subtitle={`#d-${review.id}`}
      icon="edit_note"
      width={520}
    >
      <form onSubmit={onSubmit} className="grid grid-cols-2 gap-3">
        <Field label="Originator" htmlFor="originator_type">
          <select
            id="originator_type"
            name="originator_type"
            defaultValue={review.originator_type}
            className={inputClass}
          >
            {ORIGINATORS.map((o) => (
              <option key={o} value={o}>
                {o.replace('_', ' ')}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Type" htmlFor="document_type">
          <select
            id="document_type"
            name="document_type"
            defaultValue={review.document_type ?? ''}
            className={inputClass}
          >
            <option value="">—</option>
            {DOC_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>
        <TextField label="Sender" name="sender" defaultValue={review.sender ?? ''} />
        <TextField
          label="Reference"
          name="internal_id"
          defaultValue={review.internal_id ?? ''}
          className="font-mono"
        />
        <TextField
          label="Issued"
          name="issued_date"
          type="date"
          defaultValue={value('issued_date')}
        />
        <TextField
          label="Received"
          name="received_date"
          type="date"
          defaultValue={value('received_date')}
        />
        <Field label="Tier" htmlFor="significance_tier">
          <select
            id="significance_tier"
            name="significance_tier"
            defaultValue={review.significance_tier ?? ''}
            className={inputClass}
          >
            <option value="">—</option>
            {TIERS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>
        <TextField
          label="AZ"
          value={review.az_court ?? ''}
          placeholder="—"
          readOnly
          tabIndex={-1}
          title="Comes from the proceeding."
          className="cursor-default font-mono text-muted focus:border-line"
          onChange={() => undefined}
        />
        <div className="col-span-2 flex justify-end gap-2 pt-1">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={update.isPending}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  )
}

const BULLET_TONE: Record<string, Tone> = { legal: 'accent', action: 'warning', finance: 'success' }

function Summary({ review }: { review: Review }) {
  const act = useSummaryAction(review.id)
  const retry = useStageRetry(review.id)
  const toast = useToast()
  const s = review.summary
  const state = s.approved_at
    ? `approved ${formatIsoDate(s.approved_at)}`
    : s.bullets.length
      ? 'generated'
      : (s.enrich_status ?? 'pending')
  return (
    <Section
      title="AI summary"
      icon="smart_toy"
      accent
      className="bg-linear-160 from-aibg to-transparent"
      meta={
        <Badge tone={s.bullets.length ? 'success' : 'neutral'} pill>
          {state}
        </Badge>
      }
    >
      {s.bullets.length === 0 ? (
        <p className="text-muted">
          {s.enrich_status === 'failed'
            ? 'Enrichment failed — retry the Enrich stage.'
            : 'No summary yet.'}
        </p>
      ) : (
        <div className="grid grid-cols-[max-content_1fr] items-start gap-x-[11px] gap-y-2">
          {s.bullets.map((b) => (
            <Fragment key={b.kind}>
              <Badge tone={BULLET_TONE[b.kind]} pill className="justify-center">
                {b.kind}
              </Badge>
              <span className="text-[11.5px] leading-normal text-ink2">{b.text}</span>
            </Fragment>
          ))}
        </div>
      )}
      {s.bullets.length > 0 && !s.approved_at && (
        <div className="mt-3 flex gap-2">
          <Button
            size="sm"
            disabled={act.isPending}
            onClick={() => act.mutate('approve', { onError: (e) => toast(e.message, 'error') })}
          >
            Approve
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={act.isPending || retry.isPending}
            onClick={() =>
              act.mutate('reject', {
                onSuccess: () => retry.mutate('enrich'),
                onError: (e) => toast(e.message, 'error'),
              })
            }
          >
            Regenerate
          </Button>
        </div>
      )}
    </Section>
  )
}

const PASSAGE_TONE: Record<string, string> = {
  ruling: 'border-accent',
  holding: 'border-accent',
  deadline: 'border-warning',
  finding: 'border-info',
  concession: 'border-success',
  neutral: 'border-line3',
}

function Passages({ review, hooks }: { review: Review; hooks?: PassageHooks }) {
  if (review.key_passages.length === 0) return null
  const claims = review.key_passages.filter((p) => p.claim_id).length
  return (
    <Section
      title="Key passages"
      meta={`${review.key_passages.length}${claims ? ` · ⚖ ${claims}` : ''}`}
    >
      <ul className="space-y-1.5">
        {review.key_passages.map((p) => {
          const active = hooks?.activePassageId === p.id
          const body = (
            <>
              <p className="text-[11px] leading-relaxed">“{p.text}”</p>
              <p className="font-mono text-[10px] text-muted">
                {p.kind ?? 'passage'}
                {p.page ? ` · p. ${p.page}` : ''}
                {p.claim_id ? ` · claim #${p.claim_id}` : ''}
                {p.start_offset === null ? ' · ⚠ approx' : ''}
                {p.pin_count ? ` · 📌 ${p.pin_count}` : ''}
              </p>
            </>
          )
          return (
            <li
              key={p.id}
              data-spine-passage={p.id}
              className={`rounded-[7px] border-l-[3px] px-2.5 py-2 ${PASSAGE_TONE[p.kind ?? 'neutral'] ?? 'border-line3'} ${active ? 'bg-accent/8' : 'bg-card'}`}
            >
              {hooks ? (
                <div className="flex items-start gap-1">
                  <button
                    type="button"
                    onClick={() => hooks.onFocus(p.id)}
                    aria-current={active ? 'true' : undefined}
                    className="min-w-0 flex-1 text-left hover:text-ink"
                  >
                    {body}
                  </button>
                  <button
                    type="button"
                    aria-label="Pin this passage"
                    title="Pin (n)"
                    onClick={() => hooks.onPin(p.id)}
                    className="text-muted hover:text-ink"
                  >
                    <Icon name="push_pin" size={14} />
                  </button>
                  <button
                    type="button"
                    aria-label="Ask the AI about this passage"
                    title="Ask AI"
                    onClick={() => hooks.onAskAi(p.text)}
                    className="text-muted hover:text-ink"
                  >
                    <Icon name="forum" size={14} />
                  </button>
                </div>
              ) : (
                body
              )}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

const REL_GLYPH: Record<string, string> = {
  replies_to: '←',
  attaches_as_proof: '⫸',
  references: '→',
  supersedes: '⇒',
  cited_by: '⇐',
  encloses: '⊃',
}

function Relationships({ review }: { review: Review }) {
  const decide = useRelationshipDecision(review.id)
  const toast = useToast()
  const toConfirm = review.relationships.filter((r) => r.confidence === 'ai_detected').length
  return (
    <Section
      id="review-relationships"
      boxed
      title="Relationships"
      meta={
        <>
          <span className="font-mono text-[10px] text-muted">
            {review.relationships.length || 'none'}
          </span>
          {toConfirm > 0 && (
            <Badge tone="warning" pill>
              {toConfirm} to confirm
            </Badge>
          )}
        </>
      }
      className={toConfirm > 0 ? 'border-warning/60' : ''}
    >
      <ul className="space-y-1 text-[11.5px] text-ink2">
        {review.relationships.map((r) => (
          <li key={r.id} className="flex items-center gap-2">
            <span className="w-4 text-center font-mono text-muted">
              {REL_GLYPH[r.rel_type] ?? '·'}
            </span>
            <span
              role="img"
              aria-label={`from ${r.originator_type.replace('_', ' ')}`}
              title={r.originator_type.replace('_', ' ')}
              className={`h-2 w-2 shrink-0 rounded-full ${ORIGINATOR_COLOR[r.originator_type]}`}
            />
            <Link
              to={`/document/${r.doc_id}`}
              className="min-w-0 flex-1 truncate hover:underline"
              title={r.title}
            >
              {r.title}
            </Link>
            <span className="font-mono text-[10px] text-muted">
              {r.rel_type.replace(/_/g, ' ')}
            </span>
            {r.confidence === 'ai_detected' ? (
              <>
                <button
                  type="button"
                  aria-label="Confirm relationship"
                  onClick={() =>
                    decide.mutate(
                      { relId: r.id, decision: 'confirm' },
                      { onError: (e) => toast(e.message, 'error') },
                    )
                  }
                  className="text-success"
                >
                  <Icon name="check" size={14} />
                </button>
                <button
                  type="button"
                  aria-label="Reject relationship"
                  onClick={() =>
                    decide.mutate(
                      { relId: r.id, decision: 'reject' },
                      { onError: (e) => toast(e.message, 'error') },
                    )
                  }
                  className="text-danger"
                >
                  <Icon name="close" size={14} />
                </button>
              </>
            ) : r.confidence === 'email_header' ? (
              <Badge tone="neutral">email header</Badge>
            ) : (
              <Badge tone="success">confirmed</Badge>
            )}
          </li>
        ))}
      </ul>
    </Section>
  )
}

const CLAIM_TONE: Record<string, Tone> = {
  contested: 'danger',
  asserted: 'warning',
  established: 'success',
}

const ROLE_LABEL: Record<string, string> = {
  contests: 'contests',
  refutes: 'refutes',
  supports: 'supports',
  asserts: 'asserts',
  cites_as_proof: 'cites as proof',
}

/** AI-proposed links from this document to claims in the case, awaiting a decision. */
function ClaimLinks({ review }: { review: Review }) {
  const decide = useEvidenceDecision(review.id)
  const toast = useToast()
  const act = (proposalId: number, decision: 'confirm' | 'dismiss') =>
    decide.mutate({ proposalId, decision }, { onError: (e) => toast(e.message, 'error') })
  return (
    <div className="mb-3 border-b border-line2 pb-2">
      <div className="mb-1 text-[10px] font-bold tracking-[.1em] text-muted uppercase">
        Claim links to confirm
      </div>
      <ul className="space-y-1.5">
        {review.evidence_proposals.map((p) => (
          <li key={p.proposal_id} className="flex items-start gap-2 text-[11.5px] text-ink2">
            <Badge tone={p.proposed_role === 'supports' ? 'neutral' : 'warning'}>
              {ROLE_LABEL[p.proposed_role] ?? p.proposed_role}
            </Badge>
            <span className="min-w-0 flex-1 leading-relaxed">
              {p.target_claim_text}
              {p.excerpt && (
                <span className="block text-[10.5px] text-muted italic">“{p.excerpt}”</span>
              )}
            </span>
            <button
              type="button"
              aria-label="Confirm claim link"
              onClick={() => act(p.proposal_id, 'confirm')}
              className="text-success"
            >
              <Icon name="check" size={14} />
            </button>
            <button
              type="button"
              aria-label="Dismiss claim link"
              onClick={() => act(p.proposal_id, 'dismiss')}
              className="text-danger"
            >
              <Icon name="close" size={14} />
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function Grounds({ review }: { review: Review }) {
  return (
    <Section
      id="review-grounds"
      boxed
      title="Grounds"
      meta={
        review.claims_status === 'ran' || review.grounds.length > 0
          ? `${review.grounds.length}`
          : review.claims_status.replace('_', ' ')
      }
    >
      {review.evidence_proposals.length > 0 && <ClaimLinks review={review} />}
      {review.grounds.length === 0 ? (
        <p className="text-muted">
          {review.claims_status === 'pending_triage'
            ? 'Extracted after routing to a case.'
            : 'No claims asserted.'}
        </p>
      ) : (
        <ul className="space-y-1.5">
          {review.grounds.map((g) => (
            <li key={g.id} className="flex items-start gap-2">
              <Icon name="balance" size={14} className="mt-0.5 text-muted" />
              <span className="min-w-0 flex-1 text-[11.5px] leading-relaxed text-ink2">
                {g.claim_text}
              </span>
              <Badge tone={CLAIM_TONE[g.status] ?? 'neutral'}>{g.status}</Badge>
              {g.is_precedent && <Badge tone="accent">⚖</Badge>}
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

function Actions({ review }: { review: Review }) {
  const set = useActionStatus(review.id)
  const toast = useToast()
  return (
    <Section
      boxed
      title="Detected actions"
      meta={review.actions.length ? `${review.actions.length}` : 'none'}
    >
      <ul className="space-y-1.5 text-[11.5px] text-ink2">
        {review.actions.map((a) => (
          <li
            key={a.id}
            className={`flex items-center gap-2 ${a.status !== 'open' ? 'opacity-60' : ''}`}
          >
            <Icon
              name={a.action_type === 'court_date' ? 'event' : 'timer'}
              size={14}
              className="text-muted"
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate">{a.title}</span>
              {a.due_date && (
                <span className="font-mono text-[10px] text-muted">
                  {formatShortDate(a.due_date)}
                </span>
              )}
            </span>
            {a.status === 'open' ? (
              <>
                <button
                  type="button"
                  onClick={() =>
                    set.mutate(
                      { itemId: a.id, status: 'completed' },
                      { onError: (e) => toast(e.message, 'error') },
                    )
                  }
                  className="text-[10px] text-tealink hover:underline"
                >
                  Done
                </button>
                <button
                  type="button"
                  onClick={() =>
                    set.mutate(
                      { itemId: a.id, status: 'dismissed' },
                      { onError: (e) => toast(e.message, 'error') },
                    )
                  }
                  className="text-[10px] text-muted hover:underline"
                >
                  Skip
                </button>
              </>
            ) : (
              <Badge>{a.status}</Badge>
            )}
          </li>
        ))}
      </ul>
    </Section>
  )
}

function CostSignals({ review }: { review: Review }) {
  return (
    <Section
      boxed
      title="Cost signal"
      meta={review.cost_signals.length ? `${review.cost_signals.length}` : 'none'}
    >
      <ul className="space-y-1">
        {review.cost_signals.map((c) => (
          <li key={c.id} className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate">{c.description ?? c.signal_type}</span>
            {c.amount != null && (
              <span className="font-mono text-tealink">€{c.amount.toLocaleString('en-GB')}</span>
            )}
            <Badge>{c.signal_type}</Badge>
          </li>
        ))}
      </ul>
    </Section>
  )
}

function Reactions({ review }: { review: Review }) {
  const react = useReact(review.id)
  const toast = useToast()
  const active = new Set(review.reactions.map((r) => r.reaction))
  const note = review.reactions.find((r) => r.notes)?.notes ?? ''
  return (
    <Section
      className="bg-panel2"
      title="Your reaction"
      meta="recalled by the AI later"
      dataAttr={{ 'data-reaction-bar': '' }}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        {REACTIONS.map(([key, glyph, label]) => (
          <button
            key={key}
            type="button"
            aria-pressed={active.has(key)}
            onClick={() =>
              react.mutate({ reaction: key }, { onError: (e) => toast(e.message, 'error') })
            }
            className={`rounded-full border px-2.5 py-1 text-[11px] ${active.has(key) ? 'border-accent/40 bg-accent/12 text-ink' : 'border-line text-muted hover:text-ink'}`}
          >
            {glyph} {label}
          </button>
        ))}
      </div>
      <form
        className="mt-2 flex gap-2"
        onSubmit={(e: FormEvent<HTMLFormElement>) => {
          e.preventDefault()
          const notes = String(new FormData(e.currentTarget).get('notes'))
          const reaction = (active.values().next().value ??
            'needs_proof') as Schemas['UserReactionType']
          react.mutate(
            { reaction, notes },
            { onSuccess: () => toast('Note saved'), onError: (err) => toast(err.message, 'error') },
          )
        }}
      >
        <input
          name="notes"
          defaultValue={note}
          placeholder="Note — captured as high-weight context for the AI…"
          aria-label="Reaction note"
          className={inputClass}
        />
        <Button size="sm" type="submit" variant="secondary" disabled={react.isPending}>
          Save
        </Button>
      </form>
    </Section>
  )
}
