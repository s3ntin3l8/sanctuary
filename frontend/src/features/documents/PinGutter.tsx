import { type RefObject, useLayoutEffect, useRef, useState } from 'react'

import { type Pin, useDeletePin, useUpdatePin } from '../../api/documents'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'

type Props = { docId: number; pins: Pin[]; article: RefObject<HTMLElement | null> }

/** Margin notes anchored beside the highlighted passage they belong to. */
export function PinGutter({ docId, pins, article }: Props) {
  const [tops, setTops] = useState<Record<number, number | null>>({})
  const cards = useRef<Record<number, HTMLElement | null>>({})

  // Each card sits at its mark's offset; overlapping cards are pushed down.
  useLayoutEffect(() => {
    const root = article.current
    if (!root || pins.length === 0) return
    const place = () => {
      const base = root.getBoundingClientRect().top
      const raw = pins.map((p) => {
        const mark = root.querySelector<HTMLElement>(`#p-${CSS.escape(p.passage_id)}`)
        const top = mark && mark.tagName === 'MARK' ? mark.getBoundingClientRect().top - base : null
        return { id: p.id, top }
      })
      raw.sort((a, b) => (a.top ?? -1) - (b.top ?? -1))
      let cursor = 0
      const next: Record<number, number | null> = {}
      for (const r of raw) {
        if (r.top === null) {
          next[r.id] = null
          continue
        }
        const top = Math.max(r.top, cursor)
        next[r.id] = top
        cursor = top + (cards.current[r.id]?.offsetHeight ?? 72) + 8
      }
      setTops(next)
    }
    place()
    const ro = new ResizeObserver(place)
    ro.observe(root)
    return () => ro.disconnect()
  }, [pins, article])

  if (pins.length === 0) return null
  return (
    <div className="absolute top-6 left-4 w-48" aria-label="Pins">
      {pins.map((p) => (
        <PinCard
          key={p.id}
          docId={docId}
          pin={p}
          top={tops[p.id]}
          ref={(el) => {
            cards.current[p.id] = el
          }}
        />
      ))}
    </div>
  )
}

function PinCard({
  docId,
  pin,
  top,
  ref,
}: {
  docId: number
  pin: Pin
  top: number | null | undefined
  ref: (el: HTMLElement | null) => void
}) {
  const update = useUpdatePin(docId)
  const remove = useDeletePin(docId)
  const toast = useToast()
  const [note, setNote] = useState(pin.note ?? '')
  const [seen, setSeen] = useState(pin.note)
  if (pin.note !== seen) {
    // The server's note changed (another save landed): adopt it.
    setSeen(pin.note)
    setNote(pin.note ?? '')
  }
  const unmatched = top === null
  return (
    <article
      ref={ref}
      data-pin-id={pin.id}
      className={`absolute left-0 w-48 rounded-lg border bg-card2 p-2 text-[11px] shadow-sm transition-[top] ${unmatched ? 'border-warning/50' : 'border-line'}`}
      style={{ top: top ?? 0 }}
    >
      <header className="mb-1 flex items-center gap-1 text-muted">
        <Icon name="push_pin" size={12} className="text-warning" />
        <span className="font-mono text-[9.5px]">{unmatched ? 'passage not located' : 'pin'}</span>
        <button
          type="button"
          aria-label="Delete pin"
          onClick={() => remove.mutate(pin.id, { onError: (e) => toast(e.message, 'error') })}
          className="ml-auto hover:text-danger"
        >
          <Icon name="close" size={12} />
        </button>
      </header>
      <textarea
        aria-label="Pin note"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        onBlur={() => {
          if ((note || null) !== (pin.note || null)) {
            update.mutate(
              { pinId: pin.id, note: note || null },
              { onError: (e) => toast(e.message, 'error') },
            )
          }
        }}
        placeholder="Note…"
        rows={2}
        className="w-full resize-none rounded border border-transparent bg-transparent px-1 py-0.5 outline-none focus:border-line"
      />
      {pin.updated_at && (
        <p className="font-mono text-[9px] text-muted2">
          {new Date(pin.updated_at).toLocaleString()}
        </p>
      )}
    </article>
  )
}
