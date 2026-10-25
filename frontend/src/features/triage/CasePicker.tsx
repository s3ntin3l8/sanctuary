import { type FormEvent, useState } from 'react'

import type { Schemas } from '../../api/client'
import { Icon } from '../../ui/Icon'
import { normalizeCaseId } from '../cases/caseId'

export type NewCase = { id: string; title: string }

type Props = {
  cases: Schemas['PickerCase'][]
  /** The AI-suggested case, pinned above the filter. */
  suggestedId?: string | null
  activeId?: string | null
  /** Pre-fills the title when creating (the AI's guess for this bundle). */
  defaultTitle?: string
  onPick: (caseId: string) => void
  onCreate: (c: NewCase) => void
}

/** Pick a case, or type an unknown ID and create it right here: one panel, no nested dialog. */
export function CasePicker({
  cases,
  suggestedId = null,
  activeId = null,
  defaultTitle = '',
  onPick,
  onCreate,
}: Props) {
  const [filter, setFilter] = useState('')
  const [creating, setCreating] = useState<NewCase | null>(null)
  const q = filter.trim()
  const lower = q.toLowerCase()
  const suggested = cases.find((c) => c.id === suggestedId)
  const others = cases.filter(
    (c) => c.id !== suggestedId && (!lower || `${c.id} ${c.title}`.toLowerCase().includes(lower)),
  )
  const exact = cases.some((c) => c.id.toLowerCase() === lower)

  if (creating) {
    return (
      <form
        className="space-y-2 p-3"
        onSubmit={(e: FormEvent) => {
          e.preventDefault()
          if (creating.id.trim()) onCreate({ id: creating.id.trim(), title: creating.title.trim() })
        }}
      >
        <input
          autoFocus
          aria-label="New case ID"
          value={creating.id}
          onChange={(e) => setCreating({ ...creating, id: normalizeCaseId(e.target.value) })}
          placeholder="ADV-025-A"
          className="w-full rounded-md border border-line bg-transparent px-2 py-1.5 font-mono text-[12px] outline-none"
        />
        <input
          aria-label="New case title"
          value={creating.title}
          onChange={(e) => setCreating({ ...creating, title: e.target.value })}
          placeholder="Weber ./. Weber"
          className="w-full rounded-md border border-line bg-transparent px-2 py-1.5 text-[11px] outline-none"
        />
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setCreating(null)}
            className="text-[10px] text-tealink hover:underline"
          >
            back
          </button>
          <button
            type="submit"
            className="ml-auto rounded-md bg-accent px-3 py-1 text-[11px] font-semibold text-on-accent"
          >
            Create & assign
          </button>
        </div>
      </form>
    )
  }

  return (
    <div>
      {suggested && (
        <Row
          active={activeId === suggested.id}
          title={suggested.id}
          sub={`${suggested.title} · AI-detected${suggested.is_draft ? ' · draft' : ''}`}
          onClick={() => onPick(suggested.id)}
        />
      )}
      <input
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter cases or type a new ID…"
        aria-label="Filter cases"
        className="w-full border-y border-line2 bg-transparent px-3 py-1.5 text-[11px] outline-none"
      />
      <div className="max-h-52 overflow-y-auto">
        {others.map((c) => (
          <Row
            key={c.id}
            active={activeId === c.id}
            title={c.id}
            sub={c.is_draft ? `${c.title} · draft` : c.title}
            onClick={() => onPick(c.id)}
          />
        ))}
      </div>
      {!exact && (
        <button
          type="button"
          onClick={() => setCreating({ id: normalizeCaseId(q), title: defaultTitle })}
          className="flex w-full items-center gap-2 border-t border-line2 px-3 py-2 text-left text-[11px] font-semibold text-tealink hover:bg-accent/7"
        >
          <Icon name="add" size={14} /> {q ? `Create case “${q}”` : 'Create new case…'}
        </button>
      )}
    </div>
  )
}

function Row({
  active,
  title,
  sub,
  onClick,
}: {
  active: boolean
  title: string
  sub?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="menuitem"
      aria-current={active || undefined}
      onClick={onClick}
      className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent/7"
    >
      {active ? (
        <Icon name="check" size={14} className="text-accent" />
      ) : (
        <span className="w-[14px] shrink-0" />
      )}
      <span className="min-w-0">
        <span className={`block truncate font-mono text-[11.5px] ${active ? '' : 'text-ink2'}`}>
          {title}
        </span>
        {sub && <span className="block truncate text-[9px] text-muted">{sub}</span>}
      </span>
    </button>
  )
}
