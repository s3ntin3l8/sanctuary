import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test } from 'vitest'

import type { Schemas } from '../../api/client'

import { emptyQueue, notificationsView, shellView, triageView } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { Shell } from '../../shell/Shell'
import { TriagePage } from '../triage/TriagePage'

type Run = Schemas['GmailImportStatus']

const running: Run = {
  active: true,
  total: 3,
  done: 1,
  failed_count: 0,
  cancelled: false,
  error: null,
  current_subject: 'Schriftsatz 8372/25',
  started_at: '2026-10-08T06:00:00Z',
  finished_at: null,
}
const finished: Run = { ...running, active: false, done: 3, current_subject: null }

const queueWith = (gmail_import: Run | null) => ({
  ...emptyQueue,
  counts: { ...emptyQueue.counts, queued: gmail_import?.active ? 2 : 0 },
  gmail_import,
})

afterEach(() => sessionStorage.clear())

/** Stubs the shell endpoints into `replies`, which tests may mutate to change a later poll. */
function shellStub(replies: Parameters<typeof stubApi>[0]) {
  replies['GET /api/v1/shell'] = { body: shellView }
  replies['GET /api/v1/notifications'] = { body: notificationsView }
  return stubApi(replies)
}

// The shell polls the queue every 5 s; tests that change the reply wait for the next poll.
const POLL = { timeout: 8000 }
const SLOW = 20_000

test('queue: a live import shows in the hover card, the modal, and can be stopped', async () => {
  const fetch = shellStub({
    'GET /api/v1/worker-queue': { body: queueWith(running) },
    'DELETE /api/v1/gmail/import': { status: 204, body: null },
  })
  renderAt('/', <Shell />)
  const user = userEvent.setup()

  expect(await screen.findByText('Importing from Gmail · 1/3')).toBeInTheDocument()
  await user.click(await screen.findByRole('button', { name: 'Processing queue' }))
  const dialog = screen.getByRole('dialog')
  expect(within(dialog).getByText(/Importing 1\/3/)).toBeVisible()
  expect(within(dialog).getByRole('link', { name: 'Import history' })).toHaveAttribute(
    'href',
    '/settings/gmail/import',
  )
  await user.click(within(dialog).getByRole('button', { name: 'Stop' }))
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(([r]) => r.method === 'DELETE' && r.url.endsWith('/gmail/import')),
    ).toBe(true),
  )
})

test('a finished import is announced once, not on every poll', async () => {
  const replies: Parameters<typeof stubApi>[0] = {
    'GET /api/v1/worker-queue': { body: queueWith(running) },
  }
  shellStub(replies)
  renderAt('/', <Shell />)
  await screen.findByText('Importing from Gmail · 1/3')

  replies['GET /api/v1/worker-queue'] = { body: queueWith(finished) }
  expect(await screen.findByText(/Gmail import finished: 3 imported/, {}, POLL)).toBeVisible()

  // Toasts clear after 4 s; a poll that still sees the finished run must not bring it back.
  await waitFor(() => expect(screen.queryByText(/Gmail import finished/)).toBeNull(), POLL)
  await new Promise((resolve) => setTimeout(resolve, 5_500))
  expect(screen.queryByText(/Gmail import finished/)).toBeNull()
}, 30_000)

test(
  'an import that was already over when the page opened is not announced',
  async () => {
    shellStub({ 'GET /api/v1/worker-queue': { body: queueWith(finished) } })
    renderAt('/', <Shell />)
    await screen.findByRole('button', { name: 'Processing queue' })
    await new Promise((resolve) => setTimeout(resolve, 5_500))
    expect(screen.queryByText(/Gmail import finished/)).toBeNull()
  },
  SLOW,
)

test(
  'triage: shows a running import, then a finished one that can be dismissed for good',
  async () => {
    const replies = {
      'GET /api/v1/triage': { body: triageView },
      'GET /api/v1/worker-queue': { body: queueWith(running) },
    }
    stubApi(replies)
    const { unmount } = renderAt('/triage', <TriagePage />)
    expect(await screen.findByText(/Importing 1\/3/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeVisible()

    replies['GET /api/v1/worker-queue'] = { body: queueWith(finished) }
    expect(await screen.findByText(/Imported 3 of 3/, {}, POLL)).toBeVisible()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(/Imported 3 of 3/)).toBeNull()

    // Remembered for this run across reloads of the page.
    unmount()
    renderAt('/triage', <TriagePage />)
    await screen.findByText('Klageerwiderung')
    expect(screen.queryByText(/Imported 3 of 3/)).toBeNull()
  },
  SLOW,
)

test('triage: no banner without an import', async () => {
  stubApi({
    'GET /api/v1/triage': { body: triageView },
    'GET /api/v1/worker-queue': { body: queueWith(null) },
  })
  renderAt('/triage', <TriagePage />)
  await screen.findByText('Klageerwiderung')
  expect(screen.queryByText(/Importing/)).toBeNull()
})
