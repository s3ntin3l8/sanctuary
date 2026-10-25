import { useCallback, useSyncExternalStore } from 'react'

import { useSaveTheme } from './api/settings'
import { useToast } from './ui/toast'

/** What the user picked; `system` follows the OS and is the default. */
export type ThemePref = 'system' | 'dark' | 'light'
/** What is on screen. */
export type Theme = 'dark' | 'light'

// Same key as the inline script in index.html, which applies it before first paint.
const KEY = 'site-theme'
const LIGHT_QUERY = '(prefers-color-scheme: light)'

const listeners = new Set<() => void>()
let watching: MediaQueryList | null = null

export function getThemePref(): ThemePref {
  const saved = localStorage.getItem(KEY)
  return saved === 'light' || saved === 'dark' ? saved : 'system'
}

function resolve(pref: ThemePref): Theme {
  if (pref !== 'system') return pref
  return window.matchMedia(LIGHT_QUERY).matches ? 'light' : 'dark'
}

export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
}

function apply(pref: ThemePref = getThemePref()) {
  document.documentElement.dataset.theme = resolve(pref)
  listeners.forEach((notify) => notify())
}

// Followed only while the choice is `system`; an explicit choice ignores the OS.
function onSystemChange() {
  if (getThemePref() === 'system') apply()
}

function watchSystem() {
  if (watching) return
  watching = window.matchMedia(LIGHT_QUERY)
  watching.addEventListener('change', onSystemChange)
}

/** Apply and remember a choice on this device. The server copy is written by `useThemeControl`. */
export function setThemePref(pref: ThemePref) {
  try {
    if (pref === 'system') localStorage.removeItem(KEY)
    else localStorage.setItem(KEY, pref)
  } catch {
    // Storage blocked or full: the theme still applies for this page view.
  }
  apply(pref)
}

function subscribe(notify: () => void) {
  watchSystem()
  listeners.add(notify)
  return () => {
    listeners.delete(notify)
  }
}

const snapshot = () => `${getThemePref()}:${currentTheme()}`

/** The choice and the theme it resolves to; re-renders when either changes. */
export function useTheme(): { pref: ThemePref; theme: Theme } {
  const [pref, theme] = useSyncExternalStore(subscribe, snapshot).split(':')
  return { pref: pref as ThemePref, theme: theme as Theme }
}

/** Rail button, ⌘D, palette and Settings all change the theme through here, so the
 * choice reaches the server (and the user's other browsers) and every view agrees. */
export function useThemeControl() {
  const { pref, theme } = useTheme()
  const save = useSaveTheme().mutate
  const toast = useToast()
  const choose = useCallback(
    (next: ThemePref) => {
      setThemePref(next)
      save({ theme: next }, { onError: (err) => toast(err.message, 'error') })
    },
    [save, toast],
  )
  // One press flips what is on screen, whatever the choice was.
  const toggle = useCallback(() => choose(currentTheme() === 'dark' ? 'light' : 'dark'), [choose])
  return { pref, theme, choose, toggle }
}
