import { useState } from 'react'

import {
  type CaseDetail,
  useCaseActionStatus,
  useOpposingParties,
  useReenrich,
  useRefreshBrief,
} from '../../../api/caseDetail'
import { formatDueRelative, formatEur, formatShortDate } from '../../../format'
import { Button } from '../../../ui/Button'
import { Icon } from '../../../ui/Icon'
import { useToast } from '../../../ui/toast'

const PARTY_TONE: Record<string, string> = {
  own: 'bg-success',
  opposing: 'bg-danger',
  court: 'bg-info',
  third_party: 'bg-warning',
  unknown: 'bg-line3',
}

/** Living brief, deadlines, cost exposure and parties beside the case views. */
export function Rail({
  detail,
  onOpenDoc,
}: {
  detail: CaseDetail
  onOpenDoc: (id: number) => void
}) {
  const toast = useToast()
  const refresh = useRefreshBrief(detail.id)
  const setStatus = useCaseActionStatus(detail.id)
  const [audience, setAudience] = useState<'mine' | 'all'>('mine')
  const open = detail.action_items.filter(
    (a) =>
      a.status === 'open' && (audience === 'all' || a.addressee === null || a.addressee === 'user'),
  )
  const brief = detail.brief
  return (
    <aside
      aria-label="Case brief"
      className="flex w-[270px] shrink-0 flex-col gap-3 overflow-y-auto border-l border-line bg-panel2 p-3.5 text-[12px]"
    >
      <section className="rounded-xl border border-line bg-gradient-to-br from-aibg to-card p-3.5">
        <header className="mb-2 flex items-center gap-1.5">
          <Icon name="auto_awesome" size={15} className="text-accent" />
          <h3 className="text-[10px] font-bold tracking-[.12em] text-muted uppercase">Brief</h3>
          {detail.can_edit && (
            <button
              type="button"
              aria-label="Refresh brief"
              disabled={brief.status === 'processing' || refresh.isPending}
              onClick={() =>
                refresh.mutate(undefined, { onError: (e) => toast(e.message, 'error') })
              }
              className="ml-auto text-muted hover:text-ink disabled:opacity-50"
            >
              <Icon
                name="refresh"
                size={14}
                className={brief.status === 'processing' ? 'animate-spin' : ''}
              />
            </button>
          )}
        </header>
        {brief.status === 'processing' && <p className="text-muted">Generating brief…</p>}
        {brief.status === 'failed' && (
          <p className="text-danger">Brief failed{brief.error ? `: ${brief.error}` : ''}.</p>
        )}
        {brief.status === 'none' && <p className="text-muted">No brief yet.</p>}
        {brief.status === 'ready' && (
          <div className="space-y-2 leading-relaxed text-ink2">
            {brief.posture && <p>{brief.posture}</p>}
            {(brief.pressure_points ?? []).length > 0 && (
              <ul className="list-disc space-y-0.5 pl-4">
                {(brief.pressure_points ?? []).map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            )}
            {brief.next_move && (
              <p>
                <b className="text-tealink">Next —</b> {brief.next_move}
              </p>
            )}
            {brief.detected_status && (
              <p className="text-[11px] text-muted">
                Status: {brief.detected_status.replace('_', ' ')}
                {brief.status_rationale ? ` — ${brief.status_rationale}` : ''}
              </p>
            )}
            {brief.updated_at && (
              <p className="font-mono text-[10px] text-muted2">
                {formatShortDate(brief.updated_at)}
              </p>
            )}
          </div>
        )}
      </section>

      <section>
        <h3 className="mb-1.5 flex items-center text-[10px] font-bold tracking-[.12em] text-muted uppercase">
          Deadlines <span className="ml-1 font-mono normal-case">{open.length}</span>
          <button
            type="button"
            onClick={() => setAudience((v) => (v === 'mine' ? 'all' : 'mine'))}
            className="ml-auto normal-case tracking-normal text-muted hover:text-ink"
            aria-pressed={audience === 'all'}
          >
            {audience === 'mine' ? 'mine' : 'all'}
          </button>
        </h3>
        <ul className="space-y-1.5">
          {open.slice(0, 6).map((a) => (
            <li
              key={a.id}
              className={`rounded-lg border px-2.5 py-2 ${a.is_overdue ? 'border-danger/40 bg-danger/8' : 'border-line bg-card'}`}
            >
              <div className="flex items-center gap-2 font-mono text-[10.5px]">
                <span className={a.is_overdue ? 'text-danger' : 'text-warning'}>
                  {formatDueRelative(a.due_date)}
                </span>
                <span className="text-muted">{formatShortDate(a.due_date)}</span>
                {a.addressee && a.addressee !== 'user' && (
                  <span className="rounded border border-line px-1 text-[9px] text-muted">
                    {a.addressee}
                  </span>
                )}
                {detail.can_edit && (
                  <span className="ml-auto flex gap-1">
                    <button
                      type="button"
                      aria-label={`Mark ${a.title} done`}
                      onClick={() =>
                        setStatus.mutate(
                          { itemId: a.id, status: 'completed' },
                          { onError: (e) => toast(e.message, 'error') },
                        )
                      }
                      className="text-muted hover:text-success"
                    >
                      <Icon name="check_circle" size={14} />
                    </button>
                    <button
                      type="button"
                      aria-label={`Dismiss ${a.title}`}
                      onClick={() =>
                        setStatus.mutate(
                          { itemId: a.id, status: 'dismissed' },
                          { onError: (e) => toast(e.message, 'error') },
                        )
                      }
                      className="text-muted hover:text-danger"
                    >
                      <Icon name="close" size={14} />
                    </button>
                  </span>
                )}
              </div>
              {a.source_document_id !== null ? (
                <button
                  type="button"
                  onClick={() => onOpenDoc(a.source_document_id ?? 0)}
                  className="mt-0.5 block text-left hover:underline"
                >
                  {a.title}
                </button>
              ) : (
                <p className="mt-0.5">{a.title}</p>
              )}
            </li>
          ))}
          {open.length === 0 && <li className="text-muted">No open deadlines.</li>}
        </ul>
      </section>

      <section className="rounded-xl border border-line bg-card p-3">
        <p className="font-display text-[18px] font-bold">
          {formatEur(detail.financials.total_cost_exposure_cents / 100)}
        </p>
        <p className="text-[10.5px] text-muted">projected exposure</p>
        <dl className="mt-2 grid grid-cols-3 gap-1 font-mono text-[10px]">
          <dt className="text-muted">booked</dt>
          <dt className="text-muted">paid</dt>
          <dt className="text-muted">open</dt>
          <dd>{formatEur(detail.financials.booked)}</dd>
          <dd>{formatEur(detail.financials.paid)}</dd>
          <dd>{formatEur(detail.financials.outstanding)}</dd>
        </dl>
      </section>

      <Parties detail={detail} />
    </aside>
  )
}

function Parties({ detail }: { detail: CaseDetail }) {
  const toast = useToast()
  const save = useOpposingParties(detail.id)
  const reenrich = useReenrich(detail.id)
  const [editing, setEditing] = useState(false)
  return (
    <section>
      <h3 className="mb-1.5 flex items-center text-[10px] font-bold tracking-[.12em] text-muted uppercase">
        Parties
        {detail.can_edit && (
          <button
            type="button"
            onClick={() => setEditing((v) => !v)}
            className="ml-auto normal-case tracking-normal text-muted hover:text-ink"
          >
            {editing ? 'cancel' : 'edit'}
          </button>
        )}
      </h3>
      <ul className="space-y-1">
        {detail.parties.map((p) => (
          <li key={`${p.role}-${p.name}`} className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${PARTY_TONE[p.role] ?? 'bg-line3'}`} />
            <span className="truncate">{p.name}</span>
            <span className="ml-auto font-mono text-[10px] text-muted">{p.role}</span>
          </li>
        ))}
        {detail.parties.length === 0 && <li className="text-muted">No parties detected yet.</li>}
      </ul>
      {editing ? (
        <form
          className="mt-2 space-y-1.5"
          onSubmit={(e) => {
            e.preventDefault()
            const raw = String(new FormData(e.currentTarget).get('opposing'))
            save.mutate(
              raw
                .split(',')
                .map((s) => s.trim())
                .filter(Boolean),
              { onSuccess: () => setEditing(false), onError: (err) => toast(err.message, 'error') },
            )
          }}
        >
          <input
            name="opposing"
            defaultValue={detail.opposing_parties.join(', ')}
            aria-label="Opposing parties"
            placeholder="Opposing parties, comma-separated"
            className="w-full rounded-md border border-line bg-card px-2 py-1 text-[11.5px]"
          />
          <div className="flex gap-1.5">
            <Button type="submit" className="px-2.5 py-1 text-[11px]" disabled={save.isPending}>
              Save
            </Button>
            <Button
              type="button"
              variant="secondary"
              className="px-2.5 py-1 text-[11px]"
              disabled={reenrich.isPending}
              onClick={() =>
                reenrich.mutate(undefined, {
                  onSuccess: (r) => toast(`${r.queued} documents queued for re-enrichment`),
                  onError: (err) => toast(err.message, 'error'),
                })
              }
            >
              Re-enrich
            </Button>
          </div>
        </form>
      ) : (
        detail.opposing_parties.length > 0 && (
          <p className="mt-1 text-[11px] text-muted">vs. {detail.opposing_parties.join(', ')}</p>
        )
      )}
    </section>
  )
}
