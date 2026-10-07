import { useState } from 'react'
import { Link } from 'react-router'

import { useLogout, useShell } from '../api/shell'
import { initials } from '../format'
import { leaveTo } from '../navigation'
import { Icon } from '../ui/Icon'
import { Popover } from '../ui/Popover'

export function ProfileMenu() {
  const shell = useShell().data
  const logout = useLogout()
  const [open, setOpen] = useState(false)
  const name = shell?.user.display_name || shell?.user.email || ''
  const isAdmin = shell?.user.role === 'admin'

  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      role="menu"
      label="Account"
      className="w-60 p-1.5"
      anchor={
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-label="Account menu"
          aria-expanded={open}
          className="flex h-10 w-10 items-center justify-center rounded-[11px] hover:bg-accent/7"
        >
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-accent/20 font-display text-[11px] font-extrabold text-tealink">
            {initials(name) || '·'}
          </span>
        </button>
      }
    >
      <div className="px-2.5 py-2">
        <div className="truncate text-[13px] font-semibold">{name}</div>
        <div className="truncate font-mono text-[10px] text-muted">
          {shell?.user.email}
          {isAdmin && ' · admin'}
        </div>
      </div>
      <MenuLink
        href="/settings/account"
        icon="settings"
        label="Settings"
        onClick={() => setOpen(false)}
      />
      {isAdmin && (
        <MenuLink
          href="/admin/users"
          icon="group"
          label="Manage users"
          onClick={() => setOpen(false)}
        />
      )}
      <div className="mx-2.5 my-1 flex items-center gap-1.5 border-t border-line2 pt-2 text-[10px] text-muted">
        <Icon name="lock" size={12} /> Local · nothing leaves this device
      </div>
      <button
        type="button"
        role="menuitem"
        onClick={() => logout.mutate(undefined, { onSuccess: () => leaveTo('/login') })}
        className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[12px] text-danger hover:bg-danger/10"
      >
        <Icon name="logout" size={16} /> Sign out
      </button>
    </Popover>
  )
}

function MenuLink({
  href,
  icon,
  label,
  onClick,
}: {
  href: string
  icon: string
  label: string
  onClick: () => void
}) {
  return (
    <Link
      to={href}
      role="menuitem"
      onClick={onClick}
      className="flex items-center gap-2 rounded-lg px-2.5 py-2 text-[12px] text-ink2 hover:bg-accent/7 hover:text-ink"
    >
      <Icon name={icon} size={16} /> {label}
    </Link>
  )
}
