import type { Schemas } from '../api/client'

type Stage = Schemas['StageView']

const COLORS: Record<string, string> = {
  completed: 'bg-accent',
  running: 'bg-warning animate-pulse',
  retrying: 'bg-warning',
  failed: 'bg-danger',
  skipped: 'bg-line3',
  dismissed: 'bg-line3',
  pending: 'bg-line',
}

/** Seven (or eight) tiny segments, one per pipeline stage, with a hover legend. */
export function PipelineBar({ stages, size = 'sm' }: { stages: Stage[]; size?: 'sm' | 'md' }) {
  const h = size === 'sm' ? 'h-1' : 'h-1.5'
  const done = stages.filter((s) => s.status === 'completed' || s.status === 'skipped').length
  const title = stages
    .map((s) => `${s.label}: ${s.status ?? 'queued'}${s.error ? ` — ${s.error}` : ''}`)
    .join('\n')
  return (
    <span
      className="inline-flex items-center gap-0.5"
      title={title}
      aria-label={`${done} of ${stages.length} stages complete`}
    >
      {stages.map((s) => (
        <span
          key={s.key}
          className={`${h} w-2.5 rounded-sm ${COLORS[s.status ?? 'pending'] ?? COLORS.pending}`}
        />
      ))}
    </span>
  )
}

/** Per-bundle state bar: completed / in progress / failed share of the documents. */
export function BundleStateBar({ pipeline }: { pipeline: Schemas['BundlePipeline'] }) {
  const total = pipeline.total || 1
  const n = (k: keyof typeof pipeline.counts) => pipeline.counts[k] ?? 0
  const pct = (v: number) => `${(100 * v) / total}%`
  return (
    <span className="inline-flex h-1.5 w-16 overflow-hidden rounded-sm bg-line" aria-hidden>
      <span className="bg-accent" style={{ width: pct(n('completed')) }} />
      <span
        className="bg-warning"
        style={{ width: pct(n('running') + n('partial') + n('pending')) }}
      />
      <span className="bg-danger" style={{ width: pct(n('failed')) }} />
    </span>
  )
}
