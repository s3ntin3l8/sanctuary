import { useEffect, useState } from 'react'
import { useParams } from 'react-router'

import { useSlicing, useSlicingConfirm, useSlicingRetry } from '../../api/triage'
import { leaveTo } from '../../navigation'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

/** Decide where a scanned multi-page PDF splits into separate documents. */
export function SlicingPage() {
  const batchId = Number(useParams().batchId)
  const query = useSlicing(batchId)
  const confirm = useSlicingConfirm(batchId)
  const retry = useSlicingRetry(batchId)
  const toast = useToast()
  const [cuts, setCuts] = useState<Set<number> | null>(null)
  const [cursor, setCursor] = useState(1)
  const view = query.data

  useEffect(() => {
    document.title = 'Slicing review | The Sanctuary'
  }, [])
  const activeCuts = cuts ?? new Set(view?.proposed_cuts.map((c) => c.page - 1) ?? [])

  useEffect(() => {
    if (!view || view.status !== 'ready') return
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return
      if (e.key === 'ArrowDown') setCursor((c) => Math.min(c + 1, view.page_count - 1))
      else if (e.key === 'ArrowUp') setCursor((c) => Math.max(c - 1, 1))
      else if (e.key.toLowerCase() === 'c') toggle(cursor)
      else if (e.key === 'Escape') leaveTo('/triage')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  function toggle(after: number) {
    setCuts((prev) => {
      const next = new Set(prev ?? activeCuts)
      if (next.has(after)) next.delete(after)
      else next.add(after)
      return next
    })
  }

  if (!view)
    return (
      <div className="p-6">
        <QueryState error={query.error} pending={query.isPending} />
      </div>
    )
  const sorted = [...activeCuts].sort((a, b) => a - b)
  const partCount = sorted.length + 1

  return (
    <div className="flex min-h-full flex-col">
      <header className="flex items-center gap-4 border-b border-line bg-panel px-6 py-4">
        <div>
          <a href="/triage" className="font-mono text-[11px] text-muted hover:underline">
            Triage ›
          </a>
          <h1 className="font-display text-[22px] font-extrabold tracking-tight">
            Slice scan #{batchId}
          </h1>
          <p className="font-mono text-[11px] text-muted">
            {view.subject ?? 'scan'} · {view.page_count} pages · {partCount} document
            {partCount === 1 ? '' : 's'}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {view.status === 'ready' && (
            <>
              <Button variant="secondary" onClick={() => setCuts(new Set())}>
                Single document
              </Button>
              <Button variant="secondary" onClick={() => setCuts(null)}>
                Reset to proposal
              </Button>
              <Button
                disabled={confirm.isPending}
                onClick={() =>
                  confirm.mutate(sorted, {
                    onSuccess: () => {
                      toast(`${partCount} document(s) queued`)
                      leaveTo('/triage')
                    },
                    onError: (e) => toast(e.message, 'error'),
                  })
                }
              >
                Confirm <kbd className="ml-1 font-mono text-[9px] opacity-70">↵</kbd>
              </Button>
            </>
          )}
          {view.status === 'failed' && (
            <Button
              disabled={retry.isPending}
              onClick={() => retry.mutate(undefined, { onError: (e) => toast(e.message, 'error') })}
            >
              <Icon name="replay" size={16} /> Retry preparation
            </Button>
          )}
        </div>
      </header>

      <div className="px-6 py-4">
        {view.status === 'preparing' && (
          <p className="flex items-center gap-2 text-[12px] text-muted">
            <Icon name="hourglass_top" size={16} className="text-warning" /> Preparing page previews
            and a split proposal…
          </p>
        )}
        {view.status === 'failed' && (
          <p
            role="alert"
            className="rounded-xl border border-danger/40 bg-danger/10 px-4 py-3 text-[12px] text-danger"
          >
            Preparation failed. {view.error}
          </p>
        )}
        {view.status === 'done' && (
          <p className="text-[12px] text-muted">
            This scan has already been sliced.{' '}
            <a href="/triage" className="text-tealink hover:underline">
              Back to triage
            </a>
            .
          </p>
        )}
        {view.status === 'ready' && (
          <>
            <p className="mb-3 text-[11px] text-muted">
              Click a gutter (or press <kbd className="font-mono">C</kbd> on the highlighted one) to
              split after that page. ↑↓ moves the highlight, ↵ confirms, Esc leaves.
            </p>
            <ol className="space-y-1">
              {view.pages.map((p, i) => {
                const after = p.page
                const isCut = activeCuts.has(after)
                const proposed = view.proposed_cuts.find((c) => c.page - 1 === after)
                return (
                  <li key={p.page}>
                    <div className="flex gap-3 rounded-xl border border-line bg-card p-2">
                      <div className="h-28 w-20 shrink-0 overflow-hidden rounded-md border border-line2 bg-panel2">
                        {p.has_thumbnail ? (
                          <img
                            src={`/api/v1/slicing/${batchId}/thumb/${p.page}`}
                            alt={`Page ${p.page}`}
                            className="h-full w-full object-cover"
                            loading="lazy"
                          />
                        ) : (
                          <span className="flex h-full items-center justify-center font-mono text-[10px] text-muted">
                            p. {p.page}
                          </span>
                        )}
                      </div>
                      <div className="min-w-0 flex-1 text-[11px]">
                        <div className="font-mono text-[10px] text-muted">Page {p.page}</div>
                        <p className="line-clamp-2 text-ink2">{p.text_head}</p>
                        <p className="mt-1 line-clamp-1 text-muted">…{p.text_tail}</p>
                      </div>
                    </div>
                    {i < view.pages.length - 1 && (
                      <button
                        type="button"
                        onClick={() => {
                          setCursor(after)
                          toggle(after)
                        }}
                        aria-pressed={isCut}
                        aria-label={`Split after page ${after}`}
                        className={`my-1 flex w-full items-center gap-2 rounded-md border px-3 py-1 text-[10.5px] ${isCut ? 'border-accent bg-accent/12 text-ink' : cursor === after ? 'border-line3 text-muted' : 'border-transparent text-muted2 hover:border-line3'}`}
                      >
                        <Icon name="content_cut" size={13} />{' '}
                        {isCut
                          ? `Split here — part ${sorted.indexOf(after) + 2} starts on page ${after + 1}`
                          : 'continue'}
                        {proposed && (
                          <Badge
                            tone={
                              proposed.confidence === 'high'
                                ? 'success'
                                : proposed.confidence === 'low'
                                  ? 'danger'
                                  : 'warning'
                            }
                          >
                            AI {proposed.confidence ?? 'proposed'}
                          </Badge>
                        )}
                        {proposed?.notes && (
                          <span className="truncate text-muted">{proposed.notes}</span>
                        )}
                      </button>
                    )}
                  </li>
                )
              })}
            </ol>
          </>
        )}
      </div>
    </div>
  )
}
