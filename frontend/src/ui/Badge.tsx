import type { ReactNode } from 'react'

const tones = {
  neutral: 'border-line text-muted',
  accent: 'border-accent/30 bg-accent/10 text-tealink',
  danger: 'border-danger/40 bg-danger/10 text-danger',
  warning: 'border-warning/40 bg-warning/10 text-warning',
  success: 'border-success/40 bg-success/10 text-success',
}

export type Tone = keyof typeof tones

type Props = { tone?: Tone; mono?: boolean; className?: string; children: ReactNode }

export function Badge({ tone = 'neutral', mono = false, className = '', children }: Props) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-bold tracking-wide uppercase ${mono ? 'font-mono' : ''} ${tones[tone]} ${className}`}
    >
      {children}
    </span>
  )
}
