import { useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'

import type { Schemas } from '../../api/client'
import { type TriageBundle, useConfirmBundle } from '../../api/triage'
import { Badge, type Tone } from '../../ui/Badge'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'

/** What the triage inbox hands the document review so its case/proceeding cards can route. */
export type Routing = {
  bundle: TriageBundle
  cases: Schemas['PickerCase'][]
  proceedings: Schemas['PickerProceeding'][]
  onCreateCase: () => void
}

export const CONFIDENCE_TONE: Record<string, Tone> = {
  high: 'success',
  medium: 'warning',
  low: 'danger',
}

const CARD_LABEL = 'mb-1.5 text-[8px] font-bold tracking-[.1em] text-muted2 uppercase'

/** Case and proceeding cards; each opens an anchored picker that re-routes the whole bundle. */
export function RoutingCards({
  review,
  routing,
}: {
  review: Schemas['DocumentReview']
  routing: Routing
}) {
  const { bundle, cases, proceedings, onCreateCase } = routing
  const confirm = useConfirmBundle()
  const queryClient = useQueryClient()
  const toast = useToast()
  const [open, setOpen] = useState<'case' | 'proceeding' | null>(null)
  const [filter, setFilter] = useState('')
  const caseId = review.case?.id ?? null
  const suggested = bundle.suggestion?.case_id ?? null
  const confidence = bundle.sub_groups[0]?.case_confidence ?? null
  const procOptions = proceedings.filter((p) => p.case_id === caseId)

  function route(nextCase: string, nextProceeding: number | null) {
    setOpen(null)
    confirm.mutate(
      {
        key: bundle.key,
        action: 'assign_case',
        batch_id: bundle.batch_id,
        doc_id: bundle.batch_id === null ? bundle.lead_doc_id : null,
        case_id: nextCase,
        proceeding_id: nextProceeding,
      },
      {
        onSuccess: (r) => {
          queryClient.invalidateQueries({ queryKey: ['document'] })
          toast(`Assigned to ${r.case.id}`)
        },
        onError: (e) => toast(e.message, 'error'),
      },
    )
  }

  const q = filter.trim().toLowerCase()
  const otherCases = cases.filter(
    (c) => c.id !== suggested && (!q || `${c.id} ${c.title}`.toLowerCase().includes(q)),
  )
  const suggestedCase = cases.find((c) => c.id === suggested)

  return (
    <div className="grid grid-cols-2 gap-2.5">
      <div className="relative">
        <button
          type="button"
          aria-label="Change case"
          aria-expanded={open === 'case'}
          disabled={confirm.isPending}
          onClick={() => setOpen(open === 'case' ? null : 'case')}
          className="block w-full rounded-[9px] border border-line bg-card px-[11px] py-[9px] text-left hover:bg-accent/7 disabled:opacity-60"
        >
          <div className={CARD_LABEL}>Case</div>
          <div className="flex items-center gap-[7px]">
            {review.case ? (
              <span className="font-mono text-[13px] font-semibold">{review.case.id}</span>
            ) : (
              <span className="text-[11px] text-muted">Unassigned · in triage</span>
            )}
            {review.case?.is_draft && (
              <Badge tone="warning" pill>
                draft
              </Badge>
            )}
            {confidence && (
              <Badge tone={CONFIDENCE_TONE[confidence] ?? 'neutral'} pill>
                {confidence}
              </Badge>
            )}
            <Icon name="edit" size={14} className="ml-auto text-muted2" />
          </div>
          {review.case && (
            <div className="mt-1 truncate text-[10px] text-muted">{review.case.title}</div>
          )}
        </button>
        {open === 'case' && (
          <Popover onClose={() => setOpen(null)}>
            {suggestedCase && (
              <PickRow
                active={caseId === suggestedCase.id}
                title={suggestedCase.id}
                sub={`${suggestedCase.title} · AI-detected`}
                onClick={() => route(suggestedCase.id, null)}
              />
            )}
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter cases…"
              aria-label="Filter cases"
              className="w-full border-y border-line2 bg-transparent px-3 py-1.5 text-[11px] outline-none"
            />
            <div className="max-h-52 overflow-y-auto">
              {otherCases.map((c) => (
                <PickRow
                  key={c.id}
                  active={caseId === c.id}
                  title={c.id}
                  sub={c.title}
                  onClick={() => route(c.id, null)}
                />
              ))}
            </div>
            <button
              type="button"
              onClick={() => {
                setOpen(null)
                onCreateCase()
              }}
              className="flex w-full items-center gap-2 border-t border-line2 px-3 py-2 text-left text-[11px] font-semibold text-tealink hover:bg-accent/7"
            >
              <Icon name="add" size={14} /> Create new case…
            </button>
          </Popover>
        )}
      </div>
      <div className="relative">
        <button
          type="button"
          aria-label="Change proceeding"
          aria-expanded={open === 'proceeding'}
          disabled={confirm.isPending || caseId === null}
          onClick={() => setOpen(open === 'proceeding' ? null : 'proceeding')}
          className="block w-full rounded-[9px] border border-line bg-card px-[11px] py-[9px] text-left hover:bg-accent/7 disabled:opacity-60"
        >
          <div className={CARD_LABEL}>Proceeding</div>
          {review.proceeding ? (
            <>
              <div className="flex items-center gap-[7px]">
                <span className="font-mono text-[12px]">{review.proceeding.az_court ?? '—'}</span>
                <Badge tone="accent" pill>
                  {review.proceeding.court_level.toUpperCase()}
                </Badge>
                <Icon name="expand_more" size={15} className="ml-auto text-muted2" />
              </div>
              <div className="mt-1 truncate text-[10px] text-muted">
                {review.proceeding.court_name}
              </div>
            </>
          ) : (
            <div className="flex items-center gap-[7px] text-[11px] text-muted">
              {review.az_court ? `AZ ${review.az_court} · no proceeding yet` : 'No proceeding'}
              <Icon name="expand_more" size={15} className="ml-auto text-muted2" />
            </div>
          )}
        </button>
        {open === 'proceeding' && caseId && (
          <Popover onClose={() => setOpen(null)}>
            {procOptions.map((p) => (
              <PickRow
                key={p.id}
                active={review.proceeding?.id === p.id}
                title={p.label}
                onClick={() => route(caseId, p.id)}
              />
            ))}
            <PickRow
              active={!review.proceeding}
              title="— none —"
              onClick={() => route(caseId, null)}
            />
          </Popover>
        )}
      </div>
    </div>
  )
}

function Popover({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  return (
    <>
      <button
        type="button"
        aria-label="Close picker"
        onClick={onClose}
        className="fixed inset-0 z-190 cursor-default"
      />
      <div
        role="menu"
        className="absolute top-[calc(100%+6px)] right-0 left-0 z-200 overflow-hidden rounded-[10px] border border-line3 bg-panel shadow-[0_18px_40px_rgba(0,0,0,.35)]"
      >
        {children}
      </div>
    </>
  )
}

function PickRow({
  active,
  title,
  sub,
  onClick,
}: {
  active: boolean
  title: string
  sub?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent/7"
    >
      {active ? (
        <Icon name="check" size={14} className="text-accent" />
      ) : (
        <span className="w-[14px] shrink-0" />
      )}
      <span className="min-w-0">
        <span className={`block truncate font-mono text-[11.5px] ${active ? '' : 'text-ink2'}`}>
          {title}
        </span>
        {sub && <span className="block truncate text-[9px] text-muted">{sub}</span>}
      </span>
    </button>
  )
}
