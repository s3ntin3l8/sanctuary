import type { FormEvent } from 'react'

import { useAccount, useChangeEmail, useChangePassword, useUpdateProfile } from '../../api/settings'
import { initials } from '../../format'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { SettingsCard } from '../../ui/SettingsCard'
import { TextField } from '../../ui/TextField'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

function fields(event: FormEvent<HTMLFormElement>) {
  event.preventDefault()
  return { form: event.currentTarget, data: new FormData(event.currentTarget) }
}

export function AccountPage() {
  const accountQuery = useAccount()
  const account = accountQuery.data
  const profile = useUpdateProfile()
  const email = useChangeEmail()
  const password = useChangePassword()
  const toast = useToast()

  if (!account) return <QueryState error={accountQuery.error} pending={accountQuery.isPending} />
  const name = account.display_name || account.email
  return (
    <>
      <SettingsCard title="Profile">
        <div className="flex items-center gap-3">
          <span className="flex h-11 w-11 items-center justify-center rounded-full bg-accent/20 font-display text-[14px] font-extrabold text-tealink">
            {initials(name)}
          </span>
          <div className="min-w-0">
            <div className="truncate text-[14px] font-semibold">{name}</div>
            <div className="truncate font-mono text-[11px] text-muted">{account.email}</div>
          </div>
          <Badge tone="accent" className="ml-auto">
            {account.role === 'admin' ? 'Owner' : 'User'}
          </Badge>
        </div>
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            const { data } = fields(e)
            profile.mutate(
              { display_name: String(data.get('display_name')) },
              {
                onSuccess: () => toast('Profile updated'),
                onError: (err) => toast(err.message, 'error'),
              },
            )
          }}
        >
          <div className="flex-1">
            <TextField
              label="Display name"
              name="display_name"
              defaultValue={account.display_name ?? ''}
              autoComplete="name"
            />
          </div>
          <Button type="submit" variant="secondary" disabled={profile.isPending}>
            Save
          </Button>
        </form>
      </SettingsCard>

      <SettingsCard
        title="Email"
        description="Used to sign in. Changing it keeps you signed in here."
      >
        <form
          className="space-y-3"
          onSubmit={(e) => {
            const { form, data } = fields(e)
            email.mutate(
              {
                new_email: String(data.get('new_email')),
                current_password: String(data.get('current_password') ?? ''),
              },
              {
                onSuccess: () => {
                  toast('Email updated')
                  form.reset()
                },
                onError: (err) => toast(err.message, 'error'),
              },
            )
          }}
        >
          <TextField
            label="New email"
            name="new_email"
            type="email"
            required
            defaultValue={account.email}
            autoComplete="username"
          />
          {account.has_password && (
            <TextField
              label="Current password"
              name="current_password"
              type="password"
              required
              autoComplete="current-password"
            />
          )}
          <Button type="submit" variant="secondary" disabled={email.isPending}>
            Update email
          </Button>
        </form>
      </SettingsCard>

      {account.has_password && (
        <SettingsCard
          title="Change password"
          description="Other sessions are signed out when the password changes."
        >
          <form
            className="space-y-3"
            onSubmit={(e) => {
              const { form, data } = fields(e)
              password.mutate(
                {
                  current_password: String(data.get('current_password')),
                  new_password: String(data.get('new_password')),
                },
                {
                  onSuccess: () => {
                    toast('Password updated — other sessions signed out')
                    form.reset()
                  },
                  onError: (err) => toast(err.message, 'error'),
                },
              )
            }}
          >
            <TextField
              label="Current password"
              name="current_password"
              type="password"
              required
              autoComplete="current-password"
            />
            <TextField
              label="New password"
              name="new_password"
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              hint="At least 8 characters."
            />
            <Button type="submit" variant="secondary" disabled={password.isPending}>
              Change password
            </Button>
          </form>
        </SettingsCard>
      )}
    </>
  )
}
