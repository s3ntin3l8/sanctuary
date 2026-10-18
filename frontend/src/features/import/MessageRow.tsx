import type { Schemas } from '../../api/client'
import { formatIsoDate } from '../../time'
import { Icon } from '../../ui/Icon'

export type IndexedMessage = Schemas['GmailIndexedMessage']

/** Leading state marker: imported (filled), cached locally (ring), otherwise empty. */
function StateDot({ message: m }: { message: IndexedMessage }) {
  if (m.ingested && (m.source_review_reasons ?? []).length > 0) {
    return (
      <span title="Imported · source issues need review">
        <Icon name="warning" size={14} className="text-warning" />
        <span className="sr-only">Source issues need review</span>
      </span>
    )
  }
  if (m.ingested) {
    return (
      <span
        title={m.cached ? 'Imported · cached locally' : 'Imported'}
        className="flex justify-center"
      >
        <Icon name="check_circle" size={14} filled className="text-success" />
        <span className="sr-only">Imported</span>
        {m.cached && <span className="sr-only">Cached locally</span>}
      </span>
    )
  }
  if (m.cached) {
    return (
      <span title="Cached locally — importing it needs no Gmail" className="flex justify-center">
        <span className="size-2 rounded-full border border-muted" />
        <span className="sr-only">Cached locally</span>
      </span>
    )
  }
  return <span />
}

/** One indexed message: pick box, state, date, sender, subject and attachment marker. */
export function MessageRow({
  message: m,
  selected,
  onToggle,
}: {
  message: IndexedMessage
  selected: boolean
  /** `range` is true for a shift-click: extend from the last row clicked. */
  onToggle: (gmailId: string, range: boolean) => void
}) {
  return (
    <li className="grid grid-cols-[24px_16px_88px_200px_1fr_20px] items-center gap-2 px-4 py-1.5">
      <input
        type="checkbox"
        aria-label={`Select ${m.subject ?? m.gmail_id}`}
        checked={selected}
        disabled={m.ingested}
        onChange={(e) => onToggle(m.gmail_id, (e.nativeEvent as MouseEvent).shiftKey === true)}
      />
      <StateDot message={m} />
      <span className="font-mono text-[11px] text-muted">{formatIsoDate(m.sent_at)}</span>
      <span className="flex min-w-0 items-center gap-1 text-muted">
        {m.outgoing && (
          <span title="Sent by you">
            <Icon name="north_east" size={13} />
            <span className="sr-only">Sent by you</span>
          </span>
        )}
        <span className="truncate">{m.sender}</span>
      </span>
      <span className="truncate">{m.subject ?? '(no subject)'}</span>
      {m.has_attachments ? <Icon name="attach_file" size={14} className="text-muted" /> : <span />}
    </li>
  )
}
