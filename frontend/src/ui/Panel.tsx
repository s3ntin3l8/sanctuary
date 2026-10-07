import type { ReactNode } from 'react'

import { Icon } from './Icon'

type Props = {
  title: string
  icon?: string
  meta?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
}

/** A titled card section: uppercase label row, then content. */
export function Panel({ title, icon, meta, action, children, className = '' }: Props) {
  return (
    <section aria-label={title} className={`rounded-xl border border-line bg-card ${className}`}>
      <header className="flex items-center gap-2 border-b border-line2 px-4 py-2.5">
        {icon && <Icon name={icon} size={14} className="text-muted" />}
        <h2 className="text-[10px] font-extrabold tracking-[.12em] text-ink uppercase">{title}</h2>
        {meta && <span className="font-mono text-[10px] text-muted">{meta}</span>}
        {action && <div className="ml-auto">{action}</div>}
      </header>
      <div className="px-4 py-3">{children}</div>
    </section>
  )
}
