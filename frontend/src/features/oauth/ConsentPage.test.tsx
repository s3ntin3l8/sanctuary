import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import * as navigation from '../../navigation'
import { renderAt, stubApi } from '../../test/render'
import { ConsentPage } from './ConsentPage'

const path =
  '/oauth/authorize?client_id=c1&redirect_uri=https%3A%2F%2Fclaude.ai%2Fcb' +
  '&code_challenge=abc&state=xyz&explicit=1'
const request = { client_name: 'Claude', redirect_host: 'claude.ai', mcp_enabled: true }
const directory = {
  cases: [{ id: 'ADV-024-A', title: 'Vogt v. Meier' }],
  counts_by_status: {},
  total: 1,
}

function postedJson(fetch: ReturnType<typeof stubApi>) {
  const call = fetch.mock.calls.map(([r]) => r).find((r) => r.method === 'POST')
  return call?.clone().json()
}

test('names the client and where the approval goes', async () => {
  stubApi({
    'GET /api/v1/oauth/authorization-request': { body: request },
    'GET /api/v1/cases': { body: directory },
  })
  renderAt(path, <ConsentPage />)
  expect(await screen.findByText('Claude')).toBeVisible()
  expect(screen.getByText(/then return to claude.ai/)).toBeVisible()
})

test('approving defaults to read-only on all cases and follows the redirect', async () => {
  const leaveTo = vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/oauth/authorization-request': { body: request },
    'GET /api/v1/cases': { body: directory },
    'POST /api/v1/oauth/authorize': {
      body: { redirect_url: 'https://claude.ai/cb?code=c0de&state=xyz' },
    },
  })
  renderAt(path, <ConsentPage />)
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Allow' }))
  await waitFor(() =>
    expect(leaveTo).toHaveBeenCalledWith('https://claude.ai/cb?code=c0de&state=xyz'),
  )
  expect(await postedJson(fetch)).toMatchObject({
    client_id: 'c1',
    redirect_uri: 'https://claude.ai/cb',
    code_challenge: 'abc',
    state: 'xyz',
    decision: 'approve',
    access: 'read',
    all_cases: true,
    case_ids: [],
  })
})

test('a per-case read & write grant sends the picked cases', async () => {
  vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/oauth/authorization-request': { body: request },
    'GET /api/v1/cases': { body: directory },
    'POST /api/v1/oauth/authorize': { body: { redirect_url: 'https://claude.ai/cb?code=c' } },
  })
  renderAt(path, <ConsentPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByLabelText('Only these cases'))
  expect(screen.getByRole('button', { name: 'Allow' })).toBeDisabled()
  await user.click(screen.getByLabelText(/ADV-024-A/))
  await user.click(screen.getByLabelText(/Read & write/))
  await user.click(screen.getByRole('button', { name: 'Allow' }))
  await waitFor(async () =>
    expect(await postedJson(fetch)).toMatchObject({
      access: 'read_write',
      all_cases: false,
      case_ids: ['ADV-024-A'],
    }),
  )
})

test('denying reports the refusal to the client', async () => {
  const leaveTo = vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/oauth/authorization-request': { body: request },
    'GET /api/v1/cases': { body: directory },
    'POST /api/v1/oauth/authorize': {
      body: { redirect_url: 'https://claude.ai/cb?error=access_denied&state=xyz' },
    },
  })
  renderAt(path, <ConsentPage />)
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Deny' }))
  await waitFor(() => expect(leaveTo).toHaveBeenCalled())
  expect(await postedJson(fetch)).toMatchObject({ decision: 'deny' })
})

test('refuses to grant anything while MCP is switched off', async () => {
  stubApi({
    'GET /api/v1/oauth/authorization-request': { body: { ...request, mcp_enabled: false } },
    'GET /api/v1/cases': { body: directory },
  })
  renderAt(path, <ConsentPage />)
  expect(await screen.findByText(/switched agent access off/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument()
})

test('an invalid request shows an error and offers no approval', async () => {
  stubApi({
    'GET /api/v1/oauth/authorization-request': {
      status: 422,
      body: { detail: 'redirect_uri is not registered.', code: 'invalid_authorization_request' },
    },
    'GET /api/v1/cases': { body: directory },
  })
  renderAt(path, <ConsentPage />)
  expect(await screen.findByText(/not valid/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Allow' })).not.toBeInTheDocument()
})
