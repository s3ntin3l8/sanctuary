import { useDocumentReview } from '../../api/triage'
import type { TriageBundle } from '../../api/triage'
import { Button } from '../../ui/Button'
import { actionableReasons, reviewReasonLabel } from '../documents/reviewReasons'
import { reviewItems } from './ReviewChecklist'

type Doc = TriageBundle['documents'][number]

/** Reasons worth listing while confirming; the modal itself assigns the case. */
const modalReasons = (d: Doc) =>
  actionableReasons(d.review_reasons).filter((r) => r !== 'missing_case_id')

/** The bundle's documents that still have something open, per the triage feed. */
export function docsWithOpenItems(bundle: TriageBundle): Doc[] {
  return bundle.documents.filter((d) => modalReasons(d).length > 0 || d.summary_pending)
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
  if (docs.length === 0) return null
  return (
    <div role="note" className="rounded-xl border border-warning/40 bg-warning/10 p-3 text-[11px]">
      <p className="font-semibold text-warning">Still open in this bundle</p>
      <ul className="mt-1.5 space-y-1.5 text-ink2">
        {docs.map((d) => (
          <DocOpenItems key={d.id} doc={d} onReview={() => onReview(d.id)} />
        ))}
      </ul>
      <p className="mt-1.5 text-muted">You can still confirm; fix them later from the case.</p>
    </div>
  )
}

function DocOpenItems({ doc, onReview }: { doc: Doc; onReview: () => void }) {
  const review = useDocumentReview(doc.id).data
  // Until the full review loads, fall back to what the feed already knows.
  const labels = review
    ? reviewItems(review)
        .filter((i) => i.key !== 'case')
        .map((i) => i.label)
    : [
        ...modalReasons(doc).map(reviewReasonLabel),
        ...(doc.summary_pending ? ['AI summary to approve'] : []),
      ]
  if (review && labels.length === 0) return null
  return (
    <li className="flex items-start gap-2">
      <span className="min-w-0 flex-1">
        <span className="block truncate font-semibold">{doc.title}</span>
        <span className="block text-muted">{labels.join(' · ')}</span>
      </span>
      <Button variant="secondary" size="sm" onClick={onReview}>
        Review
      </Button>
    </li>
  )
}
