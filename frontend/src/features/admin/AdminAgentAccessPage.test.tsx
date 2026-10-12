import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'

import { renderAt, stubApi } from '../../test/render'
import { AdminAgentAccessPage } from './AdminAgentAccessPage'

const token = {
  id: 7,
  name: 'Claude Code',
  prefix: 'sct_AbCd',
  access: 'read_write',
  all_cases: false,
  kind: 'static',
  oauth_client_name: null,
  cases: [{ id: 'ADV-024-A', title: 'Vogt v. Meier' }],
  created_at: '2026-10-12T08:00:00Z',
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
  owner_id: 2,
  owner_email: 'alice@example.com',
  owner_name: 'Alice',
  status: 'active',
  activity_count_7d: 3,
}
const view = {
  mcp_enabled: true,
  tokens: [
    token,
    {
      ...token,
      id: 8,
      name: 'Old',
      owner_name: 'Bob',
      status: 'revoked',
      activity_count_7d: 0,
    },
  ],
}

function requests(fetch: ReturnType<typeof stubApi>, method: string, suffix: string) {
  return fetch.mock.calls
    .map(([r]) => r)
    .filter((r) => r.method === method && r.url.endsWith(suffix))
}

test('lists every users tokens with owner, scope, status and activity count', async () => {
  stubApi({ 'GET /api/v1/admin/agent-access': { body: view } })
  renderAt('/admin/agent-access', <AdminAgentAccessPage />)
  expect(await screen.findByText('Claude Code')).toBeVisible()
  expect(screen.getByText(/Alice · sct_AbCd… · ADV-024-A/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Activity (3)' })).toBeVisible()
  // Only the active token can be revoked.
  expect(screen.getAllByRole('button', { name: 'Revoke' })).toHaveLength(1)
})

test('points to Settings → Agent access for creating tokens, even with none yet', async () => {
  stubApi({ 'GET /api/v1/admin/agent-access': { body: { ...view, tokens: [] } } })
  renderAt('/admin/agent-access', <AdminAgentAccessPage />)
  expect(await screen.findByText('No tokens yet.')).toBeVisible()
  expect(screen.getByRole('link', { name: 'Settings → Agent access' })).toHaveAttribute(
    'href',
    '/settings/agent-access',
  )
})

test('switching MCP off asks for confirmation first', async () => {
  const fetch = stubApi({
    'GET /api/v1/admin/agent-access': { body: view },
    'PUT /api/v1/admin/agent-access/enabled': { body: { ...view, mcp_enabled: false } },
    'GET /api/v1/settings/agent-access': { body: {} },
  })
  renderAt('/admin/agent-access', <AdminAgentAccessPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('switch', { name: 'MCP access' }))
  expect(requests(fetch, 'PUT', '/enabled')).toHaveLength(0)
  const dialog = await screen.findByRole('dialog')
  expect(dialog).toHaveTextContent(/cut off immediately/)
  await user.click(within(dialog).getByRole('button', { name: 'Switch off' }))
  await waitFor(() => expect(requests(fetch, 'PUT', '/enabled')).toHaveLength(1))
  const [put] = requests(fetch, 'PUT', '/enabled')
  expect(await put?.clone().json()).toEqual({ enabled: false })
})

test('revoking asks for confirmation first', async () => {
  const fetch = stubApi({
    'GET /api/v1/admin/agent-access': { body: view },
    'DELETE /api/v1/admin/agent-access/tokens/7': {
      body: { ...view, tokens: [{ ...token, status: 'revoked' }] },
    },
    'GET /api/v1/settings/agent-access': { body: {} },
  })
  renderAt('/admin/agent-access', <AdminAgentAccessPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Revoke' }))
  expect(requests(fetch, 'DELETE', '/tokens/7')).toHaveLength(0)
  await user.click(
    within(await screen.findByRole('dialog')).getByRole('button', { name: 'Revoke' }),
  )
  await waitFor(() => expect(requests(fetch, 'DELETE', '/tokens/7')).toHaveLength(1))
})

test('expands the recent activity of a token', async () => {
  stubApi({
    'GET /api/v1/admin/agent-access': { body: view },
    'GET /api/v1/admin/agent-access/tokens/7/activity': {
      body: {
        token_id: 7,
        entries: [
          {
            id: 1,
            created_at: '2026-10-12T08:30:00Z',
            event_type: 'mcp_write',
            tool: 'add_pin',
            target_type: 'document',
            target_id: '12',
            detail: {},
          },
        ],
      },
    },
  })
  renderAt('/admin/agent-access', <AdminAgentAccessPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Activity (3)' }))
  expect(await screen.findByText('add_pin')).toBeVisible()
  expect(screen.getByText('document 12')).toBeVisible()
})

test('marks tokens granted to an MCP client through OAuth', async () => {
  stubApi({
    'GET /api/v1/admin/agent-access': {
      body: {
        mcp_enabled: true,
        tokens: [{ ...token, id: 9, name: 'Claude', kind: 'oauth', oauth_client_name: 'Claude' }],
      },
    },
  })
  renderAt('/admin/agent-access', <AdminAgentAccessPage />)
  expect(await screen.findByText('via Claude')).toBeVisible()
})
