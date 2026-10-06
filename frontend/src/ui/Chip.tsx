import type { ButtonHTMLAttributes } from 'react'

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean; count?: number }

/** A filter pill; `active` renders the accent-tinted state. */
export function Chip({ active = false, count, children, className = '', ...rest }: Props) {
  return (
    <button
      type="button"
      aria-pressed={active}
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors ${
        active
          ? 'border-accent/30 bg-accent/12 text-ink'
          : 'border-line bg-transparent text-muted hover:text-ink'
      } ${className}`}
      {...rest}
    >
      {children}
      {count !== undefined && <span className="font-mono text-[10px] text-muted">{count}</span>}
    </button>
  )
}
