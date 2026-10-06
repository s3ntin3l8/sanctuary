import { type ReactNode, useEffect, useId, useRef } from 'react'

import { Icon } from './Icon'

type Props = {
  open: boolean
  onClose: () => void
  title: string
  subtitle?: string
  icon?: string
  width?: number
  children: ReactNode
  footer?: ReactNode
}

/** A centred dialog with backdrop; closes on Escape and backdrop click. */
export function Modal({
  open,
  onClose,
  title,
  subtitle,
  icon,
  width = 460,
  children,
  footer,
}: Props) {
  const panel = useRef<HTMLDivElement>(null)
  const titleId = useId()

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    panel.current?.querySelector<HTMLElement>('input, button, select, textarea')?.focus()
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-140 flex items-center justify-center bg-[rgba(6,14,32,.62)] p-4 backdrop-blur-[3px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal
        aria-labelledby={titleId}
        className="w-full rounded-2xl border border-line bg-card shadow-[0_24px_60px_rgba(0,0,0,.6)]"
        style={{ maxWidth: width }}
      >
        <header className="flex items-center gap-3 border-b border-line2 px-5 py-4">
          {icon && <Icon name={icon} size={20} className="text-accent" />}
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="font-display text-[15px] font-bold">
              {title}
            </h2>
            {subtitle && <p className="font-mono text-[11px] text-muted">{subtitle}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1 text-muted hover:bg-accent/7 hover:text-ink"
          >
            <Icon name="close" size={18} />
          </button>
        </header>
        <div className="px-5 py-4">{children}</div>
        {footer && (
          <footer className="flex justify-end gap-2 border-t border-line2 px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  )
}
