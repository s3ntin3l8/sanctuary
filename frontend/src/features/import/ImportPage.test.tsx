import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../../test/render'
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
const index = {
  running: false,
  indexed_count: 7,
  last_indexed_at: '2026-10-07T07:00:00+00:00',
  done: 0,
  total: 0,
  skipped: 0,
  error: null,
  cached_count: 0,
  cached_bytes: 0,
}
const idle = {
  active: false,
  total: 0,
  done: 0,
  failed_count: 0,
  sequential: true,
  cancelled: false,
  error: null,
  current_subject: null,
  waiting: false,
  started_at: null,
  finished_at: null,
}
const groups = {
  groups: [
    {
      key: '8372-25',
      kind: 'internal_id',
      matched_case_id: '8372-25',
      count: 4,
      ingested_count: 1,
      first_at: '2024-01-02T10:00:00+00:00',
      last_at: '2024-03-04T10:00:00+00:00',
    },
    {
      key: '3 F 426/25',
      kind: 'az_court',
      matched_case_id: null,
      count: 2,
      ingested_count: 0,
      first_at: '2024-02-01T10:00:00+00:00',
      last_at: '2024-02-09T10:00:00+00:00',
    },
    {
      key: 'unreferenced',
      kind: null,
      matched_case_id: null,
      count: 1,
      ingested_count: 0,
      first_at: '2024-01-01T10:00:00+00:00',
      last_at: '2024-01-01T10:00:00+00:00',
    },
  ],
}
const messages = {
  items: [
    {
      gmail_id: 'g1',
      thread_id: 't1',
      sender: 'lawyer@example.com',
      subject: 'Schriftsatz vom 2. Januar',
      sent_at: '2024-01-02T10:00:00+00:00',
      has_attachments: true,
      ingested: true,
      cached: true,
    },
    {
      gmail_id: 'g2',
      thread_id: 't1',
      sender: 'lawyer@example.com',
      subject: 'Ladung zur Verhandlung',
      sent_at: '2024-02-10T10:00:00+00:00',
      has_attachments: false,
      ingested: false,
      cached: false,
    },
  ],
  next_cursor: null,
}

const noNew = { count: 0, sync_mode: 'notify', since: null, checked_at: null, items: [] }

const base = {
  'GET /api/v1/settings/gmail': { body: gmail },
  'GET /api/v1/gmail/index/status': { body: index },
  'GET /api/v1/gmail/groups': { body: groups },
  'GET /api/v1/gmail/import/status': { body: idle },
  'GET /api/v1/gmail/messages': { body: messages },
  'GET /api/v1/gmail/new': { body: noNew },
}

function sent(fetch: ReturnType<typeof stubApi>, method: string, path: string) {
  return fetch.mock.calls
    .map(([r]) => r)
    .find((r) => r.method === method && new URL(r.url).pathname === path)
}

test('import: a label alone is enough to index; no filter at all asks for one', async () => {
  const stub = {
    ...base,
    'GET /api/v1/settings/gmail': { body: { ...gmail, allowlist: [], label_filter: 'Sanctuary' } },
  }
  stubApi(stub)
  const { unmount } = renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByRole('button', { name: /Refresh index/ })).toBeEnabled()
  unmount()

  stubApi({
    ...base,
    'GET /api/v1/settings/gmail': { body: { ...gmail, allowlist: [], label_filter: '' } },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByText(/Set a sender allowlist or a label/)).toBeVisible()
  expect(screen.getByRole('button', { name: /Refresh index/ })).toBeDisabled()
})

test('import: lists case references oldest first with the case each files into', async () => {
  stubApi(base)
  renderAt('/settings/gmail/import', <ImportPage />)

  expect(await screen.findByText('7 messages indexed · refreshed 2026-10-07')).toBeVisible()
  const rows = (await screen.findAllByRole('row')).slice(1)
  expect(rows).toHaveLength(3)
  const [first, second, third] = rows as [HTMLElement, HTMLElement, HTMLElement]
  // File numbers read "8372/25"; the case it files into is a link.
  expect(within(first).getByText('8372/25')).toBeVisible()
  expect(within(first).getByRole('link', { name: '8372-25' })).toHaveAttribute(
    'href',
    '/cases/8372-25',
  )
  expect(within(first).getByText('1/4')).toBeVisible()
  expect(within(second).getByText('no case yet')).toBeVisible()
  expect(within(third).getByText('No reference')).toBeVisible()
  expect(screen.getByText('1 of 7 imported')).toBeVisible()
})

