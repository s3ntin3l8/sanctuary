import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { adminUsers, aiSettings, emptyQueue, shellView } from '../../test/fixtures'
import { renderAt, stubApi } from '../../test/render'
import { AdminUsersPage } from '../admin/AdminUsersPage'
import { AccountPage } from './AccountPage'
import { AiPage } from './AiPage'
import { DataPage } from './DataPage'
import { SettingsLayout } from './SettingsLayout'

const account = {
  email: 'k.vogt@ra-vogt.de',
  display_name: 'Katharina Vogt',
  role: 'admin',
  has_password: true,
}

function postedJson(fetch: ReturnType<typeof stubApi>, method: string, suffix: string) {
  const request = fetch.mock.calls
    .map(([r]) => r)
    .find((r) => r.method === method && r.url.endsWith(suffix))
  return request?.clone().json()
}

test('settings nav hides admin-only tabs for regular users', async () => {
  stubApi({
    'GET /api/v1/shell': { body: { ...shellView, user: { ...shellView.user, role: 'user' } } },
    'GET /api/v1/settings/account': { body: { ...account, role: 'user' } },
  })
  renderAt('/settings/account', <AccountPage />, <SettingsLayout />)
  expect(await screen.findByRole('link', { name: 'Account' })).toBeVisible()
  await waitFor(() =>
    expect(screen.queryByRole('link', { name: 'AI & Models' })).not.toBeInTheDocument(),
  )
  expect(screen.queryByRole('link', { name: 'Users' })).not.toBeInTheDocument()
})

test('account: saves the display name and surfaces a wrong password', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/account': { body: account },
    'PUT /api/v1/settings/account/profile': { body: { ...account, display_name: 'K. Vogt' } },
    'PUT /api/v1/settings/account/password': {
      status: 422,
      body: { detail: 'Current password is incorrect.', code: 'wrong_password' },
    },
  })
  renderAt('/settings/account', <AccountPage />)
  const user = userEvent.setup()
  const name = await screen.findByLabelText('Display name')
  await user.clear(name)
  await user.type(name, 'K. Vogt')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(async () =>
    expect(await postedJson(fetch, 'PUT', '/profile')).toEqual({ display_name: 'K. Vogt' }),
  )
  expect(await screen.findByRole('status')).toHaveTextContent('Profile updated')

  const passwordForm = screen.getByRole('button', { name: 'Change password' }).closest('form')
  if (!passwordForm) throw new Error('password form not found')
  await user.type(within(passwordForm).getByLabelText('Current password'), 'nope')
  await user.type(within(passwordForm).getByLabelText('New password'), 'newpassword1')
  await user.click(screen.getByRole('button', { name: 'Change password' }))
  expect(await screen.findByText('Current password is incorrect.')).toBeVisible()
})

test('ai: lists endpoints and switching a role endpoint puts the role', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/ai': { body: aiSettings },
    'GET /api/v1/settings/ai/health': {
      body: {
        chat: { ok: true, provider: 'ollama', detail: '4 models available' },
        embed: { ok: true, provider: 'ollama', detail: '4 models available' },
        ocr: { ok: false, provider: null, detail: 'HTTP 500' },
      },
    },
    'GET /api/v1/settings/ai/instances/inst_a/models': {
      body: { chat: ['qwen3.5:9b'], embed: ['nomic-embed-text'], ocr: [] },
    },
    'GET /api/v1/settings/ai/instances/inst_b/models': {
      body: { chat: [], embed: [], ocr: ['chandra-ocr'] },
    },
    'PUT /api/v1/settings/ai/roles/ocr': {
      body: {
        role: { ...aiSettings.roles[2], active_id: 'inst_a', model: '' },
        health: { ok: true, provider: 'ollama', detail: '4 models available' },
        warning: null,
        embed_index: aiSettings.embed_index,
      },
    },
  })
  renderAt('/settings/ai', <AiPage />)
  expect((await screen.findAllByText('Ollama · local')).length).toBeGreaterThan(0)
  expect(screen.getAllByText('LM Studio · OCR').length).toBeGreaterThan(0)
  expect(await screen.findByText('HTTP 500')).toBeVisible()
  const ocrEndpoint = screen.getByLabelText('Endpoint', { selector: '#endpoint-ocr' })
  await userEvent.selectOptions(ocrEndpoint, 'inst_a')
  await waitFor(async () =>
    expect(await postedJson(fetch, 'PUT', '/roles/ocr')).toEqual({
      instance_id: 'inst_a',
      model: '',
    }),
  )
})

