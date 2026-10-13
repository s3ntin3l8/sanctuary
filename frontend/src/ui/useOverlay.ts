import { type RefObject, useLayoutEffect, useEffectEvent } from 'react'

type OverlayEntry = { root: HTMLElement; opener: HTMLElement | null; detach: () => void }
// Ordering matters: responsive parents must be inserted below their open descendants.
const overlays: OverlayEntry[] = []
function removeOverlay(entry: OverlayEntry) {
  entry.detach()
  const index = overlays.indexOf(entry)
  if (index >= 0) overlays.splice(index, 1)
}
if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    for (const entry of [...overlays]) removeOverlay(entry)
  })
}
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
    for (const other of [...overlays]) {
      if (!other.root.isConnected || other.root === root) removeOverlay(other)
    }
    const entry: OverlayEntry = { root, opener, detach: () => {} }
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
      if (!root.isConnected || !topmost()) return
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
      if (!root.isConnected) return
      if (topmost()) focusedInside = root.contains(event.target as Node)
      if (modal && topmost() && !root.contains(event.target as Node)) first().focus()
    }
    window.addEventListener('keydown', onKey, true)
    document.addEventListener('focusin', onFocus)
    entry.detach = () => {
      window.removeEventListener('keydown', onKey, true)
      document.removeEventListener('focusin', onFocus)
    }
    return () => {
      const wasTop = topmost()
      removeOverlay(entry)
      if (wasTop && opener?.isConnected && (modal || focusedInside)) {
        opener.focus()
      }
    }
  }, [open, panel, modal])
}
