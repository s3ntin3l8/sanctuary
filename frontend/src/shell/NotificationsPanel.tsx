import { useState } from 'react'
import { Link } from 'react-router'

import type { Schemas } from '../api/client'
import { useNotifications } from '../api/shell'
import { formatDueRelative, formatShortDate } from '../format'
import { Icon } from '../ui/Icon'
import { Popover } from '../ui/Popover'
import { CountBadge, RailButton } from './RailButton'

type Kind = Schemas['NotificationGroup']['kind']

const GROUPS: Record<Kind, { title: string; icon: string; tone: string }> = {
  overdue_deadline: { title: 'Overdue', icon: 'error', tone: 'text-danger' },
  upcoming_deadline: { title: 'Due this week', icon: 'schedule', tone: 'text-warning' },
  hearing: { title: 'Hearings', icon: 'gavel', tone: 'text-accent' },
  pending_triage: { title: 'Awaiting triage', icon: 'inbox', tone: 'text-tealink' },
  overdue_cost: { title: 'Overdue costs', icon: 'payments', tone: 'text-danger' },
}

/** Rail control: the 🔔 badge and the popover listing what needs the user. */
export function NotificationsPanel() {
  const view = useNotifications().data
  const [open, setOpen] = useState(false)
  const total = view?.total ?? 0
  const groups = (view?.groups ?? []).filter((g) => g.count > 0)

  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      label="Notifications"
      className="max-h-[80vh] w-80 overflow-y-auto p-1.5"
      anchor={
        <RailButton
          icon="notifications"
          label="Notifications"
          active={open}
          onClick={() => setOpen((v) => !v)}
          badge={<CountBadge count={total} tone="warning" />}
        />
      }
    >
      <div className="flex items-center justify-between px-2.5 py-2">
        <span className="text-[13px] font-semibold">Needs you</span>
        <span className="font-mono text-[10px] text-muted">{total}</span>
      </div>
      {groups.length === 0 && (
        <div className="px-2.5 pt-1 pb-3 text-[12px] text-muted">Nothing needs you right now.</div>
      )}
      {groups.map((g) => (
        <section key={g.kind} aria-label={GROUPS[g.kind].title} className="border-t border-line2">
          <div className="flex items-center gap-1.5 px-2.5 pt-2 pb-1 text-[10px] font-bold tracking-[.11em] text-muted uppercase">
            <Icon name={GROUPS[g.kind].icon} size={13} className={GROUPS[g.kind].tone} />
            {GROUPS[g.kind].title}
            <span className="ml-auto font-mono">{g.count}</span>
          </div>
          {g.items.map((item) => (
            <Row key={`${g.kind}-${item.id}`} item={item} onClick={() => setOpen(false)} />
          ))}
          {g.count > g.items.length && (
            <div className="px-2.5 pb-1.5 text-[10px] text-muted">
              +{g.count - g.items.length} more
            </div>
          )}
        </section>
      ))}
    </Popover>
  )
}

function Row({ item, onClick }: { item: Schemas['NotificationItem']; onClick: () => void }) {
  const isDue = item.kind !== 'pending_triage'
  return (
    <Link
      to={item.link}
      onClick={onClick}
      className="flex items-start gap-2 rounded-lg px-2.5 py-1.5 hover:bg-accent/7"
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12px] text-ink">{item.title}</div>
        <div className="truncate text-[10.5px] text-muted">
          {item.case_id && <span className="font-mono">{item.case_id}</span>}
          {item.case_id && item.detail && ' · '}
          {item.detail}
        </div>
      </div>
      {item.due_at && (
        <div className="shrink-0 text-right">
          <div className="font-mono text-[10.5px] text-ink2">{formatShortDate(item.due_at)}</div>
          {isDue && <div className="text-[10px] text-muted">{formatDueRelative(item.due_at)}</div>}
        </div>
      )}
    </Link>
  )
}
