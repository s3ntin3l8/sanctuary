import type { Schemas } from '../../api/client'
import { formatIsoDate } from '../../format'
import { Icon } from '../../ui/Icon'

export type IndexedMessage = Schemas['GmailIndexedMessage']

/** One indexed message: pick box, date, sender, subject and its state markers. */
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
    <li className="grid grid-cols-[24px_88px_200px_1fr_20px_20px_20px] items-center gap-2 px-4 py-1.5">
      <input
        type="checkbox"
        aria-label={`Select ${m.subject ?? m.gmail_id}`}
        checked={selected}
        disabled={m.ingested}
        onChange={(e) => onToggle(m.gmail_id, (e.nativeEvent as MouseEvent).shiftKey === true)}
      />
      <span className="font-mono text-[11px] text-muted">{formatIsoDate(m.sent_at)}</span>
      <span className="truncate text-muted">{m.sender}</span>
      <span className="truncate">{m.subject ?? '(no subject)'}</span>
      {m.has_attachments ? <Icon name="attach_file" size={14} className="text-muted" /> : <span />}
      {m.cached ? (
        <span title="Cached locally — importing it needs no Gmail">
          <Icon name="save" size={14} className="text-muted" />
          <span className="sr-only">Cached locally</span>
        </span>
      ) : (
        <span />
      )}
      {m.ingested ? (
        <Icon name="check_circle" size={14} filled className="text-success" />
      ) : (
        <span />
      )}
    </li>
  )
}
