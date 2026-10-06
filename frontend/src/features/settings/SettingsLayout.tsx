import { useEffect } from 'react'
import { NavLink, Outlet } from 'react-router'

import { useShell } from '../../api/shell'
import { Icon } from '../../ui/Icon'

type Item = { to: string; icon: string; label: string; admin?: boolean }
type Group = { label: string; items: Item[] }

const GROUPS: Group[] = [
  {
    label: 'Workspace',
    items: [
      { to: '/settings/account', icon: 'manage_accounts', label: 'Account' },
      { to: '/settings/appearance', icon: 'palette', label: 'Appearance' },
    ],
  },
  {
    label: 'Intelligence',
    items: [
      { to: '/settings/ai', icon: 'model_training', label: 'AI & Models', admin: true },
      { to: '/settings/identity', icon: 'groups', label: 'Identity & Context', admin: true },
    ],
  },
  { label: 'Gmail', items: [{ to: '/settings/gmail', icon: 'mail', label: 'Gmail' }] },
  {
    label: 'Data',
    items: [
      { to: '/settings/data', icon: 'database', label: 'Data', admin: true },
      { to: '/settings/export', icon: 'download', label: 'Export', admin: true },
    ],
  },
  { label: 'Admin', items: [{ to: '/admin/users', icon: 'group', label: 'Users', admin: true }] },
]

export function SettingsLayout() {
  const isAdmin = useShell().data?.user.role === 'admin'
  useEffect(() => {
    document.title = 'Settings | The Sanctuary'
  }, [])
  const groups = GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => isAdmin || !i.admin),
  })).filter((g) => g.items.length > 0)
  return (
    <div className="flex min-h-full flex-col">
      <header className="flex items-center gap-4 border-b border-line bg-panel px-6 py-4">
        <div>
          <h1 className="font-display text-[22px] font-extrabold tracking-tight">Settings</h1>
          <p className="flex items-center gap-1 font-mono text-[11px] text-muted">
            <Icon name="lock" size={12} /> local · nothing leaves this device
          </p>
        </div>
      </header>
      <div className="flex flex-1 gap-6 px-6 py-5">
        <nav aria-label="Settings" className="w-48 shrink-0 space-y-4">
          {groups.map((g) => (
            <div key={g.label}>
              <div className="mb-1 px-2 text-[9px] font-extrabold tracking-[.14em] text-muted uppercase">
                {g.label}
              </div>
              {g.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `flex items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] ${
                      isActive
                        ? 'bg-accent/10 font-semibold text-ink'
                        : 'text-muted hover:bg-accent/5 hover:text-ink'
                    }`
                  }
                >
                  <Icon name={item.icon} size={16} /> {item.label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="min-w-0 max-w-[720px] flex-1 space-y-4">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
