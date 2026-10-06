import { useState } from 'react'
import { Link } from 'react-router'

import {
  type CaseDetail,
  type ClaimView,
  type TruthMapFilter,
  useBatchMerge,
  useClaimPrecedent,
  useClaimStatus,
  useDismissClaim,
  useFindDuplicates,
  useProposalDecision,
  useTruthMap,
} from '../../../api/caseDetail'
import { formatShortDate } from '../../../format'
import { Badge, type Tone } from '../../../ui/Badge'
import { Button } from '../../../ui/Button'
import { Chip } from '../../../ui/Chip'
import { Icon } from '../../../ui/Icon'
import { QueryState } from '../../../ui/QueryState'
import { useToast } from '../../../ui/toast'
import { ORIGINATOR_COLOR } from '../../documents/DocumentReview'

const STATUS_TONE: Record<string, Tone> = {
  asserted: 'warning',
  contested: 'danger',
  needs_proof: 'warning',
  established: 'success',
  refuted: 'neutral',
}
const ROLE_GLYPH: Record<string, string> = {
  asserts: '→',
  supports: '＋',
  refutes: '✕',
  concedes: '✓',
  disputes: '≠',
}
const REACTION_GLYPH: Record<string, string> = {
  lies: '🚩',
  true: '✅',
  needs_proof: '🔍',
  precedent: '⚖️',
}

