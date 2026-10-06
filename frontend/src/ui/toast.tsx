import { createContext, type ReactNode, useCallback, useContext, useMemo, useState } from 'react'

import { Icon } from './Icon'

type Kind = 'success' | 'error'
type Toast = { id: number; kind: Kind; message: string }

const ToastContext = createContext<(message: string, kind?: Kind) => void>(() => {})

/** `const toast = useToast(); toast('Saved')` — transient bottom-right notices. */
export function useToast() {
  return useContext(ToastContext)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = useCallback((message: string, kind: Kind = 'success') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, kind, message }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000)
  }, [])
  const value = useMemo(() => push, [push])
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        className="pointer-events-none fixed right-4 bottom-4 z-200 flex flex-col gap-2"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`flex items-center gap-2 rounded-xl border px-3 py-2 text-[12px] shadow-[0_16px_40px_rgba(0,0,0,.5)] ${
              t.kind === 'error'
                ? 'border-danger/40 bg-card text-danger'
                : 'border-accent/30 bg-card text-ink'
            }`}
          >
            <Icon name={t.kind === 'error' ? 'error' : 'check_circle'} size={16} />
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
