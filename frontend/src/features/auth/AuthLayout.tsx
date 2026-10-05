import type { ReactNode } from 'react'

import { Alert } from '../../ui/Alert'

type Props = {
  subtitle: string
  error?: string | null
  footer?: ReactNode
  children: ReactNode
}

export function AuthLayout({ subtitle, error, footer, children }: Props) {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center">
          <div
            aria-hidden
            className="mb-3 h-11 w-10 bg-accent [clip-path:polygon(0_0,100%_0,100%_70%,50%_100%,0_70%)]"
          />
          <h1 className="font-display text-lg font-extrabold tracking-tight">The Sanctuary</h1>
          <p className="mt-1 text-xs text-muted">{subtitle}</p>
        </div>
        {error && (
          <div className="mb-4">
            <Alert>{error}</Alert>
          </div>
        )}
        <div className="rounded-2xl border border-line bg-card p-6 shadow-[0_24px_60px_rgba(0,0,0,.35)]">
          {children}
        </div>
        {footer && <p className="mt-5 text-center text-xs text-muted">{footer}</p>}
      </div>
    </main>
  )
}
