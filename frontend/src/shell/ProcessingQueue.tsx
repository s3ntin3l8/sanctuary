import { useState } from 'react'
import { Link } from 'react-router'

import { useRetryFailed, useWorkerQueue } from '../api/shell'
import type { Schemas } from '../api/client'
import { GmailImportStatus } from '../features/import/GmailImportStatus'
import { pluralize } from '../format'
import { Button } from '../ui/Button'
import { Icon } from '../ui/Icon'
import { Modal } from '../ui/Modal'
import { CountBadge, RailButton } from './RailButton'

const STAGES = [
  'extract',
  'metadata',
  'batch_analysis',
  'enrich',
  'relationships',
  'claims',
  'entities',
  'embeddings',
] as const

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
        badge={
          failed > 0 ? (
            <CountBadge count={failed} tone="danger" />
          ) : (
            <CountBadge count={active} tone="warning" />
          )
        }
      />
      {queue && (
        <div className="pointer-events-none absolute bottom-0 left-12 z-90 hidden w-64 rounded-xl border border-line bg-card p-3 text-[11px] shadow-[0_18px_44px_rgba(0,0,0,.45)] group-hover:block">
          <div className="mb-1 font-semibold text-ink">
            Processing · {pluralize(active, 'item')} active
            {failed > 0 && <span className="text-danger"> · {failed} failed</span>}
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

function QueueModal({
  open,
  onClose,
  queue,
}: {
  open: boolean
  onClose: () => void
  queue: Schemas['QueueView'] | undefined
}) {
  const retry = useRetryFailed()
  const total = queue ? queue.counts.executing + queue.counts.queued : 0
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Processing queue"
      subtitle={
        queue
          ? `${total} active · ${queue.counts.failed} failed · ${queue.counts.ai_inflight} AI calls`
          : undefined
      }
      icon="sync"
      width={560}
      footer={
        <>
          <a
            href="/triage"
            className="mr-auto self-center text-[12px] text-tealink hover:underline"
          >
            Open triage
          </a>
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
      <div className="mb-3 flex flex-wrap gap-1 font-mono text-[9px] text-muted">
        {STAGES.map((s, i) => (
          <span key={s}>
            {i > 0 && <span className="mx-0.5">›</span>}
            {s.replace('_', ' ')}
          </span>
        ))}
      </div>
      {retry.error && <p className="mb-2 text-[11px] text-danger">{retry.error.message}</p>}
      {queue?.gmail_import && (
        <div className="mb-3">
          <GmailImportStatus run={queue.gmail_import} variant="compact" onNavigate={onClose} />
        </div>
      )}
      {queue && total === 0 && queue.counts.failed === 0 ? (
        <p className="py-6 text-center text-[12px] text-muted">All document pipelines idle.</p>
      ) : (
        <div className="max-h-[60vh] space-y-4 overflow-y-auto">
          <QueueSection
            title="Executing"
            items={queue?.executing ?? []}
            tone="warning"
            onNavigate={onClose}
          />
          <QueueSection
            title="Queued"
            items={queue?.queued ?? []}
            tone="muted"
            onNavigate={onClose}
          />
          {queue && queue.failed.length > 0 && (
            <section>
              <h3 className="mb-1 text-[10px] font-extrabold tracking-[.12em] text-danger uppercase">
                Failed
              </h3>
              <ul className="divide-y divide-line2">
                {queue.failed.map((f) => (
                  <li key={f.doc_id} className="flex items-center gap-2 py-1.5 text-[12px]">
                    <span className="font-mono text-[10px] text-muted">D#{f.doc_id}</span>
                    <span className="min-w-0 flex-1 truncate">{f.title}</span>
                    <span className="truncate font-mono text-[10px] text-danger">
                      {f.stage}: {f.error.slice(0, 60)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </Modal>
  )
}

function QueueSection({
  title,
  items,
  tone,
  onNavigate,
}: {
  title: string
  items: Schemas['QueueItem'][]
  tone: 'warning' | 'muted'
  onNavigate: () => void
}) {
  if (items.length === 0) return null
  return (
    <section>
      <h3
        className={`mb-1 text-[10px] font-extrabold tracking-[.12em] uppercase ${tone === 'warning' ? 'text-warning' : 'text-muted'}`}
      >
        {title}
      </h3>
      <ul className="divide-y divide-line2">
        {items.map((item) => (
          <li
            key={`${item.kind}-${item.batch_id}-${item.doc_id}-${item.stage}`}
            className="flex items-center gap-2 py-1.5 text-[12px]"
          >
            <span className="font-mono text-[10px] text-muted">
              {item.kind === 'doc'
                ? `D#${item.doc_id}`
                : `${item.kind === 'slicing' ? 'S' : 'B'}#${item.batch_id}`}
            </span>
            <span className="min-w-0 flex-1 truncate">
              {item.label}
              {item.note && <span className="ml-2 text-[10px] text-muted">{item.note}</span>}
            </span>
            {item.kind === 'batch' && (
              <span className="font-mono text-[10px] text-muted">
                {pluralize(item.doc_count, 'doc')}
              </span>
            )}
            {item.kind === 'slicing' ? (
              <Link
                to={`/ingest/slice/${item.batch_id}`}
                onClick={onNavigate}
                className="font-mono text-[10px] text-tealink hover:underline"
              >
                review cuts
              </Link>
            ) : (
              <span className="font-mono text-[10px] text-tealink">{item.stage}</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
