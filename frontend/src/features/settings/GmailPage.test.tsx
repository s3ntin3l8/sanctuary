import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../../test/render'
import { GmailPage } from './GmailPage'

const gmail = {
  oauth_client: {
    configured: true,
    source: 'ui',
    client_id: '1-a.apps.googleusercontent.com',
    has_secret: true,
    redirect_uri: 'https://s.test/api/ingest/gmail/oauth/callback',
    can_edit: true,
  },
  connected: true,
  connected_at: '2026-10-01T09:00:00+00:00',
  last_sync_at: '2026-10-07T08:00:00+00:00',
  allowlist: ['lawyer@example.com'],
  label_filter: '',
  include_sent: true,
  own_addresses: ['me@example.com'],
  oauth_start_url: '/api/ingest/gmail/oauth/start',
  sync_mode: 'notify',
  last_check_at: null,
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

test('gmail: new connections notify by default, and the mode can be changed', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'PUT /api/v1/settings/gmail/sync-mode': { body: { ...gmail, sync_mode: 'auto' } },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const notify = await screen.findByRole('radio', { name: /Notify me/ })
  expect(notify).toBeChecked()
  expect(screen.getByRole('radio', { name: /^Off/ })).not.toBeChecked()

  const auto = screen.getByRole('radio', { name: /Import automatically/ })
  await userEvent.setup().click(auto)

  await waitFor(() => expect(auto).toBeChecked())
  expect(await sentTo(fetch, 'PUT', '/api/v1/settings/gmail/sync-mode')?.clone().json()).toEqual({
    mode: 'auto',
  })
})

test('gmail: the modes are disabled until Gmail is connected', async () => {
  stubApi({ 'GET /api/v1/settings/gmail': { body: { ...gmail, connected: false } } })
  renderAt('/settings/gmail', <GmailPage />)
  expect(await screen.findByRole('radio', { name: /Notify me/ })).toBeDisabled()
})

test('gmail: in notify mode the button checks for mail instead of importing it', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'POST /api/v1/gmail/new/check': { status: 202, body: null },
  })
  renderAt('/settings/gmail', <GmailPage />)
  expect(screen.queryByRole('button', { name: 'Sync now' })).toBeNull()
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Check for new mail' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Checking for new mail')
  expect(sentTo(fetch, 'POST', '/api/v1/gmail/new/check')).toBeDefined()
})

test('gmail: sync now queues a sync when the mode is off', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: { ...gmail, sync_mode: 'off' } },
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
      body: { ...gmail, connected: false, last_sync_at: null, sync_mode: 'off' },
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
  ).toEqual({
    allowlist: ['lawyer@example.com'],
    label_filter: '',
    include_sent: true,
    own_addresses: ['me@example.com'],
  })

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

test('gmail: filters save the sent-mail option and own addresses', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'PUT /api/v1/settings/gmail/filters': { body: gmail },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const user = userEvent.setup()
  const own = await screen.findByLabelText('Your addresses')
  expect(own).toHaveValue('me@example.com')
  await user.clear(own)
  await user.type(own, 'me@example.com, alias@example.com')
  await user.click(screen.getByRole('button', { name: 'Save filters' }))
  await waitFor(() => expect(sentTo(fetch, 'PUT', '/api/v1/settings/gmail/filters')).toBeDefined())
  expect(await sentTo(fetch, 'PUT', '/api/v1/settings/gmail/filters')?.clone().json()).toEqual({
    allowlist: ['lawyer@example.com'],
    label_filter: '',
    include_sent: true,
    own_addresses: ['me@example.com', 'alias@example.com'],
  })
})

test('gmail: turning off sent mail disables the address field', async () => {
  stubApi({ 'GET /api/v1/settings/gmail': { body: gmail } })
  renderAt('/settings/gmail', <GmailPage />)
  await userEvent
    .setup()
    .click(await screen.findByRole('switch', { name: /Also import mail I sent/ }))
  expect(screen.getByLabelText('Your addresses')).toBeDisabled()
})

test('gmail: saving the OAuth client without touching the secret sends none', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/gmail': { body: gmail },
    'PUT /api/v1/settings/gmail/oauth-client': { body: gmail },
  })
  renderAt('/settings/gmail', <GmailPage />)
  const id = await screen.findByLabelText('Client ID')
  await userEvent.clear(id)
  await userEvent.type(id, '2-b.apps.googleusercontent.com')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() =>
    expect(sentTo(fetch, 'PUT', '/api/v1/settings/gmail/oauth-client')).toBeTruthy(),
  )
  const request = sentTo(fetch, 'PUT', '/api/v1/settings/gmail/oauth-client')
  const sent = await request?.clone().json()
  expect(sent).toMatchObject({ client_id: '2-b.apps.googleusercontent.com', client_secret: null })
})

test('gmail: Connect is replaced by a hint until the OAuth client is set up', async () => {
  stubApi({
    'GET /api/v1/settings/gmail': {
      body: {
        ...gmail,
        connected: false,
        oauth_client: { ...gmail.oauth_client, configured: false, source: null, has_secret: false },
      },
    },
  })
  renderAt('/settings/gmail', <GmailPage />)
  expect(await screen.findByText('Set up the Google OAuth client first')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Connect Gmail' })).not.toBeInTheDocument()
})