test('import: expanding a group shows its messages, already imported ones cannot be picked', async () => {
  const fetch = stubApi({
    ...base,
    'POST /api/v1/gmail/import': { status: 202, body: { queued: 1 } },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  const user = userEvent.setup()

  await user.click(await screen.findByRole('button', { name: 'Expand 8372/25' }))

  expect(await screen.findByText('Ladung zur Verhandlung')).toBeVisible()
  expect(screen.getByRole('checkbox', { name: 'Select Schriftsatz vom 2. Januar' })).toBeDisabled()
  await user.click(screen.getByRole('checkbox', { name: 'Select Ladung zur Verhandlung' }))
  await user.click(screen.getByRole('button', { name: 'Import selected (1)' }))

  expect(await screen.findByRole('status')).toHaveTextContent('Queued 1 message')
  expect(await sent(fetch, 'POST', '/api/v1/gmail/import')?.clone().json()).toEqual({
    gmail_ids: ['g2'],
    sequential: true,
    new: false,
  })
})

test('import: "next N oldest" covers everything, or just the open reference', async () => {
  const fetch = stubApi({
    ...base,
    'POST /api/v1/gmail/import': { status: 202, body: { queued: 5 } },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  const user = userEvent.setup()
  const post = () => sent(fetch, 'POST', '/api/v1/gmail/import')?.clone().json()

  await user.click(await screen.findByRole('button', { name: 'Import' }))
  await waitFor(() => expect(sent(fetch, 'POST', '/api/v1/gmail/import')).toBeDefined())
  expect(await post()).toEqual({ oldest_n: 25, group: null, sequential: true, new: false })

  await user.click(screen.getByRole('button', { name: 'Expand 8372/25' }))
  expect(await screen.findByText('8372/25', { selector: 'strong' })).toBeVisible()
  const count = screen.getByRole('spinbutton', { name: 'How many' })
  await user.clear(count)
  await user.type(count, '10')
  await user.click(screen.getByRole('checkbox', { name: /strictly in order/ }))
  fetch.mockClear()
  await user.click(screen.getByRole('button', { name: 'Import' }))

  await waitFor(() => expect(sent(fetch, 'POST', '/api/v1/gmail/import')).toBeDefined())
  expect(await post()).toEqual({
    oldest_n: 10,
    group: '8372-25',
    sequential: false,
    new: false,
  })
})

test('import: a running import shows what it is waiting for and can be stopped', async () => {
  const fetch = stubApi({
    ...base,
    'GET /api/v1/gmail/import/status': {
      body: {
        ...idle,
        active: true,
        total: 5,
        done: 2,
        waiting: true,
        current_subject: 'Ladung zur Verhandlung',
      },
    },
    'DELETE /api/v1/gmail/import': { status: 204, body: null },
  })
  renderAt('/settings/gmail/import', <ImportPage />)

  expect(await screen.findByText(/Importing 2\/5/)).toHaveTextContent(
    'waiting for “Ladung zur Verhandlung” to finish processing',
  )
  expect(screen.getByRole('button', { name: 'Import' })).toBeDisabled()
  await userEvent.setup().click(screen.getByRole('button', { name: 'Stop' }))
  await waitFor(() => expect(sent(fetch, 'DELETE', '/api/v1/gmail/import')).toBeDefined())
})

test('import: a finished run reports what happened', async () => {
  stubApi({
    ...base,
    'GET /api/v1/gmail/import/status': {
      body: {
        ...idle,
        total: 5,
        done: 5,
        failed_count: 1,
        finished_at: '2026-10-07T09:00:00+00:00',
      },
    },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByText(/Imported 5 of 5/)).toHaveTextContent('1 failed')
})

test('import: with nothing indexed it asks for a refresh and starts indexing', async () => {
  const fetch = stubApi({
    ...base,
    'GET /api/v1/gmail/index/status': {
      body: { ...index, indexed_count: 0, last_indexed_at: null },
    },
    'GET /api/v1/gmail/groups': { body: { groups: [] } },
    'POST /api/v1/gmail/index': { status: 202, body: null },
  })
  renderAt('/settings/gmail/import', <ImportPage />)

  expect(await screen.findByText(/Nothing indexed yet/)).toBeVisible()
  await userEvent.setup().click(screen.getByRole('button', { name: /Refresh index/ }))
  await waitFor(() => expect(sent(fetch, 'POST', '/api/v1/gmail/index')).toBeDefined())
})

test('import: shows index progress while the mailbox is being read', async () => {
  stubApi({
    ...base,
    'GET /api/v1/gmail/index/status': { body: { ...index, running: true, done: 200, total: 800 } },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByText('Reading message headers — 200 of 800')).toBeVisible()
  expect(screen.getByRole('button', { name: /Indexing/ })).toBeDisabled()
})

test('import: without a Gmail connection it points at the settings', async () => {
  stubApi({
    ...base,
    'GET /api/v1/settings/gmail': { body: { ...gmail, connected: false } },
    'GET /api/v1/gmail/index/status': { body: { ...index, indexed_count: 0 } },
    'GET /api/v1/gmail/groups': { body: { groups: [] } },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Connect Gmail in Gmail settings first.',
  )
  expect(screen.getByRole('button', { name: /Refresh index/ })).toBeDisabled()
})

test('import: tells you when some messages could not be read from Gmail', async () => {
  stubApi({
    ...base,
    'GET /api/v1/gmail/index/status': { body: { ...index, skipped: 3 } },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByText(/3 messages couldn.t be read from Gmail/)).toBeVisible()
})

test('import: an index error while Celery retries says so, and a final failure says failed', async () => {
  stubApi({
    ...base,
    'GET /api/v1/gmail/index/status': {
      body: { ...index, running: true, done: 0, total: 10, error: 'quota' },
    },
  })
  const { unmount } = renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByRole('alert')).toHaveTextContent('hit an error and is retrying — quota')
  unmount()

  stubApi({ ...base, 'GET /api/v1/gmail/index/status': { body: { ...index, error: 'quota' } } })
  renderAt('/settings/gmail/import', <ImportPage />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Index refresh failed — quota')
})

test('import: shows what is cached locally and marks cached messages', async () => {
  stubApi({
    ...base,
    'GET /api/v1/gmail/index/status': {
      body: { ...index, cached_count: 12, cached_bytes: 3 * 1024 * 1024 },
    },
  })
  renderAt('/settings/gmail/import', <ImportPage />)

  expect(await screen.findByText(/12 cached \(3\.0 MB\)/)).toBeVisible()
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Expand 8372/25' }))
  const cachedRow = (await screen.findByText('Schriftsatz vom 2. Januar')).closest(
    'li',
  ) as HTMLElement
  const freshRow = screen.getByText('Ladung zur Verhandlung').closest('li') as HTMLElement
  expect(within(cachedRow).getByText('Cached locally')).toBeInTheDocument()
  expect(within(freshRow).queryByText('Cached locally')).not.toBeInTheDocument()
})

test('import: clearing the cache asks first and says it only touches local copies', async () => {
  const fetch = stubApi({
    ...base,
    'GET /api/v1/gmail/index/status': { body: { ...index, cached_count: 2, cached_bytes: 2048 } },
    'DELETE /api/v1/gmail/cache': { status: 204, body: null },
  })
  renderAt('/settings/gmail/import', <ImportPage />)
  const user = userEvent.setup()

  await user.click(await screen.findByRole('button', { name: /Clear cache/ }))
  const dialog = screen.getByRole('dialog')
  expect(dialog).toHaveTextContent('Nothing in Gmail and no already-imported bundle is touched')
  expect(sent(fetch, 'DELETE', '/api/v1/gmail/cache')).toBeUndefined()

  await user.click(within(dialog).getByRole('button', { name: 'Clear cache' }))
  await waitFor(() => expect(sent(fetch, 'DELETE', '/api/v1/gmail/cache')).toBeDefined())
  expect(await screen.findByRole('status')).toHaveTextContent('Local mail cache cleared')
})

test('import: no cache, no clear button', async () => {
  stubApi(base)
  renderAt('/settings/gmail/import', <ImportPage />)
  await screen.findByText(/7 messages indexed/)
  expect(screen.queryByRole('button', { name: /Clear cache/ })).not.toBeInTheDocument()
})