test('data: clearing the workspace needs a confirmation and reports the result', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/data': {
      body: {
        doc_count: 148,
        case_count: 12,
        claim_count: 63,
        cost_count: 27,
        db_size_mb: 84,
        ai_debug_redact: true,
      },
    },
    'GET /api/v1/settings/data/debug-logs': { body: { rows: [], log_root: '/data/ai_debug' } },
    'POST /api/v1/settings/data/clear-all-data': {
      body: { message: 'Cleared 300 database rows; 12 disk artifacts removed.' },
    },
  })
  renderAt('/settings/data', <DataPage />)
  const user = userEvent.setup()
  expect(await screen.findByText('148')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Clear' }))
  const dialog = screen.getByRole('dialog')
  expect(fetch.mock.calls.some(([r]) => r.method === 'POST')).toBe(false)
  await user.click(within(dialog).getByRole('button', { name: 'Clear workspace' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Cleared 300 database rows')
})

test('admin: creates a user and never offers self-destructive actions on yourself', async () => {
  const fetch = stubApi({
    'GET /api/v1/shell': { body: shellView },
    'GET /api/v1/worker-queue': { body: emptyQueue },
    'GET /api/v1/admin/users': { body: adminUsers },
    'POST /api/v1/admin/users': {
      status: 201,
      body: {
        ...adminUsers,
        users: [
          ...adminUsers.users,
          { ...adminUsers.users[1], id: 3, email: 'new@example.com', display_name: null },
        ],
      },
    },
  })
  renderAt('/admin/users', <AdminUsersPage />)
  const user = userEvent.setup()
  expect(await screen.findByText('Users (2)')).toBeVisible()
  const [me, other] = screen.getAllByRole('listitem')
  if (!me || !other) throw new Error('expected two user rows')
  expect(within(me).getByText('you')).toBeVisible()
  expect(within(me).queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
  expect(within(other).getByRole('button', { name: 'Delete' })).toBeVisible()

  await user.type(screen.getByLabelText('Email'), 'new@example.com')
  await user.type(screen.getByLabelText('Password'), 'password123') // pragma: allowlist secret
  await user.click(screen.getByRole('button', { name: 'Create' }))
  await waitFor(async () =>
    expect(await postedJson(fetch, 'POST', '/admin/users')).toEqual({
      email: 'new@example.com',
      password: 'password123', // pragma: allowlist secret
      role: 'user',
    }),
  )
  expect(await screen.findByText('Users (3)')).toBeVisible()
})

test('ai: editing an endpoint keeps, replaces or clears the stored key', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/ai': { body: aiSettings },
    'GET /api/v1/settings/ai/health': {
      body: {
        chat: { ok: true, provider: null, detail: 'ok' },
        embed: { ok: true, provider: null, detail: 'ok' },
        ocr: { ok: true, provider: null, detail: 'ok' },
      },
    },
    'GET /api/v1/settings/ai/instances/inst_a/models': { body: { chat: [], embed: [], ocr: [] } },
    'GET /api/v1/settings/ai/instances/inst_b/models': { body: { chat: [], embed: [], ocr: [] } },
    'PUT /api/v1/settings/ai/instances/inst_b': { body: aiSettings.instances[1] },
  })
  renderAt('/settings/ai', <AiPage />)
  const user = userEvent.setup()
  const bodies = async () =>
    Promise.all(
      fetch.mock.calls
        .map(([r]) => r)
        .filter((r) => r.method === 'PUT')
        .map((r) => r.clone().json()),
    )

  const openSecondEndpoint = async () => {
    const edits = await screen.findAllByRole('button', { name: 'Edit' })
    const second = edits[1]
    if (!second) throw new Error('expected two endpoints')
    await user.click(second)
    return screen.getByRole('dialog')
  }

  // Untouched key field → null (keep).
  let dialog = await openSecondEndpoint()
  await user.click(within(dialog).getByRole('button', { name: 'Save' }))
  await waitFor(async () => expect((await bodies()).at(-1)).toMatchObject({ api_key: null }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

  // Remove-the-stored-key checkbox → "" (clear).
  dialog = await openSecondEndpoint()
  await user.click(within(dialog).getByLabelText('Remove the stored key'))
  await user.click(within(dialog).getByRole('button', { name: 'Save' }))
  await waitFor(async () => expect((await bodies()).at(-1)).toMatchObject({ api_key: '' }))
})

test('settings pages explain a 403 instead of rendering nothing', async () => {
  stubApi({
    'GET /api/v1/settings/data': {
      status: 403,
      body: { detail: 'Admin access required', code: 'forbidden' },
    },
    'GET /api/v1/settings/data/debug-logs': {
      status: 403,
      body: { detail: 'Admin access required', code: 'forbidden' },
    },
  })
  renderAt('/settings/data', <DataPage />)
  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Only an administrator can open this page.',
  )
})
