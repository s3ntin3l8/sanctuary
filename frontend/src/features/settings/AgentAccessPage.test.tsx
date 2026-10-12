import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../../test/render'
import { AgentAccessPage } from './AgentAccessPage'

const token = {
  id: 7,
  name: 'Claude Code',
  prefix: 'sct_AbCd',
  access: 'read',
  all_cases: true,
  cases: [],
  created_at: '2026-10-12T08:00:00Z',
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
}
const view = { mcp_url: 'https://sanctuary.test/mcp', tokens: [token] }
const directory = {
  cases: [{ id: 'ADV-024-A', title: 'Vogt v. Meier' }],
  counts_by_status: {},
  total: 1,
}

function postedJson(fetch: ReturnType<typeof stubApi>, method: string, suffix: string) {
  const request = fetch.mock.calls
    .map(([r]) => r)
    .find((r) => r.method === method && r.url.endsWith(suffix))
  return request?.clone().json()
}

test('lists tokens without ever showing a secret', async () => {
  stubApi({
    'GET /api/v1/settings/agent-access': { body: view },
    'GET /api/v1/cases': { body: directory },
  })
  renderAt('/settings/agent-access', <AgentAccessPage />)
  expect(await screen.findByText('Claude Code')).toBeVisible()
  expect(screen.getByText(/sct_AbCd…/)).toBeVisible()
  expect(screen.getByText('https://sanctuary.test/mcp')).toBeVisible()
})

test('creating an all-cases token shows the one-time command', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/agent-access': { body: view },
    'GET /api/v1/cases': { body: directory },
    'POST /api/v1/settings/agent-access': {
      status: 201,
      body: { token: { ...token, id: 8, name: 'Desktop' }, secret: 'sct_SECRET' }, // pragma: allowlist secret
    },
  })
  renderAt('/settings/agent-access', <AgentAccessPage />)
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Name'), 'Desktop')
  await user.click(screen.getByRole('button', { name: 'Create token' }))
  await waitFor(async () =>
    expect(await postedJson(fetch, 'POST', '/agent-access')).toEqual({
      name: 'Desktop',
      access: 'read',
      all_cases: true,
      case_ids: [],
      expires_in_days: null,
    }),
  )
  expect(await screen.findByText(/Authorization: Bearer sct_SECRET/)).toBeVisible()
})

test('a per-case token cannot be created until a case is picked', async () => {
  stubApi({
    'GET /api/v1/settings/agent-access': { body: view },
    'GET /api/v1/cases': { body: directory },
  })
  renderAt('/settings/agent-access', <AgentAccessPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByLabelText('Only these cases'))
  expect(screen.getByRole('button', { name: 'Create token' })).toBeDisabled()
  await user.click(screen.getByLabelText(/ADV-024-A/))
  expect(screen.getByRole('button', { name: 'Create token' })).toBeEnabled()
})

test('revoking asks for confirmation first', async () => {
  const fetch = stubApi({
    'GET /api/v1/settings/agent-access': { body: view },
    'GET /api/v1/cases': { body: directory },
    'DELETE /api/v1/settings/agent-access/7': {
      body: { ...token, revoked_at: '2026-10-12T09:00:00Z' },
    },
  })
  renderAt('/settings/agent-access', <AgentAccessPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Revoke' }))
  expect(fetch.mock.calls.some(([r]) => r.method === 'DELETE')).toBe(false)
  await user.click(
    within(await screen.findByRole('dialog')).getByRole('button', { name: 'Revoke' }),
  )
  await waitFor(() => expect(fetch.mock.calls.some(([r]) => r.method === 'DELETE')).toBe(true))
})
