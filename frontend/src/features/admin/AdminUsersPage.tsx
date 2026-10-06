import { type FormEvent, useEffect, useState } from 'react'

import type { Schemas } from '../../api/client'
import {
  useAdminCreateUser,
  useAdminDeleteUser,
  useAdminReassign,
  useAdminResetPassword,
  useAdminSetRole,
  useAdminSignup,
  useAdminToggleActive,
  useAdminUsers,
} from '../../api/settings'
import { useShell } from '../../api/shell'
import { formatIsoDate } from '../../format'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Field, inputClass } from '../../ui/Field'
import { SettingsCard } from '../../ui/SettingsCard'
import { TextField } from '../../ui/TextField'
import { Toggle } from '../../ui/Toggle'
import { useToast } from '../../ui/toast'

type User = Schemas['AdminUser']

export function AdminUsersPage() {
  const view = useAdminUsers().data
  const me = useShell().data?.user
  const create = useAdminCreateUser()
  const signup = useAdminSignup()
  const toast = useToast()

  useEffect(() => {
    document.title = 'Users | The Sanctuary'
  }, [])
  if (!view || !me) return null

  function onCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    create.mutate(
      {
        email: String(data.get('email')),
        password: String(data.get('password')),
        role: data.get('role') === 'admin' ? 'admin' : 'user',
      },
      {
        onSuccess: () => {
          toast('User created')
          form.reset()
        },
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }

  return (
    <>
      <SettingsCard
        title="Self-service sign-up"
        description="When on, anyone who can reach this Sanctuary may create a regular account."
      >
        <div className="flex items-center gap-3 text-[12px]">
          <span className="flex-1">Sign-up is {view.signup_enabled ? 'enabled' : 'disabled'}</span>
          <Toggle
            checked={view.signup_enabled}
            label="Self-service sign-up"
            disabled={signup.isPending}
            onChange={(next) => signup.mutate(next)}
          />
        </div>
      </SettingsCard>

      <SettingsCard title="Create user">
        <form className="grid grid-cols-[2fr_2fr_1fr_auto] items-end gap-2" onSubmit={onCreate}>
          <TextField label="Email" name="email" type="email" required autoComplete="off" />
          <TextField
            label="Password"
            name="password"
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
          />
          <Field label="Role" htmlFor="new-role">
            <select id="new-role" name="role" defaultValue="user" className={inputClass}>
              <option value="user">User</option>
              <option value="admin">Admin</option>
            </select>
          </Field>
          <Button type="submit" disabled={create.isPending}>
            Create
          </Button>
        </form>
      </SettingsCard>

      <SettingsCard title={`Users (${view.users.length})`}>
        <ul className="divide-y divide-line2">
          {view.users.map((u) => (
            <UserRow
              key={u.id}
              user={u}
              isSelf={u.id === me.id}
              others={view.users.filter((o) => o.id !== u.id)}
            />
          ))}
        </ul>
      </SettingsCard>
    </>
  )
}

function UserRow({ user, isSelf, others }: { user: User; isSelf: boolean; others: User[] }) {
  const toggle = useAdminToggleActive()
  const setRole = useAdminSetRole()
  const reset = useAdminResetPassword()
  const remove = useAdminDeleteUser()
  const reassign = useAdminReassign()
  const toast = useToast()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [newOwner, setNewOwner] = useState<number | ''>('')
  const fail = (err: { message: string }) => toast(err.message, 'error')
  return (
    <li className="py-3">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[13px] font-semibold">
            {user.display_name || user.email}
            <Badge tone={user.role === 'admin' ? 'accent' : 'neutral'}>{user.role}</Badge>
            <Badge tone={user.is_active ? 'success' : 'danger'}>
              {user.is_active ? 'active' : 'inactive'}
            </Badge>
            {isSelf && <Badge>you</Badge>}
          </div>
          <div className="font-mono text-[10.5px] text-muted">
            {user.email} · created {formatIsoDate(user.created_at)}
            {user.last_login_at && ` · last sign-in ${formatIsoDate(user.last_login_at)}`}
            {user.owned_case_count > 0 &&
              ` · owns ${user.owned_case_count} case${user.owned_case_count === 1 ? '' : 's'}`}
          </div>
        </div>
        {!isSelf && (
          <>
            <Button
              variant="secondary"
              className="px-2.5 py-1 text-[11px]"
              disabled={toggle.isPending}
              onClick={() => toggle.mutate(user.id, { onError: fail })}
            >
              {user.is_active ? 'Deactivate' : 'Activate'}
            </Button>
            <Button
              variant="secondary"
              className="px-2.5 py-1 text-[11px]"
              disabled={setRole.isPending}
              onClick={() =>
                setRole.mutate(
                  { id: user.id, role: user.role === 'admin' ? 'user' : 'admin' },
                  { onError: fail },
                )
              }
            >
              {user.role === 'admin' ? 'Make user' : 'Make admin'}
            </Button>
            <Button
              variant="secondary"
              className="border-danger/40 px-2.5 py-1 text-[11px] text-danger"
              onClick={() => setConfirmDelete(true)}
            >
              Delete
            </Button>
          </>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <form
          className="flex items-end gap-1"
          onSubmit={(e) => {
            e.preventDefault()
            const form = e.currentTarget
            const password = String(new FormData(form).get('new_password'))
            reset.mutate(
              { id: user.id, password },
              {
                onSuccess: () => {
                  toast("Password reset — that user's sessions were signed out")
                  form.reset()
                },
                onError: fail,
              },
            )
          }}
        >
          <TextField
            label="Reset password"
            name="new_password"
            type="password"
            minLength={8}
            required
            autoComplete="new-password"
            placeholder="new password"
          />
          <Button
            type="submit"
            variant="secondary"
            className="px-2.5 py-2 text-[11px]"
            disabled={reset.isPending}
          >
            Reset
          </Button>
        </form>
        {user.owned_case_count > 0 && others.length > 0 && (
          <div className="flex items-end gap-1">
            <Field label="Reassign cases to" htmlFor={`reassign-${user.id}`}>
              <select
                id={`reassign-${user.id}`}
                value={newOwner}
                onChange={(e) => setNewOwner(e.target.value ? Number(e.target.value) : '')}
                className={inputClass}
              >
                <option value="">— choose —</option>
                {others.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.display_name || o.email}
                  </option>
                ))}
              </select>
            </Field>
            <Button
              variant="secondary"
              className="px-2.5 py-2 text-[11px]"
              disabled={newOwner === '' || reassign.isPending}
              onClick={() =>
                newOwner !== '' &&
                reassign.mutate(
                  { id: user.id, newOwnerId: newOwner },
                  { onSuccess: () => toast('Cases reassigned'), onError: fail },
                )
              }
            >
              Reassign
            </Button>
          </div>
        )}
      </div>
      <ConfirmDialog
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        onConfirm={() => {
          setConfirmDelete(false)
          remove.mutate(user.id, { onSuccess: () => toast('User deleted'), onError: fail })
        }}
        title={`Delete ${user.email}?`}
        body="This cannot be undone. Users who own cases must have them reassigned first."
        label="Delete"
        danger
      />
    </li>
  )
}
