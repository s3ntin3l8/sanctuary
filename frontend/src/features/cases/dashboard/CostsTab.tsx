import { Link } from 'react-router'

import { type CaseDetail, useFinancials, useSignalRole } from '../../../api/caseDetail'
import type { Schemas } from '../../../api/client'
import { type CostAction, useLedgerAction } from '../../../api/costs'
import { formatEur, formatShortDate } from '../../../format'
import { Badge } from '../../../ui/Badge'
import { Button } from '../../../ui/Button'
import { QueryState } from '../../../ui/QueryState'
import { useToast } from '../../../ui/toast'

type CostRow = Schemas['CostRow']

/** Cost exposure per instance (RVG/GKG theory vs. booked invoices) and cost signals. */
export function CostsTab({ detail }: { detail: CaseDetail }) {
  const toast = useToast()
  const query = useFinancials(detail.id, true)
  const act = useLedgerAction()
  const role = useSignalRole(detail.id)
  const fin = query.data
  if (!fin) return <QueryState error={query.error} pending={query.isPending} />
  const onError = (e: Error) => toast(e.message, 'error')
  const run = (costId: number, action: CostAction) => act.mutate({ costId, action }, { onError })
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-5 text-[12px]">
      <dl className="mb-4 grid grid-cols-5 gap-3" aria-label="Cost summary">
        {(
          [
            ['Projected', fin.summary.total_cost_exposure_cents / 100],
            ['Booked', fin.summary.booked],
            ['Paid', fin.summary.paid],
            ['Outstanding', fin.summary.outstanding],
            ['§91 ZPO reimbursable', fin.summary.reimbursable],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="rounded-xl border border-line bg-card p-3">
            <dt className="text-[10px] font-bold tracking-[.12em] text-muted uppercase">{label}</dt>
            <dd className="font-display text-[17px] font-bold">{formatEur(value)}</dd>
          </div>
        ))}
      </dl>

      {fin.instances.map((inst) => (
        <section
          key={inst.proceeding_id}
          className="mb-4 rounded-xl border border-line bg-card p-3"
        >
          <header className="mb-2 flex items-center gap-2">
            <h3 className="font-display text-[13px] font-bold">
              {inst.court_name}
              {inst.az_court ? ` · ${inst.az_court}` : ''}
            </h3>
            {inst.streitwert !== null && (
              <span className="font-mono text-[10.5px] text-muted">
                Streitwert {formatEur(inst.streitwert)}
              </span>
            )}
            {inst.allocation_source && <Badge>{inst.allocation_source}</Badge>}
            <span className="ml-auto font-display text-[14px] font-bold">
              {formatEur(inst.subtotal)}
            </span>
          </header>
          <Bucket
            label="Own lawyer"
            charged={inst.own_lawyer_gross}
            theory={inst.own_theoretical}
            source={inst.own_lawyer_source}
            rows={inst.invoices_own}
            canEdit={detail.can_edit}
            run={run}
          />
          <Bucket
            label={`Court fee (${Math.round(inst.court_fee_share * 100)}% share)`}
            charged={inst.court_fee_charged}
            theory={inst.court_theoretical}
            source={inst.court_fee_source}
            rows={inst.invoices_court}
            canEdit={detail.can_edit}
            run={run}
          />
          <Bucket
            label="Opposing lawyer"
            charged={inst.opposing_gross}
            theory={inst.opposing_theoretical}
            source={inst.opposing_source}
            rows={inst.invoices_opposing}
            canEdit={detail.can_edit}
            run={run}
          />
          {inst.invoices_other.length > 0 && (
            <Bucket
              label="Other"
              charged={inst.invoices_other.reduce((s, r) => s + r.amount_gross, 0)}
              theory={null}
              source="invoice"
              rows={inst.invoices_other}
              canEdit={detail.can_edit}
              run={run}
            />
          )}
        </section>
      ))}

      {fin.case_level_costs.length > 0 && (
        <section className="mb-4 rounded-xl border border-line bg-card p-3">
          <h3 className="mb-2 font-display text-[13px] font-bold">Not assigned to a proceeding</h3>
          <Rows rows={fin.case_level_costs} canEdit={detail.can_edit} run={run} />
        </section>
      )}

      <section className="rounded-xl border border-line bg-card p-3">
        <h3 className="mb-2 font-display text-[13px] font-bold">Document cost signals</h3>
        {fin.signal_docs.length === 0 && (
          <p className="text-muted">No cost signals detected yet.</p>
        )}
        <ul className="space-y-1">
          {fin.signal_docs.map((s) => (
            <li key={s.signal_id} className="flex items-center gap-2">
              <Badge>{s.signal_type.replace('_', ' ')}</Badge>
              <Link
                to={`/document/${s.id}`}
                className="min-w-0 flex-1 truncate text-tealink hover:underline"
              >
                {s.title}
              </Link>
              <span className="font-mono text-[10.5px] text-muted">
                {formatShortDate(s.issued_date)}
              </span>
              {s.amount !== null && (
                <span className="font-mono text-[11px]">{formatEur(s.amount)}</span>
              )}
              {s.signal_type === 'cost_ruling' && (
                <span className="flex items-center gap-1">
                  <select
                    aria-label="Client role in the cost ruling"
                    value={s.client_role ?? ''}
                    disabled={!detail.can_edit}
                    onChange={(e) =>
                      role.mutate(
                        {
                          signalId: s.signal_id,
                          role: (e.target.value || 'unset') as 'winner' | 'loser' | 'unset',
                        },
                        { onError },
                      )
                    }
                    className="rounded border border-line bg-card2 px-1 py-0.5 text-[11px]"
                  >
                    <option value="">who won?</option>
                    <option value="winner">we won</option>
                    <option value="loser">we lost</option>
                  </select>
                  {s.role_source === 'auto' && <span title="auto-detected">✨</span>}
                  {detail.can_edit && (
                    <button
                      type="button"
                      title="Auto-detect from the ruling text"
                      onClick={() =>
                        role.mutate({ signalId: s.signal_id, role: 'auto' }, { onError })
                      }
                      className="text-muted hover:text-ink"
                    >
                      ✨
                    </button>
                  )}
                </span>
              )}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

function Bucket({
  label,
  charged,
  theory,
  source,
  rows,
  canEdit,
  run,
}: {
  label: string
  charged: number
  theory: number | null
  source: string
  rows: CostRow[]
  canEdit: boolean
  run: (costId: number, action: CostAction) => void
}) {
  return (
    <div className="mb-2 border-t border-line2 pt-2">
      <div className="flex items-center gap-2">
        <span className="text-[11px] font-semibold">{label}</span>
        <Badge>{source}</Badge>
        {theory !== null && source !== 'rvg' && source !== 'gkg' && (
          <span className="font-mono text-[10px] text-muted2 line-through">
            {formatEur(theory)}
          </span>
        )}
        <span className="ml-auto font-mono text-[11.5px]">{formatEur(charged)}</span>
      </div>
      <Rows rows={rows} canEdit={canEdit} run={run} />
    </div>
  )
}

function Rows({
  rows,
  canEdit,
  run,
}: {
  rows: CostRow[]
  canEdit: boolean
  run: (id: number, a: CostAction) => void
}) {
  if (rows.length === 0) return null
  return (
    <ul className="mt-1 space-y-0.5">
      {rows.map((r) => (
        <li
          key={r.id}
          id={`cost-row-${r.id}`}
          className="grid grid-cols-[1fr_90px_80px_90px_auto] items-center gap-2 rounded px-1 py-0.5 hover:bg-accent/5"
        >
          <span className="truncate">
            {r.source_document_id ? (
              <Link to={`/document/${r.source_document_id}`} className="hover:underline">
                {r.title}
              </Link>
            ) : (
              r.title
            )}
          </span>
          <span className="font-mono text-[10.5px] text-muted">{formatShortDate(r.issued_at)}</span>
          <Badge
            tone={
              r.status === 'bezahlt' || r.status === 'erstattet'
                ? 'success'
                : r.status === 'offen'
                  ? 'warning'
                  : 'neutral'
            }
          >
            {r.status}
          </Badge>
          <span className="text-right font-mono text-[11px]">{formatEur(r.amount_gross)}</span>
          <span className="flex gap-1">
            {canEdit && r.status !== 'bezahlt' && r.status !== 'erstattet' && (
              <Button size="sm" variant="secondary" onClick={() => run(r.id, 'pay')}>
                pay
              </Button>
            )}
            {canEdit && r.status === 'bezahlt' && (
              <>
                <Button size="sm" variant="secondary" onClick={() => run(r.id, 'unpay')}>
                  unpay
                </Button>
                {r.is_reimbursable && (
                  <Button size="sm" variant="secondary" onClick={() => run(r.id, 'reimburse')}>
                    reimbursed
                  </Button>
                )}
              </>
            )}
            {canEdit && r.status === 'erstattet' && (
              <Button size="sm" variant="secondary" onClick={() => run(r.id, 'unreimburse')}>
                undo
              </Button>
            )}
          </span>
        </li>
      ))}
    </ul>
  )
}
