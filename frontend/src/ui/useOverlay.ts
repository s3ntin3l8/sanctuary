import { type RefObject, useLayoutEffect, useEffectEvent } from 'react'

const overlays: { root: HTMLElement; opener: HTMLElement | null }[] = []
const focusable =
  'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'

/** Only the top overlay owns Escape; dialogs also contain and restore focus. */
export function useOverlay(
  open: boolean,
  panel: RefObject<HTMLElement | null>,
  onClose: () => void,
  modal = true,
) {
  const close = useEffectEvent(onClose)
  useLayoutEffect(() => {
    const root = panel.current
    if (!open || !root) return
    const opener = document.activeElement as HTMLElement | null
    const entry = { root, opener }
    // A responsive parent can become modal while a descendant dialog is already open.
    const childIndex = overlays.findIndex(
      (other) =>
        root.contains(other.root) || (other.opener !== null && root.contains(other.opener)),
    )
    overlays.splice(childIndex < 0 ? overlays.length : childIndex, 0, entry)
    const topmost = () => overlays.at(-1) === entry
    const controls = () =>
      Array.from(root.querySelectorAll<HTMLElement>(focusable)).filter(
        (el) =>
          !el.hidden &&
          !el.closest('[hidden], [inert]') &&
          el.getAttribute('aria-hidden') !== 'true' &&
          (el.checkVisibility?.({ visibilityProperty: true }) ?? true),
      )
    const first = () => {
      const items = controls()
      return (
        items.find((el) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) ?? items[0] ?? root
      )
    }
    if (topmost()) first().focus()
    let focusedInside = root.contains(document.activeElement)
    const onKey = (event: KeyboardEvent) => {
      if (!topmost()) return
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        close()
      } else if (modal && event.key === 'Tab') {
        const items = controls()
        const index = items.indexOf(document.activeElement as HTMLElement)
        const next = event.shiftKey ? index - 1 : index + 1
        if (index < 0 || next < 0 || next >= items.length) {
          event.preventDefault()
          ;(event.shiftKey ? (items.at(-1) ?? root) : (items[0] ?? root)).focus()
        }
      }
    }
    const onFocus = (event: FocusEvent) => {
      if (topmost()) focusedInside = root.contains(event.target as Node)
      if (modal && topmost() && !root.contains(event.target as Node)) first().focus()
    }
    window.addEventListener('keydown', onKey, true)
    document.addEventListener('focusin', onFocus)
    return () => {
      const wasTop = topmost()
      overlays.splice(overlays.indexOf(entry), 1)
      window.removeEventListener('keydown', onKey, true)
      document.removeEventListener('focusin', onFocus)
      if (wasTop && opener?.isConnected && (modal || focusedInside)) {
        opener.focus()
      }
    }
  }, [open, panel, modal])
}
