import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import * as navigation from '../navigation'
import { emptyQueue, shellView } from '../test/fixtures'
import { renderAt, stubApi } from '../test/render'
import { Shell } from './Shell'

function renderShell() {
  vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/shell': { body: shellView },
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
  expect(await screen.findByText('7')).toBeVisible() // triage badge
  expect(await screen.findByText('3')).toBeVisible() // processing badge: 1 executing + 2 queued
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
