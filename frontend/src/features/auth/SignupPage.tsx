import type { FormEvent } from 'react'
import { Link } from 'react-router'

import { useAuthConfig, useSignup } from '../../api/auth'
import { leaveTo } from '../../navigation'
import { Button } from '../../ui/Button'
import { TextField } from '../../ui/TextField'
import { AuthLayout } from './AuthLayout'

export function SignupPage() {
  const firstRun = useAuthConfig().data?.first_run ?? false
  const signup = useSignup()

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    signup.mutate(
      {
        display_name: String(form.get('display_name')),
        email: String(form.get('email')),
        password: String(form.get('password')),
        password_confirm: String(form.get('password_confirm')),
      },
      { onSuccess: (session) => leaveTo(session.next) },
    )
  }

  return (
    <AuthLayout
      subtitle={firstRun ? 'Create the administrator account' : 'Create your account'}
      error={signup.error?.message}
      footer={
        !firstRun && (
          <>
            Already have an account?{' '}
            <Link to="/login" className="font-medium text-accent hover:underline">
              Sign in
            </Link>
          </>
        )
      }
    >
      {firstRun && (
        <p className="mb-4 text-xs leading-relaxed text-muted">
          This is the first account on this Sanctuary. It will be the{' '}
          <span className="font-semibold text-ink">administrator</span>.
        </p>
      )}
      <form onSubmit={onSubmit} className="space-y-4">
        <TextField label="Name" name="display_name" autoComplete="name" />
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
          minLength={8}
          autoComplete="new-password"
          hint="At least 8 characters."
        />
        <TextField
          label="Confirm password"
          name="password_confirm"
          type="password"
          required
          minLength={8}
          autoComplete="new-password"
        />
        <Button type="submit" size="lg" className="w-full" disabled={signup.isPending}>
          {firstRun ? 'Create admin account' : 'Create account'}
        </Button>
      </form>
    </AuthLayout>
  )
}
