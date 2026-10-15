import { useRef, useState } from 'react'

import { Button, buttonClass } from '../../ui/Button'
import { Chip } from '../../ui/Chip'
import { Icon } from '../../ui/Icon'
import { Menu } from '../../ui/Menu'
import type { Filter, Sort } from './historyModel'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'case', label: 'In a case' },
  { key: 'nocase', label: 'No case' },
  { key: 'pending', label: 'Pending' },
  { key: 'done', label: 'Done' },
]

const SORTS: { key: Sort; label: string }[] = [
  { key: 'oldest', label: 'Oldest history first' },
  { key: 'recent', label: 'Recent activity' },
  { key: 'pending', label: 'Most pending' },
]

/** Search, filter chips, sort, and the global "import the oldest N" action. */
export function HistoryToolbar({
  query,
  onQuery,
  filter,
  onFilter,
  counts,
  sort,
  onSort,
  oldestInput,
  onOldestInput,
  onOldestBlur,
  busy,
  onImport,
}: {
  query: string
  onQuery: (q: string) => void
  filter: Filter
  onFilter: (f: Filter) => void
  counts: Record<Filter, number>
  sort: Sort
  onSort: (s: Sort) => void
  oldestInput: string
  onOldestInput: (v: string) => void
  onOldestBlur: () => void
  busy: boolean
  onImport: () => void
}) {
  const sortRef = useRef<HTMLButtonElement>(null)
  const [sortOpen, setSortOpen] = useState(false)
  return (
    <section aria-label="Import controls" className="mb-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <label className="relative">
          <span className="sr-only">Search references</span>
          <Icon
            name="search"
            size={14}
            className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-muted"
          />
          <input
            type="search"
            value={query}
            placeholder="Search references, topics, cases…"
            onChange={(e) => onQuery(e.target.value)}
            className="w-64 rounded-[9px] border border-line bg-panel2 py-1 pr-2 pl-7 text-[12px]"
          />
        </label>
        <div className="flex flex-wrap gap-1.5">
          {FILTERS.map((f) => (
            <Chip
              key={f.key}
              active={filter === f.key}
              count={counts[f.key]}
              onClick={() => onFilter(f.key)}
            >
              {f.label}
            </Chip>
          ))}
        </div>
        <div className="ml-auto">
          <button
            ref={sortRef}
            type="button"
            className={buttonClass('secondary', '', 'sm')}
            aria-haspopup="menu"
            aria-expanded={sortOpen}
            onClick={() => setSortOpen((v) => !v)}
          >
            <Icon name="sort" size={14} /> {SORTS.find((s) => s.key === sort)?.label}
          </button>
          <Menu open={sortOpen} onClose={() => setSortOpen(false)} anchorRef={sortRef} label="Sort">
            {SORTS.map((s) => (
              <button
                key={s.key}
                type="button"
                role="menuitemradio"
                aria-checked={sort === s.key}
                className="block w-full px-3 py-1.5 text-left text-[12px] hover:bg-panel2"
                onClick={() => {
                  onSort(s.key)
                  setSortOpen(false)
                }}
              >
                {s.label}
              </button>
            ))}
          </Menu>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-card px-4 py-2">
        <span>Import next</span>
        <input
          type="number"
          aria-label="How many"
          min={1}
          max={100}
          value={oldestInput}
          onChange={(e) => onOldestInput(e.target.value)}
          onBlur={onOldestBlur}
          className="w-16 rounded-[9px] border border-line bg-panel2 px-2 py-1 text-[12px]"
        />
        <span>
          oldest from <strong>all references</strong>
        </span>
        <Button className="ml-auto" disabled={busy} onClick={onImport}>
          Import
        </Button>
      </div>
    </section>
  )
}
