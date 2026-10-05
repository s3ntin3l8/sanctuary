import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import * as navigation from '../../navigation'
import { renderAt, stubApi } from '../../test/render'
import { LoginPage } from './LoginPage'

const config = {
  first_run: false,
  signup_enabled: false,
  oidc_enabled: false,
  oidc_provider_name: 'authentik',
}

async function signIn() {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Email'), 'u@example.com')
  await user.type(screen.getByLabelText('Password'), 'password123')
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

test('signs in and leaves for the path the server returns', async () => {
  const leaveTo = vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/auth/config': { body: config },
    'POST /api/v1/auth/login': { body: { next: '/cases/ADV-024-A' } },
  })
  renderAt('/login?next=/cases/ADV-024-A', <LoginPage />)

  await signIn()

  await waitFor(() => expect(leaveTo).toHaveBeenCalledWith('/cases/ADV-024-A'))
  const login = fetch.mock.calls.map(([request]) => request).find((r) => r.method === 'POST')
  expect(await login?.json()).toEqual({
    email: 'u@example.com',
    password: 'password123', // pragma: allowlist secret
    next: '/cases/ADV-024-A',
  })
})

test('shows the server message when the credentials are rejected', async () => {
  const leaveTo = vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  stubApi({
    'GET /api/v1/auth/config': { body: config },
    'POST /api/v1/auth/login': {
      status: 401,
      body: { detail: 'Invalid email or password.', code: 'invalid_credentials' },
    },
  })
  renderAt('/login', <LoginPage />)

  await signIn()

  expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.')
  expect(leaveTo).not.toHaveBeenCalled()
})

test('offers single sign-on and sign-up only when the server enables them', async () => {
  stubApi({
    'GET /api/v1/auth/config': {
      body: { ...config, signup_enabled: true, oidc_enabled: true },
    },
  })
  renderAt('/login', <LoginPage />)

  expect(await screen.findByRole('link', { name: 'Sign in with authentik' })).toHaveAttribute(
    'href',
    '/auth/oidc/login',
  )
  expect(screen.getByRole('link', { name: 'Create one' })).toHaveAttribute('href', '/signup')
})

test('hides single sign-on and sign-up by default', async () => {
  const fetch = stubApi({ 'GET /api/v1/auth/config': { body: config } })
  renderAt('/login', <LoginPage />)

  await waitFor(() => expect(fetch).toHaveBeenCalled())
  expect(screen.queryByRole('link', { name: /Sign in with/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Create one' })).not.toBeInTheDocument()
})

test('explains a failed single sign-on redirect without echoing the query string', () => {
  stubApi({ 'GET /api/v1/auth/config': { body: config } })
  renderAt('/login?error=sso_no_account', <LoginPage />)
  expect(screen.getByRole('alert')).toHaveTextContent('No account is linked to this identity.')
})

test('ignores unknown error codes', () => {
  stubApi({ 'GET /api/v1/auth/config': { body: config } })
  renderAt('/login?error=<script>alert(1)</script>', <LoginPage />)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
