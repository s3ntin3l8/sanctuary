import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'

import { useSearch } from '../api/shell'
import { Button } from '../ui/Button'
import { useOverlay } from '../ui/useOverlay'
import { leaveTo } from '../navigation'
import { toggleTheme } from '../theme'
import { Icon } from '../ui/Icon'

type Item = { key: string; icon: string; title: string; sub?: string; run: () => void }
type Group = { label: string; items: Item[] }

type Props = { open: boolean; onClose: () => void }

/** Mounted only while open, so every opening starts with a fresh query. */
export function CommandPalette({ open, onClose }: Props) {
  if (!open) return null
  return <PaletteDialog onClose={onClose} />
}

function PaletteDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [cursor, setCursor] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLDivElement>(null)
  useOverlay(true, panel, onClose)
  const results = useSearch(debounced)
  const term = query.trim()
  const waiting = term !== debounced

  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 250)
    return () => clearTimeout(t)
  }, [query])

  const go = useCallback(
    (path: string, spa = false) => {
      onClose()
      if (spa) navigate(path)
      else leaveTo(path)
    },
    [onClose, navigate],
  )

  const groups = useMemo<Group[]>(() => {
    if (term.length < 2) {
      return [
        {
          label: 'Navigate',
          items: [
            { key: 'home', icon: 'home', title: 'Home', run: () => go('/', true) },
            { key: 'triage', icon: 'inbox', title: 'Triage', run: () => go('/triage', true) },
            { key: 'cases', icon: 'folder_open', title: 'Cases', run: () => go('/cases', true) },
            { key: 'costs', icon: 'payments', title: 'Costs', run: () => go('/costs', true) },
            {
              key: 'settings',
              icon: 'settings',
              title: 'Settings',
              run: () => go('/settings/account', true),
            },
          ],
        },
        {
          label: 'Actions',
          items: [
            {
              key: 'upload',
              icon: 'upload_file',
              title: 'Upload documents',
              run: () => go('/triage?upload=1', true),
            },
            {
              key: 'theme',
              icon: 'contrast',
              title: 'Toggle theme',
              run: () => {
                toggleTheme()
                onClose()
              },
            },
          ],
        },
      ]
    }
    const data = waiting || results.isPlaceholderData || results.error ? undefined : results.data
    if (!data) return []
    return [
      {
        label: 'Documents',
        items: data.documents.map((d) => ({
          key: `doc-${d.id}`,
          icon: 'description',
          title: d.title,
          sub: d.case_id ?? 'Triage',
          run: () => go(`/document/${d.id}`, true),
        })),
      },
      {
        label: 'Cases',
        items: data.cases.map((c) => ({
          key: `case-${c.id}`,
          icon: 'folder',
          title: c.title,
          sub: `${c.id} · ${c.status}`,
          run: () => go(`/cases/${c.id}`, true),
        })),
      },
      {
        label: 'Contacts',
        items: data.contacts.map((c) => ({
          key: `contact-${c.name}`,
          icon: 'person',
          title: c.name,
          run: () => go(`/contacts?name=${encodeURIComponent(c.name)}`, true),
        })),
      },
    ].filter((g) => g.items.length > 0)
  }, [term, waiting, results.data, results.isPlaceholderData, results.error, go, onClose])

  const flat = groups.flatMap((g) => g.items)
  const activeIndex = Math.max(0, Math.min(cursor, flat.length - 1))
  useEffect(() => {
    list.current
      ?.querySelector('[data-palette-active="true"]')
      ?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex, flat.length])

  return (
    <div
      className="fixed inset-0 z-150 flex items-start justify-center bg-[rgba(6,14,32,.6)] p-4 pt-[min(12vh,4rem)] backdrop-blur-[3px]"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-label="Command palette"
        className="flex max-h-[calc(100dvh-5rem)] w-full max-w-[620px] flex-col overflow-hidden rounded-2xl border border-line bg-card shadow-[0_30px_80px_rgba(0,0,0,.6)]"
      >
        <div className="flex items-center gap-2 border-b border-line2 px-4 py-3">
          <Icon name="search" size={18} className="text-muted" />
          <input
            ref={input}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setCursor(0)
            }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault()
                setCursor((c) => Math.max(0, Math.min(c + 1, flat.length - 1)))
              } else if (e.key === 'ArrowUp') {
                e.preventDefault()
                setCursor((c) => Math.max(c - 1, 0))
              } else if (e.key === 'Enter') {
                e.preventDefault()
                const item = flat[activeIndex]
                if (item) item.run()
                else if (term.length >= 2) go(`/search?q=${encodeURIComponent(term)}`, true)
              }
            }}
            placeholder="Search cases, documents, contacts…"
            aria-label="Search"
            className="min-w-0 flex-1 bg-transparent text-[14px] text-ink outline-none placeholder:text-muted2"
          />
          <kbd className="rounded border border-line px-1.5 font-mono text-[10px] text-muted">
            ESC
          </kbd>
        </div>
        <div ref={list} className="min-h-0 max-h-[50vh] overflow-y-auto p-2">
          {term.length >= 2 && !waiting && results.error ? (
            <div role="alert" className="space-y-3 px-3 py-6 text-center text-[12px]">
              <p className="text-danger">Search failed: {results.error.message}</p>
              <Button variant="secondary" onClick={() => void results.refetch()}>
                Retry search
              </Button>
            </div>
          ) : term.length >= 2 && (waiting || results.isFetching) ? (
            <p role="status" className="px-3 py-6 text-center text-[12px] text-muted">
              Searching…
            </p>
          ) : groups.length === 0 && term.length >= 2 ? (
            <div className="space-y-3 px-3 py-6 text-center text-[12px] text-muted">
              <p>No results. Try another name or keyword.</p>
              <Button
                variant="secondary"
                onClick={() => {
                  setQuery('')
                  setDebounced('')
                  setCursor(0)
                  input.current?.focus()
                }}
              >
                Reset search
              </Button>
            </div>
          ) : null}
          {groups.map((group) => (
            <div key={group.label} className="mb-1">
              <div className="px-3 pt-2 pb-1 text-[9px] font-extrabold tracking-[.14em] text-muted uppercase">
                {group.label}
              </div>
              {group.items.map((item) => {
                const index = flat.indexOf(item)
                return (
                  <button
                    key={item.key}
                    data-palette-active={index === activeIndex}
                    type="button"
                    onMouseEnter={() => setCursor(index)}
                    onClick={item.run}
                    className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left ${
                      index === activeIndex ? 'bg-accent/10' : 'hover:bg-accent/5'
                    }`}
                  >
                    <Icon name={item.icon} size={16} className="text-muted" />
                    <span className="min-w-0 flex-1 truncate text-[13px]">{item.title}</span>
                    {item.sub && (
                      <span className="truncate font-mono text-[10px] text-muted">{item.sub}</span>
                    )}
                  </button>
                )
              })}
            </div>
          ))}
        </div>
        <div className="flex shrink-0 items-center gap-1 border-t border-line2 px-4 py-2 text-[10px] text-muted">
          <Icon name="lock" size={12} /> Local index · ↑↓ navigate · ↵ open
          {term.length >= 2 && (
            <button
              type="button"
              className="ml-auto text-tealink hover:underline"
              onClick={() => go(`/search?q=${encodeURIComponent(term)}`, true)}
            >
              View full results
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
