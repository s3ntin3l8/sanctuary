import type { ReactNode } from 'react'
import { NavLink } from 'react-router'

import { Icon } from '../ui/Icon'

type Props = {
  icon: string
  label: string
  to?: string
  legacy?: boolean
  active?: boolean
  onClick?: () => void
  badge?: ReactNode
}

const base =
  'relative flex h-10 w-10 items-center justify-center rounded-[11px] text-muted transition-colors hover:bg-accent/7 hover:text-ink'

/**
 * One 40×40 rail control. `to` renders a link (`legacy` forces a full page
 * load for views still served by the server), otherwise a button.
 */
export function RailButton({ icon, label, to, legacy, active, onClick, badge }: Props) {
  const content = (
    <>
      <Icon name={icon} size={22} filled={active} />
      {badge}
    </>
  )
  const activeClass = 'bg-accent/10 text-accent'
  if (to && legacy) {
    return (
      <a href={to} aria-label={label} title={label} className={base}>
        {content}
      </a>
    )
  }
  if (to) {
    return (
      <NavLink
        to={to}
        end={to === '/'}
        aria-label={label}
        title={label}
        className={({ isActive }) => `${base} ${isActive ? activeClass : ''}`}
      >
        {content}
      </NavLink>
    )
  }
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className={`${base} ${active ? activeClass : ''}`}
    >
      {content}
    </button>
  )
}

export function CountBadge({ count, tone }: { count: number; tone: 'danger' | 'warning' }) {
  if (count <= 0) return null
  const colors = tone === 'danger' ? 'bg-[#ffb4ab] text-[#690005]' : 'bg-warning text-[#3b2a00]'
  return (
    <span
      className={`absolute -top-0.5 -right-0.5 min-w-4 rounded-full px-1 text-center font-mono text-[9px] font-bold leading-4 ${colors}`}
    >
      {count > 99 ? '99+' : count}
    </span>
  )
}
