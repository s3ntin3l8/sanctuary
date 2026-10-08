import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../../test/render'
import { GmailPage } from './GmailPage'

const gmail = {
  connected: true,
  connected_at: '2026-10-01T09:00:00+00:00',
  last_sync_at: '2026-10-07T08:00:00+00:00',
  allowlist: ['lawyer@example.com'],
  label_filter: '',
  oauth_start_url: '/api/ingest/gmail/oauth/start',
  auto_sync: false,
  last_sync_result: null,
  last_sync_error: null,
  reconnect_required: false,
  failed_count: 0,
  ai_external: false,
}

function sentTo(fetch: ReturnType<typeof stubApi>, method: string, path: string) {
  return fetch.mock.calls
    .map(([r]) => r)
    .find((r) => r.method === method && new URL(r.url).pathname === path)
}

test('gmail: automatic sync is off by default and the switch opts in', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'PUT /api/v1/settings/gmail/auto-sync': { body: { ...gmail, auto_sync: true } },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const toggle = await screen.findByRole('switch', { name: 'Automatic sync' })
  expect(toggle).toHaveAttribute('aria-checked', 'false')

  await userEvent.setup().click(toggle)

  await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'))
  expect(await sentTo(fetch, 'PUT', '/api/v1/settings/gmail/auto-sync')?.clone().json()).toEqual({
    enabled: true,
  })
})

test('gmail: sync now queues a sync', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'POST /api/v1/settings/gmail/sync': { status: 202, body: null },
  })
  renderAt('/settings/gmail', <GmailPage />)
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Sync now' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Sync queued')
  expect(sentTo(fetch, 'POST', '/api/v1/settings/gmail/sync')).toBeDefined()
})

test('gmail: a revoked grant says reconnect is required, with the reason', async () => {
  stubApi({
    'GET /api/v1/settings/gmail': {
      body: {
        ...gmail,
        reconnect_required: true,
        last_sync_error: 'Google rejected the stored Gmail token (revoked or expired).',
      },
    },
  })
  renderAt('/settings/gmail', <GmailPage />)
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Reconnect required — Google rejected the stored Gmail token',
  )
})

test('gmail: warns when an active AI endpoint is external', async () => {
  stubApi({ 'GET /api/v1/settings/gmail': { body: { ...gmail, ai_external: true } } })
  renderAt('/settings/gmail', <GmailPage />)
  expect(await screen.findByRole('alert')).toHaveTextContent('would leave this machine')
})

test('gmail: disconnecting needs a confirmation', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'DELETE /api/v1/settings/gmail': {
      body: { ...gmail, connected: false, last_sync_at: null, auto_sync: false },
    },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Disconnect' }))
  expect(sentTo(fetch, 'DELETE', '/api/v1/settings/gmail')).toBeUndefined()

  const dialog = screen.getByRole('dialog')
  await user.click(within(dialog).getByRole('button', { name: 'Disconnect' }))

  expect(await screen.findByText('No Gmail connected')).toBeVisible()
  expect(sentTo(fetch, 'DELETE', '/api/v1/settings/gmail')).toBeDefined()
})

test('gmail: changing the sync point sends the chosen date', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: { ...gmail, failed_count: 2 } },
    'POST /api/v1/settings/gmail/reset-sync': { body: { ...gmail, failed_count: 0 } },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Change sync point…' }))
  const dialog = screen.getByRole('dialog')
  await user.type(within(dialog).getByLabelText('Resume from'), '2026-02-01')
  await user.click(within(dialog).getByRole('button', { name: 'Change' }))

  await waitFor(() =>
    expect(sentTo(fetch, 'POST', '/api/v1/settings/gmail/reset-sync')).toBeDefined(),
  )
  expect(await sentTo(fetch, 'POST', '/api/v1/settings/gmail/reset-sync')?.clone().json()).toEqual({
    since: '2026-02-01',
  })
})

test('gmail: filters come before sync and need a sender or a label', async () => {
  stubApi({
    'GET /api/v1/settings/gmail': { body: { ...gmail, allowlist: [], label_filter: '' } },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const user = userEvent.setup()
  const save = await screen.findByRole('button', { name: 'Save filters' })
  expect(save).toBeDisabled()
  expect(screen.getByText('Required')).toBeVisible()
  expect(screen.getByRole('button', { name: 'Preview matches' })).toBeDisabled()

  const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
  expect(headings.indexOf('What Sanctuary reads')).toBeLessThan(headings.indexOf('Sync'))

  // A label alone is enough.
  await user.type(screen.getByLabelText('Label'), 'Sanctuary')
  expect(save).toBeEnabled()
  expect(screen.queryByText('Required')).toBeNull()
})

test('gmail: previewing the filters shows how many messages they match', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'POST /api/v1/settings/gmail/filters/preview': { body: { estimate: 214 } },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Preview matches' }))

  expect(await screen.findByText('214')).toBeVisible()
  expect(
    await sentTo(fetch, 'POST', '/api/v1/settings/gmail/filters/preview')?.clone().json(),
  ).toEqual({ allowlist: ['lawyer@example.com'], label_filter: '' })

  // Editing invalidates the count.
  await user.type(screen.getByLabelText('Label'), 'x')
  expect(screen.queryByText('214')).toBeNull()
})

test('gmail: saving filters keeps edits typed after clicking Save', async () => {
  const saved = { ...gmail, allowlist: ['lawyer@example.com'], label_filter: 'Sanctuary' }
  stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'PUT /api/v1/settings/gmail/filters': { body: saved },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const user = userEvent.setup()
  const label = await screen.findByLabelText('Label')
  await user.type(label, 'Sanctuary')
  await user.click(screen.getByRole('button', { name: 'Save filters' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Gmail filters saved')

  // Editing after a save is not clobbered by the (older) saved value.
  await user.type(label, '2')
  expect(label).toHaveValue('Sanctuary2')
})

test('gmail: preview explains why it is disabled when not connected', async () => {
  stubApi({ 'GET /api/v1/settings/gmail': { body: { ...gmail, connected: false } } })
  renderAt('/settings/gmail', <GmailPage />)
  expect(await screen.findByRole('button', { name: 'Preview matches' })).toBeDisabled()
  expect(screen.getAllByText('Connect Gmail first').length).toBeGreaterThan(0)
})
