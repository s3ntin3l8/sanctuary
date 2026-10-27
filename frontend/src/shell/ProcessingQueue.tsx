import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { useRetryFailed, useRetryStage, useWorkerQueue } from '../api/shell'
import type { Schemas } from '../api/client'
import { GmailImportStatus } from '../features/import/GmailImportStatus'
import { pluralize } from '../format'
import { formatAgo, toMillis } from '../time'
import { Button } from '../ui/Button'
import { Icon } from '../ui/Icon'
import { Modal } from '../ui/Modal'
import { RailButton } from './RailButton'

type Queue = Schemas['QueueView']
type Tab = 'active' | 'queued' | 'failed' | 'recent'

/** Rail control: badge with live counts, hover summary, and the queue modal. */
export function ProcessingQueue() {
  const queue = useWorkerQueue().data
  const [open, setOpen] = useState(false)
  const counts = queue?.counts
  const active = (counts?.executing ?? 0) + (counts?.queued ?? 0)
  const failed = counts?.failed ?? 0

  return (
    <div className="group relative">
      <RailButton
        icon="sync"
        label="Processing queue"
        onClick={() => setOpen(true)}
        count={failed > 0 ? failed : active}
        tone={failed > 0 ? 'danger' : 'warning'}
      />
      {queue && (
        <div className="pointer-events-none absolute bottom-0 left-12 z-90 hidden w-64 rounded-xl border border-line bg-card p-3 text-[11px] shadow-[0_18px_44px_rgba(0,0,0,.45)] group-hover:block">
          <div className="mb-1 font-semibold text-ink">
            Processing · {pluralize(active, 'item')} active
            {failed > 0 && <span className="text-danger"> · {failed} failed</span>}
            {failed > queue.failed.length && (
              <span className="text-muted"> (listing {queue.failed.length})</span>
            )}
          </div>
          <div className="text-muted">
            {counts?.executing} executing · {counts?.queued} queued · {counts?.ai_inflight} AI calls
          </div>
          {queue.gmail_import?.active && (
            <div className="mt-1 text-muted">
              Importing from Gmail · {queue.gmail_import.done}/{queue.gmail_import.total}
            </div>
          )}
          <div className="mt-2 flex items-center gap-1 text-[10px] text-muted">
            <Icon name="lock" size={12} /> local
          </div>
        </div>
      )}
      <QueueModal open={open} onClose={() => setOpen(false)} queue={queue} />
    </div>
  )
}

/** A clock that ticks every second while `active`, for live elapsed/countdown text. */
function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [active])
  return now
}

function formatSpan(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

function QueueModal({
  open,
  onClose,
  queue,
}: {
  open: boolean
  onClose: () => void
  queue: Queue | undefined
}) {
  const retry = useRetryFailed()
  const [picked, setPicked] = useState<Tab | null>(null)
  const now = useNow(open)
  const executing = queue?.counts.executing ?? 0
  const queued = queue?.counts.queued ?? 0
  const failed = queue?.counts.failed ?? 0
  // Failures need attention first; otherwise show what is running.
  const tab = picked ?? (failed > 0 ? 'failed' : 'active')
  const close = () => {
    setPicked(null)
    onClose()
  }
  const tabs: { key: Tab; label: string; count: number | null }[] = [
    { key: 'active', label: 'Active', count: executing },
    { key: 'queued', label: 'Queued', count: queued },
    { key: 'failed', label: 'Failed', count: failed },
    { key: 'recent', label: 'Recent', count: null },
  ]
  return (
    <Modal
      open={open}
      onClose={close}
      title="Processing queue"
      subtitle={
        queue
          ? `${executing + queued} active · ${failed} failed · ${queue.counts.ai_inflight} AI calls`
          : undefined
      }
      icon="sync"
      width={640}
      footer={
        <>
          <Link
            to="/triage"
            onClick={close}
            className="mr-auto self-center text-[12px] text-tealink hover:underline"
          >
            Open triage
          </Link>
          <Button
            variant="secondary"
            disabled={!queue?.counts.failed || retry.isPending}
            onClick={() => retry.mutate()}
          >
            <Icon name="replay" size={16} /> Retry failed
          </Button>
        </>
      }
    >
      {queue && <SystemStrip queue={queue} />}
      {retry.error && <p className="mb-2 text-[11px] text-danger">{retry.error.message}</p>}
      {queue?.gmail_import && (
        <div className="mb-3">
          <GmailImportStatus run={queue.gmail_import} variant="compact" onNavigate={close} />
        </div>
      )}
      <div role="tablist" aria-label="Queue view" className="mb-2 flex gap-1 border-b border-line">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setPicked(t.key)}
            className={`-mb-px border-b-2 px-3 py-1.5 text-[12px] ${
              tab === t.key
                ? 'border-accent font-semibold text-ink'
                : 'border-transparent text-muted hover:text-ink'
            }`}
          >
            {t.label}
            {t.count !== null && (
              <span
                className={`ml-1.5 font-mono text-[10px] ${t.key === 'failed' && t.count > 0 ? 'text-danger' : 'text-muted'}`}
              >
                {t.count}
              </span>
            )}
          </button>
        ))}
      </div>
      <div role="tabpanel" className="max-h-[48vh] overflow-y-auto">
        {!queue ? null : tab === 'failed' ? (
          <FailedList failed={queue.failed} total={queue.counts.failed} onNavigate={close} />
        ) : tab === 'recent' ? (
          <RecentList recent={queue.recent} now={now} onNavigate={close} />
        ) : (
          <QueueList
            items={tab === 'active' ? queue.executing : queue.queued}
            empty={
              tab === 'active'
                ? 'All document pipelines idle.'
                : 'Nothing waiting — every document is being processed or done.'
            }
            now={now}
            onNavigate={close}
          />
        )}
      </div>
    </Modal>
  )
}

