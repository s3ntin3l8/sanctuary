export function Progress({ value, max, label }: { value: number; max: number; label: string }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={max}
      className="h-1.5 w-full overflow-hidden rounded-full bg-line2"
    >
      <div className="h-full bg-accent transition-[width]" style={{ width: `${pct}%` }} />
    </div>
  )
}
