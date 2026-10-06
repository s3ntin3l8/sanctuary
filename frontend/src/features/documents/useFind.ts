import { type RefObject, useCallback, useEffect, useMemo, useState } from 'react'

type HighlightRegistry = { set(name: string, h: unknown): void; delete(name: string): void }
type HighlightCtor = new (...ranges: Range[]) => unknown
const css = globalThis.CSS as unknown as { highlights?: HighlightRegistry } | undefined
const HighlightClass = (globalThis as unknown as { Highlight?: HighlightCtor }).Highlight

export type Find = {
  isOpen: boolean
  open: () => void
  close: () => void
  query: string
  setQuery: (q: string) => void
  index: number
  total: number
  next: (delta: 1 | -1) => void
}

function collect(root: HTMLElement | null, query: string): Range[] {
  const q = query.trim().toLowerCase()
  if (!root || q.length < 2) return []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const found: Range[] = []
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent?.toLowerCase() ?? ''
    let at = text.indexOf(q)
    while (at !== -1 && found.length < 500) {
      const r = document.createRange()
      r.setStart(node, at)
      r.setEnd(node, at + q.length)
      found.push(r)
      at = text.indexOf(q, at + q.length)
    }
  }
  return found
}

/**
 * In-document find over the rendered body. Matches are painted with the CSS
 * Custom Highlight API where available (no DOM mutation under React), and
 * the current match is scrolled into view.
 */
export function useFind(root: RefObject<HTMLElement | null>, bodyVersion: string | null): Find {
  const [isOpen, setOpen] = useState(false)
  const [query, setQueryState] = useState('')
  const [index, setIndex] = useState(0)
  // Ranges are derived from the query and the current body; `collect` reads the
  // live DOM, so this runs on commit of either input rather than in an effect.
  const ranges = useMemo(
    () => (isOpen ? collect(root.current, query) : []),
    // bodyVersion is not read, but a new body means new text nodes to search.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isOpen, query, bodyVersion, root],
  )

  const setQuery = useCallback((q: string) => {
    setQueryState(q)
    setIndex(0)
  }, [])

  useEffect(() => {
    if (!css?.highlights || !HighlightClass) return
    const clear = () => {
      css.highlights?.delete('find')
      css.highlights?.delete('find-current')
    }
    if (!isOpen || ranges.length === 0) {
      clear()
      return
    }
    css.highlights.set('find', new HighlightClass(...ranges))
    const current = ranges[index]
    if (current) {
      css.highlights.set('find-current', new HighlightClass(current))
      const rect = current.getBoundingClientRect()
      const parent = current.startContainer.parentElement
      if (parent && (rect.top < 80 || rect.bottom > window.innerHeight - 40)) {
        parent.scrollIntoView({ block: 'center' })
      }
    }
    return clear
  }, [ranges, index, isOpen])

  const open = useCallback(() => setOpen(true), [])
  const close = useCallback(() => {
    setOpen(false)
    setQueryState('')
  }, [])
  const next = useCallback(
    (delta: 1 | -1) => {
      if (ranges.length === 0) return
      setIndex((i) => (i + delta + ranges.length) % ranges.length)
    },
    [ranges.length],
  )

  return { isOpen, open, close, query, setQuery, index, total: ranges.length, next }
}
