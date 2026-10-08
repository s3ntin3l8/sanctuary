const LABELS: Record<string, string> = {
  missing_case_id: 'no case',
  missing_originator: 'no originator',
  missing_sender: 'no sender',
  missing_received_date: 'no received date',
  missing_issued_date: 'no issued date',
  low_confidence: 'low-confidence metadata',
  unresolved_relationship: 'unresolved relationship',
  contests_existing_claim: 'contests an existing claim',
  contradiction_detected: 'contradiction detected',
}

/** Review reasons that need a human to fix metadata (not just a ratification click). */
const IGNORABLE = new Set(['pending_confirmation', 'missing_parent'])

export function reviewReasonLabel(reason: string): string {
  return LABELS[reason] ?? reason.replace(/_/g, ' ')
}

export function actionableReasons(reasons: string[]): string[] {
  return reasons.filter((r) => !IGNORABLE.has(r))
}
