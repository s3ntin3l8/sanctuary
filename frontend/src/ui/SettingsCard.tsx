import type { ReactNode } from 'react'

export function SettingsCard({
  title,
  description,
  children,
  danger,
}: {
  title: string
  description?: string
  children: ReactNode
  danger?: boolean
}) {
  return (
    <section
      className={`rounded-2xl border bg-card p-5 ${danger ? 'border-danger/40' : 'border-line'}`}
    >
      <h2 className={`font-display text-[14px] font-bold ${danger ? 'text-danger' : ''}`}>
        {title}
      </h2>
      {description && <p className="mt-0.5 text-[11.5px] text-muted">{description}</p>}
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  )
}