/** The Truth Map: every atomic claim in the case with its evidence chain. */
export function TruthMapTab({ detail }: { detail: CaseDetail }) {
  const toast = useToast()
  const [filter, setFilter] = useState<TruthMapFilter>('open')
  const query = useTruthMap(detail.id, filter, true)
  const batch = useBatchMerge(detail.id)
  const decide = useProposalDecision(detail.id)
  const find = useFindDuplicates(detail.id)
  const tm = query.data
  if (!tm) return <QueryState error={query.error} pending={query.isPending} />
  const job = tm.dedup_job
  const onError = (e: Error) => toast(e.message, 'error')
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-5 text-[12px]" id="truthmap-panel">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {(['open', 'established', 'refuted', 'all'] as const).map((f) => (
          <Chip key={f} active={filter === f} onClick={() => setFilter(f)}>
            {f}
          </Chip>
        ))}
        <span className="font-mono text-[11px] text-muted">{tm.open_claim_count} open</span>
        {detail.can_edit && (
          <span className="ml-auto flex items-center gap-2">
            {job?.status === 'running' ? (
              <span className="font-mono text-[11px] text-muted">
                scanning {job.processed}/{job.total}…
              </span>
            ) : (
              <>
                {job?.status === 'done' && (
                  <span className="font-mono text-[11px] text-muted">
                    last scan: {job.proposals} proposals
                  </span>
                )}
                {job?.status === 'failed' && (
                  <span className="font-mono text-[11px] text-danger">scan failed</span>
                )}
                <Button
                  variant="secondary"
                  className="px-2.5 py-1 text-[11px]"
                  disabled={find.isPending}
                  onClick={() => find.mutate(undefined, { onError })}
                >
                  <Icon name="join_inner" size={13} /> Find duplicates
                </Button>
              </>
            )}
          </span>
        )}
      </div>

      {tm.pending_merges.length > 0 && (
        <section className="mb-4 rounded-xl border border-warning/40 bg-warning/5 p-3">
          <header className="mb-2 flex items-center gap-2">
            <h3 className="text-[10px] font-bold tracking-[.12em] text-warning uppercase">
              Pending merges · {tm.pending_merges.length}
            </h3>
            {detail.can_edit && (
              <span className="ml-auto flex gap-1.5">
                <Button
                  className="px-2 py-1 text-[11px]"
                  onClick={() => batch.mutate('confirm', { onError })}
                >
                  Merge all
                </Button>
                <Button
                  variant="secondary"
                  className="px-2 py-1 text-[11px]"
                  onClick={() => batch.mutate('dismiss', { onError })}
                >
                  Dismiss all
                </Button>
              </span>
            )}
          </header>
          <ul className="space-y-2">
            {tm.pending_merges.map((m) => (
              <li key={m.proposal_id} className="rounded-lg border border-line bg-card p-2.5">
                <p>“{m.new_claim_text}”</p>
                <p className="text-muted">≈ “{m.existing_claim_text}”</p>
                {m.rationale && <p className="mt-1 text-[11px] text-muted">{m.rationale}</p>}
                {detail.can_edit && (
                  <div className="mt-1.5 flex gap-1.5">
                    <Button
                      className="px-2 py-0.5 text-[11px]"
                      onClick={() =>
                        decide.mutate(
                          { kind: 'merge', proposalId: m.proposal_id, decision: 'confirm' },
                          { onError },
                        )
                      }
                    >
                      Merge
                    </Button>
                    <Button
                      variant="secondary"
                      className="px-2 py-0.5 text-[11px]"
                      onClick={() =>
                        decide.mutate(
                          { kind: 'merge', proposalId: m.proposal_id, decision: 'dismiss' },
                          { onError },
                        )
                      }
                    >
                      Keep separate
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {tm.pending_evidence.length > 0 && (
        <section className="mb-4 rounded-xl border border-accent/40 bg-accent/5 p-3">
          <h3 className="mb-2 text-[10px] font-bold tracking-[.12em] text-accent uppercase">
            Pending cross-document stances · {tm.pending_evidence.length}
          </h3>
          <ul className="space-y-2">
            {tm.pending_evidence.map((p) => (
              <li key={p.proposal_id} className="rounded-lg border border-line bg-card p-2.5">
                <p>
                  <Badge tone={p.proposed_role === 'refutes' ? 'danger' : 'success'}>
                    {p.proposed_role}
                  </Badge>{' '}
                  “{p.target_claim_text}”
                </p>
                <p className="text-muted">
                  from{' '}
                  <Link
                    to={`/document/${p.source_document_id}`}
                    className="text-tealink hover:underline"
                  >
                    {p.source_document_title ?? `#${p.source_document_id}`}
                  </Link>
                  {p.excerpt ? ` — “${p.excerpt}”` : ''}
                </p>
                {detail.can_edit && (
                  <div className="mt-1.5 flex gap-1.5">
                    <Button
                      className="px-2 py-0.5 text-[11px]"
                      onClick={() =>
                        decide.mutate(
                          { kind: 'evidence', proposalId: p.proposal_id, decision: 'confirm' },
                          { onError },
                        )
                      }
                    >
                      Confirm
                    </Button>
                    <Button
                      variant="secondary"
                      className="px-2 py-0.5 text-[11px]"
                      onClick={() =>
                        decide.mutate(
                          { kind: 'evidence', proposalId: p.proposal_id, decision: 'dismiss' },
                          { onError },
                        )
                      }
                    >
                      Dismiss
                    </Button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {tm.groups.length === 0 && (
        <p className="text-muted">
          {tm.pipeline_active_doc_count > 0
            ? `No claims yet — ${tm.pipeline_active_doc_count} documents are still being analysed.`
            : 'No claims match this filter.'}
        </p>
      )}
      {tm.groups.map((g) => (
        <section key={g.status} className="mb-5">
          <h3 className="mb-2 flex items-center gap-2 text-[10px] font-bold tracking-[.12em] text-muted uppercase">
            <Badge tone={STATUS_TONE[g.status] ?? 'neutral'}>{g.status.replace('_', ' ')}</Badge>
            <span className="font-mono normal-case">{g.claims.length}</span>
          </h3>
          <ul className="space-y-2">
            {g.claims.map((c) => (
              <ClaimCard key={c.id} claim={c} caseId={detail.id} canEdit={detail.can_edit} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

function ClaimCard({
  claim,
  caseId,
  canEdit,
}: {
  claim: ClaimView
  caseId: string
  canEdit: boolean
}) {
  const toast = useToast()
  const setStatus = useClaimStatus(caseId)
  const precedent = useClaimPrecedent(caseId)
  const dismiss = useDismissClaim(caseId)
  const [open, setOpen] = useState(false)
  const onError = (e: Error) => toast(e.message, 'error')
  return (
    <li id={`claim-card-${claim.id}`} className="rounded-xl border border-line bg-card p-3">
      <div className="flex items-start gap-2">
        <Badge tone={STATUS_TONE[claim.status] ?? 'neutral'}>
          {claim.status.replace('_', ' ')}
        </Badge>
        <p className="min-w-0 flex-1 leading-relaxed">{claim.claim_text}</p>
        {claim.is_precedent && <span title="Precedent">⚖️</span>}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 font-mono text-[10px] text-muted">
        <span>{claim.claim_type}</span>
        <span>· {formatShortDate(claim.first_made_at)}</span>
        <span>· {claim.evidence.length} evidence</span>
        <button type="button" onClick={() => setOpen((v) => !v)} className="hover:text-ink">
          {open ? 'hide' : 'show'}
        </button>
        {canEdit && (
          <span className="ml-auto flex items-center gap-1">
            {claim.allowed_transitions.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setStatus.mutate({ claimId: claim.id, status: s }, { onError })}
                className="rounded border border-line px-1.5 py-0.5 hover:border-accent hover:text-ink"
              >
                mark {s.replace('_', ' ')}
              </button>
            ))}
            <button
              type="button"
              aria-pressed={claim.is_precedent}
              onClick={() => precedent.mutate(claim.id, { onError })}
              className="rounded border border-line px-1.5 py-0.5 hover:border-accent"
              title="Toggle precedent"
            >
              ⚖️
            </button>
            <button
              type="button"
              aria-label="Dismiss claim"
              onClick={() => dismiss.mutate(claim.id, { onError })}
              className="rounded border border-line px-1.5 py-0.5 hover:border-danger hover:text-danger"
            >
              <Icon name="delete" size={12} />
            </button>
          </span>
        )}
      </div>
      {open && (
        <ul className="mt-2 space-y-1 border-t border-line2 pt-2">
          {claim.evidence.map((e) => (
            <li key={e.id} className="flex items-start gap-2">
              <span className="w-4 text-center font-mono">{ROLE_GLYPH[e.role] ?? '·'}</span>
              <span
                className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${ORIGINATOR_COLOR[e.document_originator]}`}
              />
              <span className="min-w-0 flex-1">
                <Link to={`/document/${e.document_id}`} className="text-tealink hover:underline">
                  {e.document_title}
                </Link>
                <span className="ml-1 font-mono text-[10px] text-muted">
                  {formatShortDate(e.document_date)} · {e.role}
                  {e.reactions.map((r) => ` ${REACTION_GLYPH[r] ?? ''}`)}
                </span>
                {e.excerpt && <span className="block text-muted">“{e.excerpt}”</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}
