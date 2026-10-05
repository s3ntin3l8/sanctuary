import type { FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'

import { useAuthConfig, useLogin } from '../../api/auth'
import { leaveTo } from '../../navigation'
import { Button, buttonClass } from '../../ui/Button'
import { TextField } from '../../ui/TextField'
import { AuthLayout } from './AuthLayout'

/** Messages for the `?error=` codes the single sign-on callback redirects with. */
const SSO_ERRORS: Record<string, string> = {
  sso_failed: 'Single sign-on failed. Please try again.',
  sso_no_identity: 'Single sign-on returned no identity.',
  sso_no_account: 'No account is linked to this identity. Ask an administrator for access.',
}

export function LoginPage() {
  const [params] = useSearchParams()
  const config = useAuthConfig().data
  const login = useLogin()

  const ssoError = SSO_ERRORS[params.get('error') ?? ''] ?? null

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    login.mutate(
      {
        email: String(form.get('email')),
        password: String(form.get('password')),
        next: params.get('next') ?? '/',
      },
      { onSuccess: (session) => leaveTo(session.next) },
    )
  }

  return (
    <AuthLayout
      subtitle="Sign in to continue"
      error={login.error?.message ?? ssoError}
      footer={
        config?.signup_enabled && (
          <>
            No account?{' '}
            <Link to="/signup" className="font-medium text-accent hover:underline">
              Create one
            </Link>
          </>
        )
      }
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <TextField
          label="Email"
          name="email"
          type="email"
          required
          autoFocus
          autoComplete="username"
        />
        <TextField
          label="Password"
          name="password"
          type="password"
          required
          autoComplete="current-password"
        />
        <Button type="submit" className="w-full" disabled={login.isPending}>
          Sign in
        </Button>
      </form>
      {config?.oidc_enabled && (
        <>
          <div className="my-4 flex items-center gap-3">
            <div className="h-px flex-1 bg-line" />
            <span className="text-[10px] tracking-[.1em] text-muted uppercase">or</span>
            <div className="h-px flex-1 bg-line" />
          </div>
          <a href="/auth/oidc/login" className={buttonClass('secondary', 'w-full')}>
            Sign in with {config.oidc_provider_name}
          </a>
        </>
      )}
    </AuthLayout>
  )
}
