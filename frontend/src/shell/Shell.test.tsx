import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import * as navigation from '../navigation'
import { emptyQueue, notificationsView, shellView } from '../test/fixtures'
import { renderAt, stubApi } from '../test/render'
import { displayTimeZone, formatShortDate, setDisplayTimeZone } from '../time'
import { Shell } from './Shell'

function renderShell() {
  setDisplayTimeZone(null)
  vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/shell': { body: shellView },
    'GET /api/v1/notifications': { body: notificationsView },
    'GET /api/v1/worker-queue': {
      body: { ...emptyQueue, counts: { executing: 1, queued: 2, failed: 0, ai_inflight: 0 } },
    },
    'POST /api/v1/auth/logout': { status: 204, body: null },
    'GET /api/v1/search': {
      body: {
        documents: [{ id: 2211, title: 'Klageerwiderung', case_id: 'ADV-024-A' }],
        cases: [],
        contacts: [],
        total: 1,
      },
    },
  })
  renderAt('/', <Shell />)
  return fetch
}

test('rail links: triage is a full page load, cases is client-side', async () => {
  renderShell()
  expect(await screen.findByRole('link', { name: 'Triage' })).toHaveAttribute('href', '/triage')
  expect(screen.getByRole('link', { name: 'Cases' })).toHaveAttribute('href', '/cases')
  expect(screen.queryByRole('link', { name: 'Costs' })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Contacts' })).not.toBeInTheDocument()
  expect(await screen.findByText('7')).toBeVisible() // triage badge
  expect(await screen.findByText('3')).toBeVisible() // processing badge: 1 executing + 2 queued
  expect(await screen.findByText('4')).toBeVisible() // notifications badge
})

test('profile menu signs out and leaves for /login', async () => {
  renderShell()
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Account menu' }))
  expect(screen.getByText('Katharina Vogt')).toBeVisible()
  expect(screen.getByRole('menuitem', { name: 'Manage users' })).toHaveAttribute(
    'href',
    '/admin/users',
  )
  await user.click(screen.getByRole('menuitem', { name: 'Sign out' }))
  await waitFor(() => expect(navigation.leaveTo).toHaveBeenCalledWith('/login'))
})

test('⌘K opens the palette and search results navigate', async () => {
  renderShell()
  const user = userEvent.setup()
  await user.keyboard('{Meta>}k{/Meta}')
  const input = await screen.findByLabelText('Search')
  await user.type(input, 'Klage')
  const hit = await screen.findByRole('button', { name: /Klageerwiderung/ })
  await user.click(hit)
  // Documents are an SPA route now: client-side navigation, no full page load.
  expect(navigation.leaveTo).not.toHaveBeenCalled()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

  await user.keyboard('{Meta>}k{/Meta}')
  expect(await screen.findByRole('dialog')).toBeVisible()
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

test('the workspace timezone is applied before the page renders its dates', async () => {
  stubApi({
    'GET /api/v1/shell': { body: { ...shellView, timezone: 'Pacific/Auckland' } },
    'GET /api/v1/notifications': { body: notificationsView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
  })
  setDisplayTimeZone(null)
  // 22:00 UTC is already the next day in Auckland.
  const Page = () => <p>{formatShortDate('2026-06-17T22:00:00')}</p>
  renderAt('/', <Page />, <Shell />)
  expect(await screen.findByText("18 Jun '26")).toBeVisible()
  expect(displayTimeZone()).toBe('Pacific/Auckland')
})

test('a failed shell request shows an error with retry instead of dates in a guessed zone', async () => {
  let fail = true
  const fetch = vi.fn(async (request: Request) => {
    const path = new URL(request.url).pathname
    if (path === '/api/v1/shell') {
      return fail
        ? Response.json({ detail: 'down', code: 'boom' }, { status: 500 })
        : Response.json({ ...shellView, timezone: 'Pacific/Auckland' })
    }
    if (path === '/api/v1/notifications') return Response.json(notificationsView)
    return Response.json(emptyQueue)
  })
  vi.stubGlobal('fetch', fetch)
  setDisplayTimeZone(null)
  const Page = () => <p>{formatShortDate('2026-06-17T22:00:00')}</p>
  renderAt('/', <Page />, <Shell />)
  const user = userEvent.setup()
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the workspace')
  expect(screen.queryByText("17 Jun '26")).not.toBeInTheDocument()
  fail = false
  await user.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText("18 Jun '26")).toBeVisible()
})
