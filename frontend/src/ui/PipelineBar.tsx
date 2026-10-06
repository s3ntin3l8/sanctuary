import type { Schemas } from '../api/client'

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
