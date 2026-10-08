import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { emptyQueue, triageView } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { TriagePage } from '../triage/TriagePage'
import { ImportPage } from './ImportPage'

const gmail = {
  connected: true,
  connected_at: '2026-10-01T09:00:00+00:00',
  last_sync_at: '2026-10-07T08:00:00+00:00',
  allowlist: ['lawyer@example.com'],
  label_filter: '',
  oauth_start_url: '/api/ingest/gmail/oauth/start',
  sync_mode: 'notify',
  last_check_at: null,
  last_sync_result: null,
  last_sync_error: null,
  reconnect_required: false,
  failed_count: 0,
  ai_external: false,
}
const message = (id: string, subject: string) => ({
  gmail_id: id,
  thread_id: `t-${id}`,
  sender: 'lawyer@example.com',
  subject,
  sent_at: '2026-10-09T09:00:00+00:00',
  has_attachments: true,
  ingested: false,
  cached: false,
})
const news = {
  count: 2,
  sync_mode: 'notify',
  since: '2026-10-07T08:00:00+00:00',
  checked_at: '2026-10-09T10:00:00+00:00',
  items: [message('n1', '8372/25 Ladung'), message('n2', '8372/25 Schriftsatz')],
}
const idle = {
  active: false,
  total: 0,
  done: 0,
  failed_count: 0,
  cancelled: false,
  error: null,
  current_subject: null,
  waiting: false,
  started_at: null,
  finished_at: null,
}

function importPage(replies: Parameters<typeof stubApi>[0] = {}) {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'GET /api/v1/gmail/index/status': {
      body: {
        running: false,
        indexed_count: 7,
        last_indexed_at: null,
        done: 0,
        total: 0,
        skipped: 0,
        error: null,
        cached_count: 0,
        cached_bytes: 0,
      },
    },
    'GET /api/v1/gmail/groups': { body: { groups: [] } },
    'GET /api/v1/gmail/import/status': { body: idle },
    'GET /api/v1/gmail/new': { body: news },
    ...replies,
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  return fetch
}

async function body(fetch: ReturnType<typeof stubApi>, method: string, path: string) {
  const r = fetch.mock.calls
    .map(([x]) => x)
    .find((x) => x.method === method && new URL(x.url).pathname === path)
  return r?.clone().json()
}

test('banner: announces new mail and imports all of it', async () => {
  const fetch = importPage({
    'POST /api/v1/gmail/import': { status: 202, body: { queued: 2 } },
  })
  const banner = await screen.findByRole('region', { name: 'New mail' })
  expect(within(banner).getByText('2 new messages')).toBeVisible()

  await userEvent.setup().click(within(banner).getByRole('button', { name: 'Import all' }))

  expect(await screen.findByRole('status')).toHaveTextContent('Queued 2 messages')
  expect(await body(fetch, 'POST', '/api/v1/gmail/import')).toEqual({
    new: true,
    })
})

test('banner: review lists the messages and imports a hand-picked few', async () => {
  const fetch = importPage({
    'POST /api/v1/gmail/import': { status: 202, body: { queued: 1 } },
  })
  const user = userEvent.setup()
  const banner = await screen.findByRole('region', { name: 'New mail' })
  await user.click(within(banner).getByRole('button', { name: 'Review' }))

  expect(within(banner).getByText('8372/25 Ladung')).toBeVisible()
  const pick = within(banner).getByRole('button', { name: /Import selected \(0\)/ })
  expect(pick).toBeDisabled()
  await user.click(within(banner).getByRole('checkbox', { name: /Select 8372\/25 Schriftsatz/ }))
  await user.click(within(banner).getByRole('button', { name: /Import selected \(1\)/ }))

  await waitFor(async () =>
    expect(await body(fetch, 'POST', '/api/v1/gmail/import')).toEqual({
      new: true,
          gmail_ids: ['n2'],
    }),
  )
})

test('banner: skipping asks first, then moves the sync point and clears the banner', async () => {
  const fetch = importPage({
    'POST /api/v1/gmail/new/dismiss': {
      body: { ...news, count: 0, items: [], since: '2026-10-09T09:00:00+00:00' },
    },
  })
  const user = userEvent.setup()
  const banner = await screen.findByRole('region', { name: 'New mail' })
  await user.click(within(banner).getByRole('button', { name: 'Skip' }))
  expect(await body(fetch, 'POST', '/api/v1/gmail/new/dismiss')).toBeUndefined()

  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Skip' }))

  await waitFor(() => expect(screen.queryByRole('region', { name: 'New mail' })).toBeNull())
})

test('banner: check now asks the server to look', async () => {
  const fetch = importPage({ 'POST /api/v1/gmail/new/check': { status: 202, body: null } })
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Check now' }))
  await waitFor(() =>
    expect(
      fetch.mock.calls.some(([r]) => r.method === 'POST' && r.url.endsWith('/gmail/new/check')),
    ).toBe(true),
  )
})

test('banner: hidden with nothing new, and in automatic mode where nothing awaits a decision', async () => {
  importPage({ 'GET /api/v1/gmail/new': { body: { ...news, count: 0, items: [] } } })
  await screen.findByText(/messages indexed/)
  expect(screen.queryByRole('region', { name: 'New mail' })).toBeNull()
})

test('banner: not shown to people who import automatically', async () => {
  importPage({ 'GET /api/v1/gmail/new': { body: { ...news, sync_mode: 'auto' } } })
  await screen.findByText(/messages indexed/)
  expect(screen.queryByRole('region', { name: 'New mail' })).toBeNull()
})

test('triage: a notice and a count on the Import button, replaced by the run while importing', async () => {
  const replies: Parameters<typeof stubApi>[0] = {
    'GET /api/v1/triage': { body: triageView },
    'GET /api/v1/gmail/new': { body: news },
    'GET /api/v1/worker-queue': { body: { ...emptyQueue, gmail_import: null } },
  }
  stubApi(replies)
  renderAt('/triage', <TriagePage />)

  expect(await screen.findByText('2 new messages')).toBeVisible()
  expect(screen.getByRole('link', { name: /Import from Gmail/ })).toHaveTextContent('2')
  expect(screen.getByRole('link', { name: 'Review' })).toHaveAttribute(
    'href',
    '/settings/gmail/import',
  )
})
