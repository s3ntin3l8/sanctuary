import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'

import * as navigation from '../../navigation'
import { renderAt, stubApi } from '../../test/render'
import { SignupPage } from './SignupPage'

const config = {
  first_run: false,
  signup_enabled: true,
  oidc_enabled: false,
  oidc_provider_name: 'authentik',
}

test('first run: creates the administrator and leaves for the app', async () => {
  const leaveTo = vi.spyOn(navigation, 'leaveTo').mockImplementation(() => {})
  const fetch = stubApi({
    'GET /api/v1/auth/config': { body: { ...config, first_run: true } },
    'POST /api/v1/auth/signup': { body: { next: '/' } },
  })
  renderAt('/signup', <SignupPage />)
  const user = userEvent.setup()

  const submit = await screen.findByRole('button', { name: 'Create admin account' })
  expect(screen.queryByRole('link', { name: 'Sign in' })).not.toBeInTheDocument()
  await user.type(screen.getByLabelText('Name'), 'Katharina Vogt')
  await user.type(screen.getByLabelText('Email'), 'boss@example.com')
  await user.type(screen.getByLabelText('Password'), 'password123')
  await user.type(screen.getByLabelText('Confirm password'), 'password123')
  await user.click(submit)

  await waitFor(() => expect(leaveTo).toHaveBeenCalledWith('/'))
  const signup = fetch.mock.calls.map(([request]) => request).find((r) => r.method === 'POST')
  expect(await signup?.json()).toEqual({
    display_name: 'Katharina Vogt',
    email: 'boss@example.com',
    password: 'password123', // pragma: allowlist secret
    password_confirm: 'password123', // pragma: allowlist secret
  })
})

test('later sign-ups: shows the server validation message and a way back to sign-in', async () => {
  stubApi({
    'GET /api/v1/auth/config': { body: config },
    'POST /api/v1/auth/signup': {
      status: 422,
      body: { detail: 'Passwords do not match.', code: 'password_mismatch' },
    },
  })
  renderAt('/signup', <SignupPage />)
  const user = userEvent.setup()

  await user.type(screen.getByLabelText('Email'), 'new@example.com')
  await user.type(screen.getByLabelText('Password'), 'password123')
  await user.type(screen.getByLabelText('Confirm password'), 'password124')
  await user.click(screen.getByRole('button', { name: 'Create account' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Passwords do not match.')
  expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login')
})
