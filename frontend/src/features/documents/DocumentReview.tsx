import { type FormEvent, useState } from 'react'
import { Link } from 'react-router'

import type { Schemas } from '../../api/client'
import {
  useActionStatus,
  useDocumentReview,
  useDraftDecision,
  useReact,
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
  onReassign?: (review: Review) => void
  onOpenHud?: (docId: number) => void
}

/** What the full-screen HUD wires into the passage spine. */
export type PassageHooks = {
  activePassageId: string | null
  onFocus: (passageId: string) => void
  onPin: (passageId: string) => void
  onAskAi: (passageText: string) => void
}

/** The intelligence panel for one document in the triage inline review. */
export function DocumentReview({ docId, onReassign, onOpenHud }: Props) {
  const query = useDocumentReview(docId)
  const review = query.data
  if (!review) return <QueryState error={query.error} pending={query.isPending} />
  return (
    <div className="space-y-3 text-[12px]">
      <Header review={review} onOpenHud={onOpenHud} />
      <ReviewSections review={review} onReassign={onReassign} />
    </div>
  )
}

/** The rail sections shared by the triage review and the HUD (`passages` adds spine behaviour). */
export function ReviewSections({
  review,
  onReassign,
  passages,
  singleColumn = false,
}: {
  review: Review
  onReassign?: (review: Review) => void
  passages?: PassageHooks
  singleColumn?: boolean
}) {
  return (
    <>
      <Pipeline review={review} />
      <CaseAndProceeding review={review} onReassign={onReassign} />
      <Metadata review={review} />
      <Summary review={review} />
      <Passages review={review} hooks={passages} />
      <div className={singleColumn ? 'space-y-3' : 'grid grid-cols-2 gap-3'}>
        <Relationships review={review} />
        <Grounds review={review} />
        <Actions review={review} />
        <CostSignals review={review} />
      </div>
      <Reactions review={review} />
    </>
  )
}

function Section({
  title,
  meta,
  children,
  action,
}: {
  title: string
  meta?: string
  children: React.ReactNode
  action?: React.ReactNode
}) {
  return (
    <section className="rounded-xl border border-line bg-card2 p-3">
      <header className="mb-2 flex items-center gap-2">
        <h4 className="text-[9.5px] font-extrabold tracking-[.12em] text-muted uppercase">
          {title}
        </h4>
        {meta && <span className="font-mono text-[10px] text-muted">{meta}</span>}
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
    <header className="flex items-start gap-2">
      <span
        className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${ORIGINATOR_COLOR[review.originator_type]}`}
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
        <div className="font-mono text-[10px] text-muted">
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
        <Button
          variant="secondary"
          className="px-2.5 py-1 text-[11px]"
          onClick={() => onOpenHud(review.id)}
        >
          <Icon name="open_in_full" size={14} /> HUD
        </Button>
      )}
    </header>
  )
}

function Pipeline({ review }: { review: Review }) {
  const retry = useStageRetry(review.id)
  const toast = useToast()
  const stages = review.pipeline.stages
  const done = stages.filter((s) => s.status === 'completed' || s.status === 'skipped').length
  const failed = stages.filter((s) => s.status === 'failed')
  return (
    <Section
      title="Pipeline"
      meta={`${done}/${stages.length} · ${review.pipeline.state}`}
      action={
        failed.length > 0 && (
          <Button
            variant="secondary"
            className="px-2 py-0.5 text-[10px]"
            disabled={retry.isPending}
            onClick={() => retry.mutate('all', { onError: (e) => toast(e.message, 'error') })}
          >
            Retry all
          </Button>
        )
      }
    >
      <div className="flex flex-wrap items-center gap-1.5">
        {stages.map((s) => (
          <span
            key={s.key}
            title={`${s.label}: ${s.status ?? 'queued'}${s.error ? `\n${s.error}` : ''}`}
            className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[10px] ${
              s.status === 'failed'
                ? 'border-danger/40 text-danger'
                : s.status === 'running' || s.status === 'retrying'
                  ? 'border-warning/40 text-warning'
                  : s.status === 'completed'
                    ? 'border-accent/30 text-tealink'
                    : 'border-line text-muted'
            }`}
          >
            {s.label}
            {s.status === 'failed' && (
              <button
                type="button"
                aria-label={`Retry ${s.label}`}
                onClick={() => retry.mutate(s.key, { onError: (e) => toast(e.message, 'error') })}
                className="hover:text-ink"
              >
                <Icon name="replay" size={12} />
              </button>
            )}
          </span>
        ))}
      </div>
      {failed[0]?.error && (
        <p className="mt-1 truncate font-mono text-[10px] text-danger" title={failed[0].error}>
          {failed[0].error}
        </p>
      )}
    </Section>
  )
}

