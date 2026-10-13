import { type ReactNode, useEffect, useEffectEvent, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useOverlay } from './useOverlay'

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
  const panel = useRef<HTMLDivElement>(null)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  useOverlay(open, panel, onClose, false)
  const positionPanel = useEffectEvent(() => {
    if (!root.current || !panel.current) return
    const anchor = root.current.getBoundingClientRect()
    const height = panel.current.getBoundingClientRect().height
    setPosition({
      left: anchor.right + 8,
      top: Math.max(8, Math.min(anchor.bottom - height, window.innerHeight - height - 8)),
    })
  })
  useLayoutEffect(() => {
    if (open) positionPanel()
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (
        !root.current?.contains(event.target as Node) &&
        !panel.current?.contains(event.target as Node)
      )
        onClose()
    }
    const onScroll = (event: Event) => {
      if (!panel.current?.contains(event.target as Node)) positionPanel()
    }
    const observer = new ResizeObserver(() => positionPanel())
    if (panel.current) observer.observe(panel.current)
    window.addEventListener('mousedown', onDown)
    window.addEventListener('resize', onClose)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      observer.disconnect()
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', onClose)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open, onClose])

  return (
    <div ref={root} className="relative shrink-0">
      {anchor}
      {open &&
        createPortal(
          <div
            ref={panel}
            tabIndex={-1}
            style={position}
            role={role}
            aria-label={label}
            className={`fixed z-110 max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-xl border border-line bg-card shadow-[0_20px_50px_rgba(0,0,0,.5)] ${className}`}
          >
            {children}
          </div>,
          document.body,
        )}
    </div>
  )
}
