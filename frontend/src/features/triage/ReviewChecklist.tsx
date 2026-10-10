import { useState } from 'react'

import type { Schemas } from '../../api/client'
import {
  type TriageBundle,
  useAcknowledgeContradiction,
  useAcknowledgeOcrUnverified,
} from '../../api/triage'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'
import { openReasons, reviewReasonLabel } from '../documents/reviewReasons'

type Review = Schemas['DocumentReview']

type Item = {
  key: string
  label: string
  /** Section the row scrolls to; rows that expand in place have none. */
  target?: string
  detail?: React.ReactNode
}

const MISSING_FIELDS: Record<string, string> = {
  missing_originator: 'originator',
  missing_sender: 'sender',
  missing_received_date: 'received date',
  missing_issued_date: 'issued date',
  issued_date_suspect: 'issued date',
}

/** The OCR cross-check stores up to 20 doubtful words a page; the row shows the first few. */
const MAX_WORDS_SHOWN = 8

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** Scroll a review section into view and flash it so the eye lands there. */
function jumpTo(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  el.classList.add('ring-2', 'ring-accent')
  window.setTimeout(() => el.classList.remove('ring-2', 'ring-accent'), 1200)
}

/** Everything the user still has to look at on this document, derived from what the pane shows. */
export function reviewItems(review: Review): Item[] {
  const items: Item[] = []
  const reasons = new Set(review.review_reasons)
  if (reasons.has('missing_case_id')) {
    items.push({ key: 'case', label: 'Assign a case', target: 'review-case' })
  }
  const flagged = review.metadata
    .filter((f) => f.confidence === 'low' || f.confidence === 'medium')
    .map((f) => f.label.toLowerCase())
  const missing = Object.entries(MISSING_FIELDS)
    .filter(([reason]) => reasons.has(reason))
    .map(([, name]) => name)
  const check = [...new Set([...missing, ...flagged])]
  if (check.length > 0) {
    items.push({
      key: 'metadata',
      label: `Check metadata: ${check.join(', ')}`,
      target: 'review-metadata',
    })
  }
  // Only edges *from* this document count, as on the backend; an incoming AI edge
  // belongs to (and is confirmed from) the other document.
  // Cover→enclosure edges follow the bundle layout; they are not the AI's call to confirm.
  const rels = review.relationships.filter(
    (r) => r.direction === 'out' && r.confidence === 'ai_detected' && r.rel_type !== 'encloses',
  ).length
  if (rels > 0) {
    items.push({
      key: 'relationships',
      label: `${plural(rels, 'relationship', 'relationships')} to confirm`,
      target: 'review-relationships',
    })
  }
  // Only proposals that contest or refute an existing claim hold the document in review.
  const links = review.evidence_proposals.filter(
    (p) => p.proposed_role === 'contests' || p.proposed_role === 'refutes',
  ).length
  if (links > 0) {
    items.push({
      key: 'claim-links',
      label: `${plural(links, 'claim link', 'claim links')} to confirm`,
      target: 'review-grounds',
    })
  }
  if (review.summary.bullets.length > 0 && !review.summary.approved_at) {
    items.push({
      key: 'summary',
      label: 'AI summary to approve',
      target: 'review-summary',
    })
  }
  if (reasons.has('contradiction_detected')) {
    items.push({
      key: 'contradiction',
      label: 'AI flagged a contradiction',
    })
  }
  const unverified = review.pipeline.ocr_unverified
  if (reasons.has('ocr_unverified') && unverified.length > 0) {
    items.push({
      key: 'ocr',
      label: `OCR text to check on ${unverified.length === 1 ? 'page' : 'pages'} ${unverified
        .map((p) => p.page)
        .join(', ')}`,
    })
  }
  return items
}

/** Other documents in the bundle that still have open items (⌘↵ confirms them all). */
function openSiblings(review: Review, bundle: TriageBundle) {
  return bundle.documents.filter(
    (d) => d.id !== review.id && (openReasons(d).length > 0 || d.summary_pending),
  )
}

