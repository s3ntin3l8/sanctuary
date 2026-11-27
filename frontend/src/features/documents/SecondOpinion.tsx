import { Link } from 'react-router'

import { type ReviewFinding, useFindingDecision } from '../../api/reviewFindings'
import { formatShortDate } from '../../time'
import { AgentBadge } from '../../ui/AgentBadge'
import { Badge, type Tone } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'

const VERDICT: Record<ReviewFinding['verdict'], [Tone, string]> = {
  disagree: ['danger', 'disagrees'],
  uncertain: ['warning', 'uncertain'],
  agree: ['success', 'agrees'],
}

const STATUS: Partial<Record<ReviewFinding['status'], [Tone, string]>> = {
  confirmed: ['success', 'confirmed'],
  dismissed: ['neutral', 'dismissed'],
  stale: ['neutral', 'stale'],
}

const SUMMARY_LABEL: Record<string, string> = {
  legal_significance: 'Legal',
  required_action: 'Action',
  financial_impact: 'Finance',
}

/** Fields whose values are enum keys (`third_party`, `pre_trial`) and read better spaced out. */
const ENUM_FIELDS = new Set([
  'significance_tier',
  'document_type',
  'originator_type',
  'status',
  'detected_status',
  'client_role',
])

/** One AI or proposed value, readable: plain text, a summary's bullets, or compact JSON. */
export function FindingValue({ value, isEnum = false }: { value: unknown; isEnum?: boolean }) {
  if (value === null || value === undefined || value === '') {
    return <span className="text-muted">—</span>
  }
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return <span>{isEnum ? String(value).replace(/_/g, ' ') : String(value)}</span>
  }
  if (typeof value === 'object' && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>).filter(
      ([k, v]) => v !== null && v !== undefined && k !== 'content_sha256',
    )
    if (entries.length === 0) return <span className="text-muted">—</span>
    return (
      <ul className="space-y-0.5">
        {entries.map(([k, v]) => (
          <li key={k}>
            <span className="font-mono text-[10px] text-muted">
              {SUMMARY_LABEL[k] ?? k.replace(/_/g, ' ')}
            </span>{' '}
            {typeof v === 'object' ? JSON.stringify(v) : String(v)}
          </li>
        ))}
      </ul>
    )
  }
  return <span className="font-mono text-[10.5px]">{JSON.stringify(value)}</span>
}

function ReviewerBadge({ finding }: { finding: ReviewFinding }) {
  if (finding.reviewer_kind === 'verifier') {
    return (
      <Badge tone="accent" className="normal-case" mono>
        <Icon name="verified" size={11} />
        via verifier
      </Badge>
    )
  }
  return finding.agent_token_name ? (
    <AgentBadge name={finding.agent_token_name} />
  ) : (
    <Badge tone="accent" className="normal-case" mono>
      <Icon name="smart_toy" size={11} />
      via agent
    </Badge>
  )
}

function FindingRow({
  finding,
  showDocument,
  canResolve,
}: {
  finding: ReviewFinding
  showDocument: boolean
  canResolve: boolean
}) {
  const decide = useFindingDecision()
  const toast = useToast()
  const [verdictTone, verdictLabel] = VERDICT[finding.verdict]
  const status = STATUS[finding.status]
  const pending = finding.status === 'pending'
  const isEnum = ENUM_FIELDS.has(finding.field ?? '')
  const act = (decision: 'confirm' | 'dismiss') =>
    decide.mutate(
      { id: finding.id, decision },
      {
        onSuccess: () => toast(decision === 'confirm' ? 'Finding confirmed' : 'Finding dismissed'),
        onError: (e) => toast(e.message, 'error'),
      },
    )
  return (
    <li
      aria-label={`${finding.target_label}: ${verdictLabel}`}
      className={`rounded-lg border px-2.5 py-2 text-[11.5px] ${
        finding.status === 'stale'
          ? 'border-dashed border-line3 bg-transparent'
          : pending && finding.verdict === 'disagree'
            ? 'border-danger/40 bg-danger/5'
            : 'border-line bg-card'
      }`}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-semibold text-ink">{finding.target_label}</span>
        <Badge tone={verdictTone}>{verdictLabel}</Badge>
        {status && <Badge tone={status[0]}>{status[1]}</Badge>}
        <ReviewerBadge finding={finding} />
        <span className="ml-auto font-mono text-[10px] text-muted">
          {formatShortDate(finding.created_at)}
        </span>
      </div>
      {showDocument && finding.document_id !== null && (
        <Link
          to={`/document/${finding.document_id}`}
          className="mt-0.5 block truncate text-[10.5px] text-tealink hover:underline"
        >
          {finding.document_title}
        </Link>
      )}
      <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-ink2">
        <dt className="text-[10px] font-bold tracking-[.08em] text-muted uppercase">AI</dt>
        <dd className="min-w-0 break-words">
          <FindingValue value={finding.ai_value} isEnum={isEnum} />
        </dd>
        {finding.verdict === 'disagree' && (
          <>
            <dt className="text-[10px] font-bold tracking-[.08em] text-muted uppercase">
              Proposed
            </dt>
            <dd className="min-w-0 break-words">
              <FindingValue value={finding.proposed_value} isEnum={isEnum} />
            </dd>
          </>
        )}
      </dl>
      <p className="mt-1.5 leading-relaxed text-ink2">{finding.rationale}</p>
      {finding.cited_excerpt && (
        <blockquote className="mt-1 border-l-2 border-line3 pl-2 text-[10.5px] text-muted italic">
          “{finding.cited_excerpt}”
        </blockquote>
      )}
      {pending && canResolve && (
        <div className="mt-2 flex gap-1.5">
          <Button size="sm" disabled={decide.isPending} onClick={() => act('confirm')}>
            Confirm
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={decide.isPending}
            onClick={() => act('dismiss')}
          >
            Dismiss
          </Button>
        </div>
      )}
    </li>
  )
}

/** Findings of an agent or the verifier on AI output; pending ones can be confirmed or dismissed. */
export function FindingList({
  findings,
  showDocument = false,
  canResolve = true,
}: {
  findings: ReviewFinding[]
  showDocument?: boolean
  canResolve?: boolean
}) {
  return (
    <ul className="space-y-1.5">
      {findings.map((f) => (
        <FindingRow key={f.id} finding={f} showDocument={showDocument} canResolve={canResolve} />
      ))}
    </ul>
  )
}