function SystemStrip({ queue }: { queue: Queue }) {
  const { workers, queue_depth: depth, gpu_gate: gate } = queue.system
  return (
    <div
      aria-label="System status"
      className="mb-3 space-y-1 rounded-xl border border-line bg-card2 px-3 py-2 font-mono text-[10.5px] text-muted"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {workers === null ? (
          <span>Workers: unknown (broker unreachable)</span>
        ) : (
          workers.map((w) => (
            <span key={w.name} className="flex items-center gap-1" title={`${w.concurrency} slots`}>
              <span
                aria-hidden
                className={`inline-block h-1.5 w-1.5 rounded-full ${w.up ? 'bg-success' : 'bg-danger'}`}
              />
              {w.name} {w.up ? 'up' : 'down'}
            </span>
          ))
        )}
        {depth && (
          <span>
            backlog {depth.ingest} ingest · {depth.ai} ai · {depth.maintenance} maint
          </span>
        )}
        {gate && (
          <span>
            GPU {gate.holders.length ? gate.holders.join(' + ') : 'free'}
            {gate.waiting > 0 && ` · ${gate.waiting} waiting`}
          </span>
        )}
      </div>
      {queue.ai_calls.length > 0 && (
        <div className="truncate" title={queue.ai_calls.join(', ')}>
          AI now: {queue.ai_calls.join(' · ')}
        </div>
      )}
    </div>
  )
}

function itemLink(item: Schemas['QueueItem']): string {
  if (item.kind === 'slicing') return `/ingest/slice/${item.batch_id}`
  if (item.kind === 'doc' && item.doc_id) return `/document/${item.doc_id}`
  return `/triage?bundle=batch-${item.batch_id}`
}

function QueueList({
  items,
  empty,
  now,
  onNavigate,
}: {
  items: Schemas['QueueItem'][]
  empty: string
  now: number
  onNavigate: () => void
}) {
  if (items.length === 0) return <p className="py-6 text-center text-[12px] text-muted">{empty}</p>
  return (
    <ul className="divide-y divide-line2">
      {items.map((item) => {
        const wait = item.next_at ? toMillis(item.next_at) - now : null
        const detail = [
          item.started_at && `running ${formatSpan(now - toMillis(item.started_at))}`,
          item.attempt != null &&
            item.max_attempts != null &&
            `attempt ${item.attempt}/${item.max_attempts}`,
          wait !== null && wait > 0 && `retry in ${formatSpan(wait)}`,
          item.kind === 'batch' && pluralize(item.doc_count, 'doc'),
          item.note,
        ].filter(Boolean)
        return (
          <li
            key={`${item.kind}-${item.batch_id}-${item.doc_id}-${item.stage}`}
            className="flex items-center gap-2 py-1.5 text-[12px]"
          >
            <span className="font-mono text-[10px] text-muted">
              {item.kind === 'doc'
                ? `D#${item.doc_id}`
                : `${item.kind === 'slicing' ? 'S' : 'B'}#${item.batch_id}`}
            </span>
            <span className="min-w-0 flex-1">
              <Link
                to={itemLink(item)}
                onClick={onNavigate}
                className="block truncate hover:underline"
              >
                {item.label}
              </Link>
              {detail.length > 0 && (
                <span className="block truncate font-mono text-[10px] text-muted">
                  {detail.join(' · ')}
                </span>
              )}
            </span>
            <span className="font-mono text-[10px] text-tealink">
              {item.kind === 'slicing' ? 'review cuts' : item.stage?.replace('_', ' ')}
            </span>
          </li>
        )
      })}
    </ul>
  )
}

