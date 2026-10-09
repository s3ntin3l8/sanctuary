import { type InputHTMLAttributes, useId } from 'react'

import { inputClass } from './Field'

type Props = InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }

export function TextField({ label, hint, id, className, ...rest }: Props) {
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
        className={`mt-1 ${inputClass}${className ? ` ${className}` : ''}`}
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
