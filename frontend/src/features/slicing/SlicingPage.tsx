import { useEffect, useState } from 'react'
import { useParams } from 'react-router'

import { useSlicing, useSlicingConfirm, useSlicingRetry } from '../../api/triage'
import type { Schemas } from '../../api/client'
import { leaveTo } from '../../navigation'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'
import { PageViewer } from './PageViewer'

type Kind = Schemas['SliceCut']['kind']

/** Decide where a scanned multi-page PDF splits into documents, and whether each new part is a
 * letter of its own or an attachment of the letter before it. */
export function SlicingPage() {
  const batchId = Number(useParams().batchId)
  const query = useSlicing(batchId)
  const confirm = useSlicingConfirm(batchId)
  const retry = useSlicingRetry(batchId)
  const toast = useToast()
  const [cuts, setCuts] = useState<Map<number, Kind> | null>(null)
  // null = the pages the server marked blank; edited once the user toggles a page.
  const [discarded, setDiscarded] = useState<Set<number> | null>(null)
  const [cursor, setCursor] = useState(1)
  const [viewing, setViewing] = useState<number | null>(null)
  const view = query.data

  useEffect(() => {
    document.title = 'Slicing review | The Sanctuary'
  }, [])
  // A proposal's page is where the new part starts; a cut is stored as the page it follows.
  const activeCuts: Map<number, Kind> =
    cuts ?? new Map(view?.proposed_cuts.map((c) => [c.page - 1, c.kind]) ?? [])
  const activeDiscarded: Set<number> =
    discarded ?? new Set(view?.pages.filter((p) => p.blank).map((p) => p.page) ?? [])

  useEffect(() => {
    if (!view || view.status !== 'ready') return
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return
      if (viewing !== null) {
        // The viewer owns the keyboard: Esc closes it, not the page; Enter never confirms.
        const k = e.key.toLowerCase()
        if (e.key === 'ArrowLeft') setViewing(Math.max(viewing - 1, 1))
        else if (e.key === 'ArrowRight') setViewing(Math.min(viewing + 1, view.page_count))
        else if (e.key === 'Escape') setViewing(null)
        else if (k === 'd') toggleDiscard(viewing)
        else if (viewing > 1 && k === 'c') toggle(viewing - 1)
        else if (viewing > 1 && k === 'l') setKind(viewing - 1, 'letter')
        else if (viewing > 1 && k === 'a') setKind(viewing - 1, 'attachment')
        return
      }
      if (e.key === ' ' || e.key.toLowerCase() === 'o') {
        e.preventDefault()
        setViewing(cursor)
      }
      if (e.key === 'ArrowDown') setCursor((c) => Math.min(c + 1, view.page_count - 1))
      else if (e.key === 'ArrowUp') setCursor((c) => Math.max(c - 1, 1))
      else if (e.key.toLowerCase() === 'c') toggle(cursor)
      else if (e.key.toLowerCase() === 'l') setKind(cursor, 'letter')
      else if (e.key.toLowerCase() === 'a') setKind(cursor, 'attachment')
      else if (e.key.toLowerCase() === 'd') toggleDiscard(cursor + 1)
      else if (e.key === 'Enter' && !confirm.isPending) submit()
      else if (e.key === 'Escape') leaveTo('/triage')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  function submit() {
    confirm.mutate(
      {
        cuts: [...activeCuts].sort(([a], [b]) => a - b).map(([page, kind]) => ({ page, kind })),
        discard: [...activeDiscarded].sort((a, b) => a - b),
      },
      {
        onSuccess: (r) => {
          toast(`${r.document_ids.length} document(s) queued`)
          leaveTo('/triage')
        },
        onError: (e) => toast(e.message, 'error'),
      },
    )
  }

  function toggle(after: number) {
    setCuts((prev) => {
      const next = new Map(prev ?? activeCuts)
      if (next.has(after)) next.delete(after)
      else next.set(after, 'attachment')
      return next
    })
  }

  function toggleDiscard(page: number) {
    setDiscarded((prev) => {
      const next = new Set(prev ?? activeDiscarded)
      if (next.has(page)) next.delete(page)
      else next.add(page)
      return next
    })
  }

  function setKind(after: number, kind: Kind) {
    setCuts((prev) => {
      // Choosing a kind on a gap that isn't cut yet cuts it.
      const next = new Map(prev ?? activeCuts)
      next.set(after, kind)
      return next
    })
  }

  if (!view)
    return (
      <div className="p-6">
        <QueryState error={query.error} pending={query.isPending} />
      </div>
    )
  const sorted = [...activeCuts.keys()].sort((a, b) => a - b)
  // Parts that are entirely discarded vanish; their letter opener carries to the next kept part
  // (mirrors the server).
  let partCount = 0
  let letterCount = 0
  let carryLetter = false
  ;[0, ...sorted].forEach((after, i) => {
    const end = sorted[i] ?? view.page_count
    const opensLetter = i === 0 || activeCuts.get(after) === 'letter'
    const kept = view.pages.some(
      (p) => p.page > after && p.page <= end && !activeDiscarded.has(p.page),
    )
    if (!kept) {
      carryLetter = carryLetter || opensLetter
      return
    }
    partCount += 1
    if (partCount === 1 || opensLetter || carryLetter) letterCount += 1
    carryLetter = false
  })

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
            {partCount === 1 ? '' : 's'} · {letterCount} letter{letterCount === 1 ? '' : 's'}
            {activeDiscarded.size > 0 && ` · ${activeDiscarded.size} discarded`}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {view.status === 'ready' && (
            <>
              <Button
                variant="secondary"
                disabled={activeCuts.size === 0}
                title={
                  activeCuts.size === 0
                    ? 'No cuts — Confirm keeps the scan as one document'
                    : 'Remove every cut and keep the scan as one document'
                }
                onClick={() => setCuts(new Map())}
              >
                Clear all cuts
              </Button>
              <Button
                variant="secondary"
                title="Discard your edits and go back to the saved proposal"
                onClick={() => {
                  setCuts(null)
                  setDiscarded(null)
                }}
              >
                Reset to proposal
              </Button>
              <Button
                variant="secondary"
                title="Read the pages again and compute a fresh proposal on the server"
                disabled={retry.isPending}
                onClick={() =>
                  retry.mutate(undefined, { onError: (e) => toast(e.message, 'error') })
                }
              >
                <Icon name="replay" size={16} /> Re-run proposal
              </Button>
              <Button
                disabled={confirm.isPending || partCount === 0}
                title={partCount === 0 ? 'Every page is discarded' : undefined}
                onClick={submit}
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
          <div className="space-y-2">
            <p className="flex items-center gap-2 text-[12px] text-muted">
              <Icon name="hourglass_top" size={16} className="text-warning" />
              {view.progress_phase === 'ai'
                ? 'Asking the model where one letter ends and the next begins…'
                : view.progress_done && view.progress_total
                  ? `Reading page ${view.progress_done} of ${view.progress_total}…`
                  : 'Preparing page previews and a split proposal…'}
            </p>
            {view.progress_total ? (
              <div
                role="progressbar"
                aria-valuenow={view.progress_done ?? 0}
                aria-valuemax={view.progress_total}
                className="h-1 w-64 overflow-hidden rounded-full bg-line2"
              >
                <div
                  className="h-full bg-accent transition-[width] duration-500"
                  style={{
                    width: `${Math.round(((view.progress_done ?? 0) / view.progress_total) * 100)}%`,
                  }}
                />
              </div>
            ) : null}
          </div>
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
              split after that page, then mark the new part as a <b>new letter</b> or an{' '}
              <b>attachment</b> of the letter above (<kbd className="font-mono">L</kbd> /{' '}
              <kbd className="font-mono">A</kbd>). ↑↓ moves the highlight,{' '}
              <kbd className="font-mono">Space</kbd> opens the page,{' '}
              <kbd className="font-mono">D</kbd> discards the page below the highlight (it ends up
              in no document), ↵ confirms, Esc leaves.
            </p>
            <ol className="space-y-1">
              {view.pages.map((p, i) => {
                const after = p.page
                const cutKind = activeCuts.get(after)
                const isCut = cutKind !== undefined
                const proposed = view.proposed_cuts.find((c) => c.page - 1 === after)
                const isDiscarded = activeDiscarded.has(p.page)
                return (
                  <li key={p.page}>
                    <div
                      className={`flex gap-3 rounded-xl border border-line bg-card p-2 ${isDiscarded ? 'opacity-50' : ''}`}
                    >
                      <button
                        type="button"
                        aria-label={`Open page ${p.page}`}
                        onClick={() => {
                          setCursor(Math.min(p.page, view.page_count - 1))
                          setViewing(p.page)
                        }}
                        className="relative h-28 w-20 shrink-0 cursor-zoom-in overflow-hidden rounded-md border border-line2 bg-panel2 hover:border-accent"
                      >
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
                        {isDiscarded && (
                          <span className="absolute inset-0 flex items-center justify-center bg-bg/60 font-mono text-[10px] text-danger line-through">
                            discarded
                          </span>
                        )}
                      </button>
                      <div className="min-w-0 flex-1 text-[11px]">
                        <div className="flex items-center gap-2 font-mono text-[10px] text-muted">
                          Page {p.page}
                          {p.blank && <Badge tone="warning">blank?</Badge>}
                        </div>
                        <p className="line-clamp-2 text-ink2" title={p.text_head}>
                          <span className="font-mono text-[9px] text-muted uppercase">starts </span>
                          {p.text_head}
                        </p>
                        <p className="mt-1 line-clamp-1 text-muted" title={p.text_tail}>
                          <span className="font-mono text-[9px] uppercase">ends </span>…
                          {p.text_tail}
                        </p>
                        <button
                          type="button"
                          aria-pressed={isDiscarded}
                          aria-label={`${isDiscarded ? 'Keep' : 'Discard'} page ${p.page}`}
                          onClick={() => toggleDiscard(p.page)}
                          className={`mt-1 flex items-center gap-1 rounded-md border px-2 py-0.5 text-[10.5px] ${isDiscarded ? 'border-danger/50 text-danger' : 'border-line3 text-muted hover:text-ink'}`}
                        >
                          <Icon name={isDiscarded ? 'undo' : 'delete'} size={13} />{' '}
                          {isDiscarded ? 'Keep page' : 'Discard page'}
                        </button>
                      </div>
                    </div>
                    {i < view.pages.length - 1 && (
                      <div className="my-1 flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => {
                            setCursor(after)
                            toggle(after)
                          }}
                          aria-pressed={isCut}
                          aria-label={`Split after page ${after}`}
                          className={`flex min-w-0 flex-1 items-center gap-2 rounded-md border px-3 py-1 text-[10.5px] ${isCut ? 'border-accent bg-accent/12 text-ink' : cursor === after ? 'border-line3 text-muted' : 'border-transparent text-muted2 hover:border-line3'}`}
                        >
                          <Icon name="content_cut" size={13} />{' '}
                          {isCut
                            ? `Part ${sorted.indexOf(after) + 2} starts on page ${after + 1}`
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
                        {isCut && (
                          <div role="group" aria-label={`Part ${sorted.indexOf(after) + 2} is`}>
                            {(['letter', 'attachment'] as const).map((k) => (
                              <button
                                key={k}
                                type="button"
                                aria-pressed={cutKind === k}
                                onClick={() => {
                                  setCursor(after)
                                  setKind(after, k)
                                }}
                                className={`border px-2 py-1 text-[10.5px] first:rounded-l-md last:rounded-r-md ${cutKind === k ? 'border-accent bg-accent/12 text-ink' : 'border-line3 text-muted hover:text-ink'}`}
                              >
                                {k === 'letter' ? 'New letter' : 'Attachment'}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          </>
        )}
      </div>
      {viewing !== null && view.status === 'ready' && (
        <PageViewer
          batchId={batchId}
          page={viewing}
          pages={view.pages}
          cuts={activeCuts}
          discarded={activeDiscarded}
          onToggleDiscard={toggleDiscard}
          onPage={setViewing}
          onClose={() => setViewing(null)}
          onToggleCut={toggle}
          onSetKind={setKind}
        />
      )}
    </div>
  )
}
