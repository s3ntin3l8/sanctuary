import type { ReactNode } from 'react'

/** Label + control + hint, for controls that are not a plain text input. */
export function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string
  hint?: string
  htmlFor?: string
  children: ReactNode
}) {
  return (
    <div>
      <label
        htmlFor={htmlFor}
        className="text-[10px] font-bold tracking-[.1em] text-muted uppercase"
      >
        {label}
      </label>
      <div className="mt-1">{children}</div>
      {hint && <p className="mt-1 text-[10px] text-muted">{hint}</p>}
    </div>
  )
}

export const inputClass =
  'w-full rounded-[9px] border border-line bg-panel2 px-3 py-2 text-[13px] text-ink placeholder:text-muted2 focus:border-accent focus:outline-none'
