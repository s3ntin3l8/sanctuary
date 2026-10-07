import { useEffect } from 'react'

/** True when the key event comes from a field that owns typing. */
export function isTypingTarget(event: KeyboardEvent) {
  const el = event.target as HTMLElement | null
  if (!el) return false
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.isContentEditable
}

/** True when ⌘ / Ctrl / Alt is held, i.e. the key is a chord, not a plain letter. */
export function hasModifier(event: KeyboardEvent) {
  return event.metaKey || event.ctrlKey || event.altKey
}

export const ROW_ATTR = 'data-nav-row'

/** True while a modal, popover or menu owns the keyboard. */
export function overlayOpen() {
  return document.querySelector('[role="dialog"], [role="menu"]') !== null
}

/**
 * `j` / `k` move focus between the elements marked `data-nav-row`, in DOM
 * order, wrapping at the ends. The rows are links or buttons, so Enter is
 * the browser's own activation and `:focus-visible` is the highlight.
 */
export function useRovingRows() {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'j' && event.key !== 'k') return
      if (isTypingTarget(event) || hasModifier(event) || event.defaultPrevented) return
      if (overlayOpen()) return
      const rows = Array.from(document.querySelectorAll<HTMLElement>(`[${ROW_ATTR}]`))
      if (rows.length === 0) return
      const current = rows.indexOf(document.activeElement as HTMLElement)
      const next =
        event.key === 'j'
          ? (current + 1) % rows.length
          : current <= 0
            ? rows.length - 1
            : current - 1
      event.preventDefault()
      rows[next]?.focus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
}
