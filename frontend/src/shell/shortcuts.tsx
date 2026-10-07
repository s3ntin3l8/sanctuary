import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react'

import { Modal } from '../ui/Modal'
import { hasModifier, isTypingTarget } from './keys'

export type ShortcutRows = readonly (readonly [key: string, action: string])[]
type Section = { title: string; rows: ShortcutRows }

const GLOBAL: Section = {
  title: 'Everywhere',
  rows: [
    ['⌘K / Ctrl+K', 'Command palette'],
    ['⌘D / Ctrl+D', 'Toggle light / dark theme'],
    ['?', 'This overview'],
  ],
}

type Ctx = {
  open: boolean
  show: () => void
  hide: () => void
  register: (section: Section) => () => void
}

const ShortcutsContext = createContext<Ctx>({
  open: false,
  show: () => {},
  hide: () => {},
  register: () => () => {},
})

/** `const { show, open } = useShortcuts()` — the global ? cheat sheet. */
export function useShortcuts() {
  return useContext(ShortcutsContext)
}

/**
 * Register the current page's shortcuts for the cheat sheet while mounted.
 * Pass a module-level constant for `rows` so the registration is stable.
 */
export function usePageShortcuts(title: string, rows: ShortcutRows) {
  const { register } = useShortcuts()
  useEffect(() => register({ title, rows }), [register, title, rows])
}

/** Owns the cheat sheet and its `?` binding; pages add their own sections. */
export function ShortcutsProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [sections, setSections] = useState<Section[]>([])
  const show = useCallback(() => setOpen(true), [])
  const hide = useCallback(() => setOpen(false), [])
  const register = useCallback((section: Section) => {
    setSections((s) => [...s, section])
    return () => setSections((s) => s.filter((x) => x !== section))
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== '?' || isTypingTarget(event) || hasModifier(event)) return
      event.preventDefault()
      setOpen((v) => !v)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const value = useMemo(() => ({ open, show, hide, register }), [open, show, hide, register])
  return (
    <ShortcutsContext.Provider value={value}>
      {children}
      <Modal open={open} onClose={hide} title="Keyboard shortcuts" icon="keyboard" width={520}>
        <div className="space-y-4">
          {[...sections, GLOBAL].map((s) => (
            <section key={s.title} aria-label={s.title}>
              <h3 className="mb-1.5 text-[10px] font-bold tracking-[.11em] text-muted uppercase">
                {s.title}
              </h3>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[12px]">
                {s.rows.map(([k, v]) => (
                  <KeyRow key={k} k={k} v={v} />
                ))}
              </dl>
            </section>
          ))}
        </div>
      </Modal>
    </ShortcutsContext.Provider>
  )
}

function KeyRow({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt>
        <kbd className="rounded border border-line bg-card2 px-1.5 py-0.5 font-mono text-[10.5px]">
          {k}
        </kbd>
      </dt>
      <dd className="text-ink2">{v}</dd>
    </>
  )
}
