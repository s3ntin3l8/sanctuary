const LABELS: Record<string, string> = {
  missing_case_id: 'no case assigned',
  missing_originator: 'originator missing',
  missing_sender: 'sender missing',
  missing_received_date: 'received date missing',
  missing_issued_date: 'issued date missing',
  low_confidence: 'metadata to check',
  unresolved_relationship: 'relationships to confirm',
  contests_existing_claim: 'claim links to confirm',
  contradiction_detected: 'contradiction to review',
}

/** Review reasons that need a human to fix metadata (not just a ratification click). */
const IGNORABLE = new Set(['pending_confirmation', 'missing_parent'])

export function reviewReasonLabel(reason: string): string {
  return LABELS[reason] ?? reason.replace(/_/g, ' ')
}

export function actionableReasons(reasons: string[]): string[] {
  return reasons.filter((r) => !IGNORABLE.has(r))
}
