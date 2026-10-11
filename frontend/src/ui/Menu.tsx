import { type ReactNode, type RefObject, useLayoutEffect, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

type Props = {
  open: boolean
  onClose: () => void
  /** The trigger the panel hangs from. */
  anchorRef: RefObject<HTMLElement | null>
  /** Which edge of the trigger the panel lines up with. */
  align?: 'left' | 'right'
  role?: 'menu' | 'dialog'
  label: string
  className?: string
  children: ReactNode
}

const GAP = 4

/**
 * A dropdown panel rendered into `document.body` and positioned from its trigger,
 * so no row opacity, stacking context or scroll container can hide or clip it.
 * Flips above the trigger when there is no room below; closes on outside click,
 * Escape, scroll and resize.
 */
export function Menu({
  open,
  onClose,
  anchorRef,
  align = 'right',
  role = 'menu',
  label,
  className = '',
  children,
}: Props) {
  const [pos, setPos] = useState<{ top?: number; bottom?: number; left?: number; right?: number }>()

  useLayoutEffect(() => {
    const el = anchorRef.current
    if (!open || !el) return
    const r = el.getBoundingClientRect()
    const below = window.innerHeight - r.bottom
    const place: typeof pos =
      align === 'right' ? { right: window.innerWidth - r.right } : { left: r.left }
    // Panels are at most ~280px tall; flip when the space below is smaller and above is larger.
    if (below < 280 && r.top > below) place.bottom = window.innerHeight - r.top + GAP
    else place.top = r.bottom + GAP
    setPos(place)
  }, [open, anchorRef, align])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('resize', onClose)
    window.addEventListener('scroll', onClose, true)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', onClose)
      window.removeEventListener('scroll', onClose, true)
    }
  }, [open, onClose])

  if (!open || !pos) return null
  return createPortal(
    <>
      <button
        type="button"
        aria-label="Close menu"
        onClick={onClose}
        className="fixed inset-0 z-190 cursor-default"
      />
      <div
        role={role}
        aria-label={label}
        style={pos}
        className={`fixed z-200 rounded-xl border border-line bg-card shadow-[0_16px_40px_rgba(0,0,0,.5)] ${className}`}
      >
        {children}
      </div>
    </>,
    document.body,
  )
}
