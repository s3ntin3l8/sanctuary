import { type InputHTMLAttributes, useId } from 'react'

type Props = InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }

export function TextField({ label, hint, id, ...rest }: Props) {
  const generatedId = useId()
  const inputId = id ?? generatedId
  const hintId = `${inputId}-hint`
  return (
    <div>
      <label
        htmlFor={inputId}
        className="text-[10px] font-bold tracking-[.1em] text-muted uppercase"
      >
        {label}
      </label>
      <input
        id={inputId}
        aria-describedby={hint ? hintId : undefined}
        className="mt-1 w-full rounded-[9px] border border-line bg-panel2 px-3 py-2 text-[13px] text-ink placeholder:text-muted2 focus:border-accent focus:outline-none"
        {...rest}
      />
      {hint && (
        <p id={hintId} className="mt-1 text-[10px] text-muted">
          {hint}
        </p>
      )}
    </div>
  )
}
