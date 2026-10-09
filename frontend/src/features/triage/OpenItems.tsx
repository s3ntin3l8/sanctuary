import { useQueries } from '@tanstack/react-query'

import { documentReviewOptions } from '../../api/triage'
import type { Schemas } from '../../api/client'
import type { TriageBundle } from '../../api/triage'
import { Button } from '../../ui/Button'
import { openReasons, reviewReasonLabel } from '../documents/reviewReasons'
import { reviewItems } from './ReviewChecklist'

type Doc = TriageBundle['documents'][number]

/** The bundle's documents that still have something open, per the triage feed. */
export function docsWithOpenItems(bundle: TriageBundle): Doc[] {
  return bundle.documents.filter((d) => openReasons(d).length > 0 || d.summary_pending)
}

/**
 * "Still open in this bundle": per document, the same rows as the in-pane checklist
 * (`reviewItems`), with a Review button that jumps to it. Confirming stays allowed.
 */
export function OpenItems({
  bundle,
  onReview,
}: {
  bundle: TriageBundle
  onReview: (docId: number) => void
}) {
  const docs = docsWithOpenItems(bundle)
  // One batched lookup for the whole bundle (the open document is already cached).
  const reviews = useQueries({ queries: docs.map((d) => documentReviewOptions(d.id)) })
  // The feed can say "open" while the live review has nothing to show (a stale
  // reason); such documents drop out, and so does the box when none are left.
  const rows = docs
    .map((doc, i) => ({ doc, labels: openLabels(doc, reviews[i]?.data) }))
    .filter((r) => r.labels.length > 0)
  if (rows.length === 0) return null
  return (
    <div role="note" className="rounded-xl border border-warning/40 bg-warning/10 p-3 text-[11px]">
      <p className="font-semibold text-warning">Still open in this bundle</p>
      <ul className="mt-1.5 space-y-1.5 text-ink2">
        {rows.map(({ doc, labels }) => (
          <li key={doc.id} className="flex items-start gap-2">
            <span className="min-w-0 flex-1">
              <span className="block truncate font-semibold">{doc.title}</span>
              <span className="block text-muted">{labels.join(' · ')}</span>
            </span>
            <Button variant="secondary" size="sm" onClick={() => onReview(doc.id)}>
              Review
            </Button>
          </li>
        ))}
      </ul>
      <p className="mt-1.5 text-muted">You can still confirm; fix them later from the case.</p>
    </div>
  )
}

/**
 * Until the full review loads, fall back to what the feed already knows. The
 * "assign a case" row is dropped on purpose: the confirm dialog assigns the case.
 */
function openLabels(doc: Doc, review: Schemas['DocumentReview'] | undefined): string[] {
  if (review) {
    return reviewItems(review)
      .filter((i) => i.key !== 'case')
      .map((i) => i.label)
  }
  return [
    ...openReasons(doc).map(reviewReasonLabel),
    ...(doc.summary_pending ? ['AI summary to approve'] : []),
  ]
}
