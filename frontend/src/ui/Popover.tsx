import { type ReactNode, useEffect, useRef } from 'react'

type Props = {
  open: boolean
  onClose: () => void
  /** The element the panel is anchored to; rendered in place. */
  anchor: ReactNode
  /** Accessible role of the panel: a list of links or a block of content. */
  role?: 'menu' | 'dialog'
  label: string
  className?: string
  children: ReactNode
}

/**
 * A panel that opens beside its anchor and closes on outside click or Escape.
 * Rail controls (profile menu, notifications) use it to the right of the rail;
 * pass `className` for width and stacking.
 */
export function Popover({
  open,
  onClose,
  anchor,
  role = 'dialog',
  label,
  className = '',
  children,
}: Props) {
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) onClose()
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, onClose])

  return (
    <div ref={root} className="relative">
      {anchor}
      {open && (
        <div
          role={role}
          aria-label={label}
          className={`absolute bottom-0 left-12 z-110 rounded-xl border border-line bg-card shadow-[0_20px_50px_rgba(0,0,0,.5)] ${className}`}
        >
          {children}
        </div>
      )}
    </div>
  )
}