function FailedList({
  failed,
  total,
  onNavigate,
}: {
  failed: Schemas['FailedDoc'][]
  total: number
  onNavigate: () => void
}) {
  const retry = useRetryStage()
  const [open, setOpen] = useState<number | null>(null)
  if (failed.length === 0)
    return <p className="py-6 text-center text-[12px] text-muted">Nothing has failed.</p>
  return (
    <>
      <ul className="divide-y divide-line2">
        {failed.map((f) => {
          const root = f.stages[0]
          return (
            <li key={f.doc_id} className="py-1.5 text-[12px]">
              <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] text-muted">D#{f.doc_id}</span>
                <Link
                  to={`/document/${f.doc_id}`}
                  onClick={onNavigate}
                  className="min-w-0 flex-1 truncate hover:underline"
                >
                  {f.title}
                </Link>
                <span className="font-mono text-[10px] text-danger">
                  {f.stages.map((s) => s.stage.replace('_', ' ')).join(' · ')}
                </span>
                <button
                  type="button"
                  aria-expanded={open === f.doc_id}
                  aria-label={`Details for D#${f.doc_id}`}
                  onClick={() => setOpen(open === f.doc_id ? null : f.doc_id)}
                  className="text-muted hover:text-ink"
                >
                  <Icon name={open === f.doc_id ? 'expand_less' : 'expand_more'} size={16} />
                </button>
                {root && (
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={retry.isPending}
                    onClick={() =>
                      retry.mutate({
                        docId: f.doc_id,
                        stage: root.stage as Schemas['PipelineStage'],
                      })
                    }
                  >
                    Retry
                  </Button>
                )}
              </div>
              {open === f.doc_id && (
                <ul className="mt-1 space-y-1 rounded-lg bg-card2 p-2 font-mono text-[10.5px]">
                  {f.stages.map((s) => (
                    <li key={s.stage}>
                      <span className="text-danger">{s.stage}</span>
                      {s.attempt != null && (
                        <span className="text-muted"> (attempt {s.attempt})</span>
                      )}
                      <div className="break-words whitespace-pre-wrap text-muted">
                        {s.error || 'No error text recorded.'}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
      {retry.error && <p className="mt-2 text-[11px] text-danger">{retry.error.message}</p>}
      {total > failed.length && (
        <p className="mt-2 text-[10.5px] text-muted">
          Showing {failed.length} of {total} failed documents.
        </p>
      )}
    </>
  )
}

function RecentList({
  recent,
  now,
  onNavigate,
}: {
  recent: Schemas['RecentStage'][]
  now: number
  onNavigate: () => void
}) {
  if (recent.length === 0)
    return (
      <p className="py-6 text-center text-[12px] text-muted">
        Nothing finished in the last 24 hours.
      </p>
    )
  return (
    <ul className="divide-y divide-line2">
      {recent.map((r) => (
        <li key={`${r.doc_id}-${r.stage}`} className="flex items-center gap-2 py-1.5 text-[12px]">
          <span className="font-mono text-[10px] text-muted">D#{r.doc_id}</span>
          <Link
            to={`/document/${r.doc_id}`}
            onClick={onNavigate}
            className="min-w-0 flex-1 truncate hover:underline"
          >
            {r.title}
          </Link>
          <span className="font-mono text-[10px] text-tealink">{r.stage.replace('_', ' ')}</span>
          <span className="w-12 text-right font-mono text-[10px] text-muted">
            {r.duration_ms != null ? formatSpan(r.duration_ms) : ''}
          </span>
          <span className="w-10 text-right font-mono text-[10px] text-muted">
            {formatAgo(r.completed_at, new Date(now))}
          </span>
        </li>
      ))}
    </ul>
  )
}
