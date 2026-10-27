import { useEffect, useRef, useState } from 'react'
import { Outlet } from 'react-router'

import { useShell, useWorkerQueue } from '../api/shell'
import { setDisplayTimeZone, useDisplayTimeZone } from '../time'
import { Button } from '../ui/Button'
import { useToast } from '../ui/toast'
import { setThemePref, useThemeControl } from '../theme'
import { CommandPalette } from './CommandPalette'
import { NotificationsPanel } from './NotificationsPanel'
import { ProcessingQueue } from './ProcessingQueue'
import { ProfileMenu } from './ProfileMenu'
import { overlayOpen } from './keys'
import { RailButton } from './RailButton'

/** The 56px icon rail plus the global overlays, wrapping every signed-in page. */
export function Shell() {
  const shellQuery = useShell()
  const shell = shellQuery.data
  // Pages read dates through the time module, so they only mount once it holds the
  // workspace zone; a changed zone remounts them so every date is re-read.
  const zone = shell?.timezone
  useEffect(() => {
    if (zone) setDisplayTimeZone(zone)
  }, [zone])
  const appliedZone = useDisplayTimeZone()
  // The saved choice follows the user to this browser; a change made here is written
  // to the shell cache too, so this only runs when the server's copy really moved.
  const savedTheme = shell?.theme
  useEffect(() => {
    if (savedTheme) setThemePref(savedTheme)
  }, [savedTheme])
  const [palette, setPalette] = useState(false)
  const { theme, toggle: toggleTheme } = useThemeControl()
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
      if (event.defaultPrevented || (overlayOpen() && !palette)) return
      const meta = event.metaKey || event.ctrlKey
      if (meta && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPalette((v) => !v)
      } else if (meta && event.key.toLowerCase() === 'd') {
        event.preventDefault()
        toggleTheme()
      }
    }
    window.addEventListener('keydown', onKey)
    const openPalette = () => setPalette(true)
    window.addEventListener('open-command-palette', openPalette)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('open-command-palette', openPalette)
    }
  }, [palette, toggleTheme])

  return (
    <div className="flex h-dvh overflow-hidden">
      <nav
        aria-label="Primary"
        className="flex w-14 shrink-0 flex-col items-center gap-1 border-r border-line bg-rail py-3"
      >
        <a
          href="/"
          aria-label="The Sanctuary"
          className="mb-2 flex h-10 w-10 shrink-0 items-center justify-center"
        >
          <span className="h-7 w-6 bg-accent [clip-path:polygon(0_0,100%_0,100%_70%,50%_100%,0_70%)]" />
        </a>
        <RailButton icon="home" label="Home" to="/" />
        <RailButton
          icon="inbox"
          label="Triage"
          to="/triage"
          count={shell?.triage_count ?? 0}
          tone="danger"
        />
        <RailButton icon="folder_open" label="Cases" to="/cases" />
        <div className="flex-1" />
        <RailButton
          icon={theme === 'dark' ? 'light_mode' : 'dark_mode'}
          label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          onClick={() => toggleTheme()}
        />
        <RailButton icon="search" label="Search (⌘K)" onClick={() => setPalette(true)} />
        <NotificationsPanel />
        <ProcessingQueue />
        <ProfileMenu />
      </nav>
      <main className="min-w-0 flex-1 overflow-y-auto">
        {appliedZone ? (
          <Outlet key={appliedZone} />
        ) : shellQuery.isError ? (
          <div role="alert" className="m-5 flex items-center gap-3 text-[12px] text-danger">
            <span>Could not load the workspace settings: {shellQuery.error.message}</span>
            <Button onClick={() => void shellQuery.refetch()}>Retry</Button>
          </div>
        ) : (
          <div aria-busy="true" aria-label="Loading" className="space-y-3 p-5">
            <div className="h-5 w-48 animate-pulse rounded bg-panel2" />
            <div className="h-24 animate-pulse rounded-xl bg-panel2" />
            <div className="h-24 animate-pulse rounded-xl bg-panel2" />
          </div>
        )}
      </main>
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
    </div>
  )
}
