const LABELS: Record<string, string> = {
  missing_case_id: 'no case assigned',
  missing_originator: 'originator missing',
  missing_sender: 'sender missing',
  missing_received_date: 'received date missing',
  missing_issued_date: 'issued date missing',
  issued_date_suspect: 'issued date looks wrong',
  low_confidence: 'metadata to check',
  unresolved_relationship: 'relationships to confirm',
  contests_existing_claim: 'claim links to confirm',
  contradiction_detected: 'contradiction to review',
}

/** Reasons that never hold a document in review (the confirm click / enclosure without parent). */
const IGNORABLE = new Set(['pending_confirmation', 'missing_parent'])

export function reviewReasonLabel(reason: string): string {
  return LABELS[reason] ?? reason.replace(/_/g, ' ')
}

export function actionableReasons(reasons: string[]): string[] {
  return reasons.filter((r) => !IGNORABLE.has(r))
}

/** What is still open on a bundle, as short phrases (review reasons + summaries awaiting approval). */
export function bundleOpenParts(b: {
  open_review_reasons: string[]
  summaries_pending: number
}): string[] {
  return [
    ...b.open_review_reasons.map(reviewReasonLabel),
    ...(b.summaries_pending > 0
      ? [`${b.summaries_pending} ${b.summaries_pending === 1 ? 'summary' : 'summaries'} to approve`]
      : []),
  ]
}

/**
 * What a bundle document still has open before the bundle can be confirmed. The
 * case is not one of them: confirming assigns it.
 */
export function openReasons(doc: { review_reasons: string[] }): string[] {
  return actionableReasons(doc.review_reasons).filter((r) => r !== 'missing_case_id')
}
