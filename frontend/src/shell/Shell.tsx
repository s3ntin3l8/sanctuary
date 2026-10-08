import { useEffect, useRef, useState } from 'react'
import { Outlet } from 'react-router'

import { useShell, useWorkerQueue } from '../api/shell'
import { useToast } from '../ui/toast'
import { currentTheme, toggleTheme } from '../theme'
import { CommandPalette } from './CommandPalette'
import { NotificationsPanel } from './NotificationsPanel'
import { ProcessingQueue } from './ProcessingQueue'
import { ProfileMenu } from './ProfileMenu'
import { CountBadge, RailButton } from './RailButton'

/** The 56px icon rail plus the global overlays, wrapping every signed-in page. */
export function Shell() {
  const shell = useShell().data
  const [palette, setPalette] = useState(false)
  const [theme, setThemeState] = useState(currentTheme)
  const toast = useToast()
  const run = useWorkerQueue().data?.gmail_import
  // Started-at of the run last seen live, so its end is announced exactly once —
  // and a run that was already over when the page loaded never is.
  const liveRun = useRef<string | null>(null)
  useEffect(() => {
    if (!run) return
    if (run.active) {
      liveRun.current = run.started_at ?? null
    } else if (liveRun.current !== null && liveRun.current === (run.started_at ?? null)) {
      liveRun.current = null
      toast(
        run.cancelled
          ? `Gmail import stopped after ${run.done} of ${run.total} — see Triage`
          : `Gmail import finished: ${run.done} imported${run.failed_count > 0 ? ` · ${run.failed_count} failed` : ''} — see Triage`,
        run.failed_count > 0 ? 'error' : 'success',
      )
    }
  }, [run, toast])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const meta = event.metaKey || event.ctrlKey
      if (meta && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPalette((v) => !v)
      } else if (meta && event.key.toLowerCase() === 'd') {
        event.preventDefault()
        setThemeState(toggleTheme())
      }
    }
    window.addEventListener('keydown', onKey)
    const openPalette = () => setPalette(true)
    window.addEventListener('open-command-palette', openPalette)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('open-command-palette', openPalette)
    }
  }, [])

  return (
    <div className="flex h-screen overflow-hidden">
      <nav
        aria-label="Primary"
        className="flex w-14 shrink-0 flex-col items-center gap-1 border-r border-line bg-rail py-3"
      >
        <a
          href="/"
          aria-label="The Sanctuary"
          className="mb-2 flex h-10 w-10 items-center justify-center"
        >
          <span className="h-7 w-6 bg-accent [clip-path:polygon(0_0,100%_0,100%_70%,50%_100%,0_70%)]" />
        </a>
        <RailButton icon="home" label="Home" to="/" />
        <RailButton
          icon="inbox"
          label="Triage"
          to="/triage"
          badge={<CountBadge count={shell?.triage_count ?? 0} tone="danger" />}
        />
        <RailButton icon="folder_open" label="Cases" to="/cases" />
        <div className="flex-1" />
        <RailButton
          icon={theme === 'dark' ? 'light_mode' : 'dark_mode'}
          label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          onClick={() => setThemeState(toggleTheme())}
        />
        <RailButton icon="search" label="Search (⌘K)" onClick={() => setPalette(true)} />
        <NotificationsPanel />
        <ProcessingQueue />
        <ProfileMenu />
      </nav>
      <main className="min-w-0 flex-1 overflow-y-auto">
        <Outlet />
      </main>
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
    </div>
  )
}
