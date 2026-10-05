import type { ReactNode } from 'react'

export function Alert({ children }: { children: ReactNode }) {
  return (
    <div
      role="alert"
      className="rounded-[9px] border border-danger/30 bg-danger/10 px-3 py-2 text-xs font-medium text-danger"
    >
      {children}
    </div>
  )
}