function CaseAndProceeding({
  review,
  onReassign,
}: {
  review: Review
  onReassign?: (r: Review) => void
}) {
  const draft = useDraftDecision()
  const toast = useToast()
  return (
    <div className="grid grid-cols-2 gap-3">
      <Section
        title="Case"
        action={
          onReassign && (
            <button
              type="button"
              onClick={() => onReassign(review)}
              className="text-[10px] text-tealink hover:underline"
            >
              change
            </button>
          )
        }
      >
        {review.case ? (
          <div>
            <div className="flex items-center gap-2">
              <a
                href={`/cases/${review.case.id}`}
                className="font-mono text-[12px] font-semibold text-tealink hover:underline"
              >
                {review.case.id}
              </a>
              {review.case.is_draft && <Badge tone="warning">draft</Badge>}
            </div>
            <div className="truncate text-[11px] text-muted">{review.case.title}</div>
            {review.case.is_draft && (
              <div className="mt-2 flex gap-1">
                <Button
                  className="px-2 py-0.5 text-[10px]"
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
                  variant="secondary"
                  className="px-2 py-0.5 text-[10px] text-danger"
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
          </div>
        ) : (
          <span className="text-muted">Unassigned · in triage</span>
        )}
      </Section>
      <Section title="Proceeding">
        {review.proceeding ? (
          <div>
            <div className="flex items-center gap-2 font-mono text-[12px] font-semibold">
              {review.proceeding.az_court ?? '—'}
              <Badge tone="accent">{review.proceeding.court_level.toUpperCase()}</Badge>
            </div>
            <div className="truncate text-[11px] text-muted">{review.proceeding.court_name}</div>
          </div>
        ) : (
          <span className="text-muted">
            {review.az_court ? `AZ ${review.az_court} · no proceeding yet` : 'No proceeding'}
          </span>
        )}
      </Section>
    </div>
  )
}

const CONF_CLASS: Record<string, string> = {
  high: 'border-l-success',
  medium: 'border-l-warning bg-warning/5',
  low: 'border-l-danger bg-danger/5',
}

function Metadata({ review }: { review: Review }) {
  const [editing, setEditing] = useState(false)
  const flagged = review.metadata.filter(
    (f) => f.confidence === 'low' || f.confidence === 'medium',
  ).length
  return (
    <Section
      title="Metadata review"
      meta={
        flagged > 0
          ? `${flagged} need review`
          : review.needs_review
            ? review.review_reasons.join(', ')
            : 'all confirmed'
      }
      action={
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="text-[10px] text-tealink hover:underline"
        >
          Edit
        </button>
      }
    >
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
        {review.metadata.map((f) => (
          <div
            key={f.field}
            className={`rounded-md border border-line2 border-l-2 px-2 py-1 ${CONF_CLASS[f.confidence ?? ''] ?? 'border-l-line3'}`}
          >
            <dt className="text-[9px] font-bold tracking-[.1em] text-muted uppercase">{f.label}</dt>
            <dd className="truncate font-mono text-[11px]" title={f.value ?? ''}>
              {f.value ?? <span className="text-muted2">—</span>}
            </dd>
          </div>
        ))}
      </dl>
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
        <Field label="AZ" hint="Comes from the proceeding.">
          <div className={`${inputClass} text-muted`}>{review.az_court ?? '—'}</div>
        </Field>
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
  return (
    <Section
      title="AI summary"
      meta={
        s.approved_at
          ? `approved ${formatIsoDate(s.approved_at)}`
          : s.bullets.length
            ? 'generated'
            : (s.enrich_status ?? 'pending')
      }
      action={
        s.bullets.length > 0 && !s.approved_at ? (
          <span className="flex gap-1">
            <Button
              className="px-2 py-0.5 text-[10px]"
              disabled={act.isPending}
              onClick={() => act.mutate('approve', { onError: (e) => toast(e.message, 'error') })}
            >
              Approve
            </Button>
            <Button
              variant="secondary"
              className="px-2 py-0.5 text-[10px]"
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
          </span>
        ) : null
      }
    >
      {s.bullets.length === 0 ? (
        <p className="text-muted">
          {s.enrich_status === 'failed'
            ? 'Enrichment failed — retry the Enrich stage.'
            : 'No summary yet.'}
        </p>
      ) : (
        <ul className="space-y-1">
          {s.bullets.map((b) => (
            <li key={b.kind} className="flex gap-2">
              <Badge tone={BULLET_TONE[b.kind]}>{b.kind}</Badge>
              <span className="leading-relaxed">{b.text}</span>
            </li>
          ))}
        </ul>
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
      <ul className="space-y-2">
        {review.key_passages.map((p) => {
          const active = hooks?.activePassageId === p.id
          const body = (
            <>
              <p className="leading-relaxed">“{p.text}”</p>
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
              className={`rounded-r border-l-2 pl-2 ${PASSAGE_TONE[p.kind ?? 'neutral'] ?? 'border-line3'} ${active ? 'bg-accent/8' : ''}`}
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
  return (
    <Section
      title="Relationships"
      meta={review.relationships.length ? `${review.relationships.length}` : 'none'}
    >
      <ul className="space-y-1">
        {review.relationships.map((r) => (
          <li key={r.id} className="flex items-center gap-2">
            <span className="w-4 text-center font-mono text-muted">
              {REL_GLYPH[r.rel_type] ?? '·'}
            </span>
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

function Grounds({ review }: { review: Review }) {
  return (
    <Section
      title="Grounds"
      meta={
        review.claims_status === 'ran'
          ? `${review.grounds.length}`
          : review.claims_status.replace('_', ' ')
      }
    >
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
              <span className="min-w-0 flex-1 leading-relaxed">{g.claim_text}</span>
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
      title="Detected actions"
      meta={review.actions.length ? `${review.actions.length}` : 'none'}
    >
      <ul className="space-y-1.5">
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
    <Section title="Your reaction" meta="recalled by the AI later">
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
        <Button
          type="submit"
          variant="secondary"
          className="px-3 py-1 text-[11px]"
          disabled={react.isPending}
        >
          Save
        </Button>
      </form>
    </Section>
  )
}
