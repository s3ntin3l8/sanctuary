import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import { AppearancePage } from './features/settings/AppearancePage'
import { Shell } from './shell/Shell'
import { emptyQueue, notificationsView, shellView } from './test/fixtures'
import { renderAt, stubApi } from './test/render'
import { currentTheme, getThemePref, setThemePref, useTheme } from './theme'

// An OS whose light/dark setting the test can flip.
let osLight = false
const osListeners = new Set<() => void>()

function setOs(light: boolean) {
  osLight = light
  act(() => osListeners.forEach((fn) => fn()))
}

beforeEach(() => {
  osLight = false
  osListeners.clear()
  localStorage.clear()
  delete document.documentElement.dataset.theme
  vi.stubGlobal('matchMedia', (query: string) => ({
    get matches() {
      return query.includes('light') && osLight
    },
    media: query,
    addEventListener: (_: string, fn: () => void) => osListeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => osListeners.delete(fn),
  }))
})

afterEach(() => localStorage.clear())

function Probe() {
  const { pref, theme } = useTheme()
  return <p>{`${pref}/${theme}`}</p>
}

test('no saved choice means system, which follows the OS live', () => {
  expect(getThemePref()).toBe('system')
  renderAt('/', <Probe />)
  act(() => setThemePref('system'))
  expect(screen.getByText('system/dark')).toBeVisible()
  setOs(true)
  expect(screen.getByText('system/light')).toBeVisible()
  expect(currentTheme()).toBe('light')
})

test('an explicit choice ignores the OS; system forgets it again', () => {
  renderAt('/', <Probe />)
  act(() => setThemePref('dark'))
  setOs(true)
  expect(screen.getByText('dark/dark')).toBeVisible()
  expect(localStorage.getItem('site-theme')).toBe('dark')
  act(() => setThemePref('system'))
  expect(localStorage.getItem('site-theme')).toBeNull()
  expect(screen.getByText('system/light')).toBeVisible()
})

test('Settings → Appearance offers System, Dark and Light and saves the pick', async () => {
  const fetch = stubApi({
    'GET /api/v1/shell': { body: shellView },
    'GET /api/v1/settings/appearance': {
      body: {
        theme: 'system',
        dashboard_cards: { action_items: true, costs: true, documents: true },
        timezone: 'UTC',
        timezone_choices: ['UTC'],
      },
    },
    'PUT /api/v1/settings/appearance/theme': {
      body: {
        theme: 'light',
        dashboard_cards: { action_items: true, costs: true, documents: true },
        timezone: 'UTC',
        timezone_choices: ['UTC'],
      },
    },
  })
  renderAt('/settings/appearance', <AppearancePage />)
  const user = userEvent.setup()
  expect(await screen.findByRole('button', { name: /System/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  expect(screen.getByText(/Following your system: currently dark/)).toBeVisible()
  await user.click(screen.getByRole('button', { name: /Light/ }))
  expect(screen.getByRole('button', { name: /Light/ })).toHaveAttribute('aria-pressed', 'true')
  expect(localStorage.getItem('site-theme')).toBe('light')
  const sent = () => fetch.mock.calls.map(([r]) => r).find((r) => r.method === 'PUT')
  await waitFor(() => expect(sent()).toBeDefined())
  expect(await sent()?.clone().json()).toEqual({ theme: 'light' })
})

function stubShell(theme: 'system' | 'dark' | 'light') {
  return stubApi({
    'GET /api/v1/shell': { body: { ...shellView, theme } },
    'GET /api/v1/notifications': { body: notificationsView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/search': {
      body: { documents: [], cases: [], contacts: [], total: 0 },
    },
    'PUT /api/v1/settings/appearance/theme': {
      body: {
        theme: 'light',
        dashboard_cards: { action_items: true, costs: true, documents: true },
        timezone: 'UTC',
        timezone_choices: ['UTC'],
      },
    },
  })
}

test('the shell applies the theme saved on the server', async () => {
  stubShell('light')
  renderAt('/', <Shell />)
  expect(await screen.findByRole('button', { name: 'Switch to dark theme' })).toBeVisible()
  expect(localStorage.getItem('site-theme')).toBe('light')
})

test('toggling from the palette updates the rail button', async () => {
  stubShell('dark')
  renderAt('/', <Shell />)
  const user = userEvent.setup()
  expect(await screen.findByRole('button', { name: 'Switch to light theme' })).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Search (⌘K)' }))
  await user.click(await screen.findByRole('button', { name: 'Toggle theme' }))
  expect(await screen.findByRole('button', { name: 'Switch to dark theme' })).toBeVisible()
})
