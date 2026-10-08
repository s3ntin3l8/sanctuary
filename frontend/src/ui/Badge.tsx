import type { ReactNode } from 'react'

const outlined = {
  neutral: 'border-line text-muted',
  accent: 'border-accent/30 bg-accent/10 text-tealink',
  danger: 'border-danger/40 bg-danger/10 text-danger',
  warning: 'border-warning/40 bg-warning/10 text-warning',
  success: 'border-success/40 bg-success/10 text-success',
}

// Borderless tinted label (the reference's confidence / summary-kind pills).
const pilled = {
  neutral: 'bg-line2 text-muted',
  accent: 'bg-accent/12 text-tealink',
  danger: 'bg-danger/13 text-danger',
  warning: 'bg-warning/13 text-warning',
  success: 'bg-success/13 text-success',
}

export type Tone = keyof typeof outlined

type Props = {
  tone?: Tone
  mono?: boolean
  pill?: boolean
  className?: string
  children: ReactNode
}

export function Badge({
  tone = 'neutral',
  mono = false,
  pill = false,
  className = '',
  children,
}: Props) {
  const shape = pill
    ? 'rounded px-[7px] py-0.5 text-[8.5px] font-extrabold tracking-[.06em]'
    : 'rounded-md border px-1.5 py-0.5 text-[10px] font-bold tracking-wide'
  return (
    <span
      className={`inline-flex items-center gap-1 uppercase ${shape} ${mono ? 'font-mono' : ''} ${pill ? pilled[tone] : outlined[tone]} ${className}`}
    >
      {children}
    </span>
  )
}
