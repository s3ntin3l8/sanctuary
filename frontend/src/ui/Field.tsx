import type { ReactNode } from 'react'

/**
 * Label + control + hint. Without `htmlFor` the control is wrapped by the
 * label, so a single input/select/textarea child is associated implicitly.
 */
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
  const caption = (
    <span className="text-[10px] font-bold tracking-[.1em] text-muted uppercase">{label}</span>
  )
  return (
    <div>
      {htmlFor ? (
        <>
          <label htmlFor={htmlFor}>{caption}</label>
          <div className="mt-1">{children}</div>
        </>
      ) : (
        <label className="block">
          {caption}
          <div className="mt-1">{children}</div>
        </label>
      )}
      {hint && <p className="mt-1 text-[10px] text-muted">{hint}</p>}
    </div>
  )
}

export const inputClass =
  'w-full rounded-[9px] border border-line bg-panel2 px-3 py-2 text-[13px] text-ink placeholder:text-muted2 focus:border-accent focus:outline-none'