/** "To review": what's left before this document, and its bundle, is ready to confirm. */
export function ReviewChecklist({ review, bundle }: { review: Review; bundle?: TriageBundle }) {
  const items = reviewItems(review)
  const siblings = bundle ? openSiblings(review, bundle) : []
  if (items.length === 0 && !bundle) return null

  if (items.length === 0) {
    const ready = siblings.length === 0
    return (
      <section
        aria-label="To review"
        className={`flex items-center gap-2 border-b border-line2 px-4.5 py-2.5 text-[11.5px] ${ready ? 'text-success' : 'text-warning'}`}
      >
        <Icon name={ready ? 'check_circle' : 'pending'} size={16} />
        {ready ? (
          <span>
            Ready to confirm <kbd className="font-mono text-[10px] text-muted">⌘↵</kbd>
          </span>
        ) : (
          <span>
            This document is clear, but{' '}
            {plural(siblings.length, 'other document', 'other documents')} in the bundle{' '}
            {siblings.length === 1 ? 'has' : 'have'} open items:{' '}
            {siblings.map((d) => d.title).join(', ')}
          </span>
        )}
      </section>
    )
  }

  return (
    <section aria-label="To review" className="border-b border-line2 bg-warning/5 px-4.5 py-2.5">
      <h4 className="mb-1.5 text-[9px] font-bold tracking-[.1em] text-warning uppercase">
        To review <span className="font-mono">{items.length}</span>
      </h4>
      <ul className="space-y-1">
        {items.map((item) => (
          <ChecklistRow key={item.key} item={item} review={review} />
        ))}
      </ul>
      {siblings.length > 0 && (
        <p className="mt-1.5 text-[10.5px] text-muted">
          + {plural(siblings.length, 'other document', 'other documents')} in this bundle{' '}
          {siblings.length === 1 ? 'has' : 'have'} open items
        </p>
      )}
    </section>
  )
}

function ChecklistRow({ item, review }: { item: Item; review: Review }) {
  const ackContradiction = useAcknowledgeContradiction(review.id)
  const ackOcr = useAcknowledgeOcrUnverified(review.id)
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const contradiction = item.key === 'contradiction'
  const ocr = item.key === 'ocr'
  const ack = ocr ? ackOcr : ackContradiction
  return (
    <li className="text-[11.5px]">
      <div className="flex items-center gap-2">
        <Icon name="priority_high" size={14} className="text-warning" />
        <span className="flex-1">{item.label}</span>
        {contradiction || ocr ? (
          <Button variant="secondary" size="sm" onClick={() => setOpen((v) => !v)}>
            {open ? 'Hide' : 'Details'}
          </Button>
        ) : (
          <button
            type="button"
            onClick={() => item.target && jumpTo(item.target)}
            className="text-[10.5px] text-tealink hover:underline"
          >
            Go to →
          </button>
        )}
      </div>
      {ocr && open && (
        <div className="mt-1.5 ml-5.5 rounded-md border border-line2 bg-card p-2">
          <p className="text-muted">
            A second OCR could not find these words on the scan. Check them against the original;
            the text below may be misread.
          </p>
          <ul className="mt-1 space-y-1 text-ink2">
            {review.pipeline.ocr_unverified.map((p) => (
              <li key={p.page}>
                <span className="font-mono text-[10.5px] text-muted">p{p.page}</span>{' '}
                {p.words.slice(0, MAX_WORDS_SHOWN).join(', ')}
                {p.words.length > MAX_WORDS_SHOWN && ` +${p.words.length - MAX_WORDS_SHOWN} more`}
              </li>
            ))}
          </ul>
          <Button
            size="sm"
            className="mt-2"
            disabled={ack.isPending}
            onClick={() => ack.mutate(undefined, { onError: (e) => toast(e.message, 'error') })}
          >
            Checked
          </Button>
        </div>
      )}
      {contradiction && open && (
        <div className="mt-1.5 ml-5.5 rounded-md border border-line2 bg-card p-2">
          {review.contradiction_notes.length > 0 ? (
            <ul className="list-disc space-y-1 pl-4 text-ink2">
              {review.contradiction_notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : (
            <p className="text-muted">{reviewReasonLabel('contradiction_detected')}</p>
          )}
          <Button
            size="sm"
            className="mt-2"
            disabled={ack.isPending}
            onClick={() => ack.mutate(undefined, { onError: (e) => toast(e.message, 'error') })}
          >
            Acknowledge
          </Button>
        </div>
      )}
    </li>
  )
}
