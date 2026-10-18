import { formatIsoDate } from '../../time'
import type { TimeAxis } from './historyModel'

/** A span on the shared time axis; a single day collapses to a dot. */
export function TimelineBar({
  axis,
  from,
  to,
  tone = 'muted',
}: {
  axis: TimeAxis
  from: string
  to: string
  tone?: 'muted' | 'accent' | 'success'
}) {
  const left = axis.pos(from) * 100
  const width = Math.max(0, (axis.pos(to) - axis.pos(from)) * 100)
  const color = { muted: 'bg-muted/50', accent: 'bg-accent', success: 'bg-success/70' }[tone]
  return (
    <div
      className="relative h-3"
      title={`${formatIsoDate(from)} – ${formatIsoDate(to)}`}
      aria-hidden="true"
    >
      <span
        className={`absolute top-1/2 h-1.5 min-w-1.5 -translate-y-1/2 rounded-full ${color}`}
        style={{ left: `${left}%`, width: `${width}%` }}
      />
    </div>
  )
}

/** Year ticks for the table header, on the same axis as the bars. */
export function TimelineAxis({ axis }: { axis: TimeAxis }) {
  return (
    <div className="relative h-3" aria-hidden="true">
      {axis.years.map(({ year, at }) => (
        <span
          key={year}
          className="absolute top-0 border-l border-line2 pl-1 font-mono text-[9px] leading-3 text-muted"
          style={{ left: `${at * 100}%` }}
        >
          {year}
        </span>
      ))}
    </div>
  )
}
