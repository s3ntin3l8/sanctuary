import { useState } from 'react'
import { Link } from 'react-router'

import {
  useCheckGmailNew,
  useDismissGmailNew,
  useGmailNew,
  useStartGmailImport,
} from '../../api/gmailImport'
import { formatIsoDate, pluralize } from '../../format'
import { Button } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Icon } from '../../ui/Icon'
import { useToast } from '../../ui/toast'
import { IMPORT_PATH } from './GmailImportStatus'
import { MessageRow } from './MessageRow'

/** The new mail worth announcing: only for people who confirm imports themselves. */
export function useNewMailToReview() {
  const data = useGmailNew().data
  return data && data.count > 0 && data.sync_mode !== 'auto' ? data : null
}

/** One line for pages other than Import: "3 new messages from Gmail — Review". */
export function NewMailNotice() {
  const news = useNewMailToReview()
  if (!news) return null
  return (
    <div className="flex items-center gap-2 rounded-xl border border-accent/30 bg-accent/5 px-3 py-2 text-[12px]">
      <Icon name="mark_email_unread" size={14} className="shrink-0 text-tealink" />
      <div className="min-w-0 flex-1 truncate">
        <strong>{pluralize(news.count, 'new message')}</strong> from Gmail awaiting your decision
      </div>
      <Link
        to={IMPORT_PATH}
        className="shrink-0 text-[11px] font-semibold text-tealink hover:underline"
      >
        Review
      </Link>
    </div>
  )
}

/**
 * The Import page's banner for new mail: review it, import it (all or a pick),
 * or skip it. Nothing is imported until you choose.
 */
export function NewMailBanner({ busy }: { busy: boolean }) {
  const news = useNewMailToReview()
  const check = useCheckGmailNew()
  const start = useStartGmailImport()
  const dismiss = useDismissGmailNew()
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirmSkip, setConfirmSkip] = useState(false)
  if (!news) return null

  function toggle(gmailId: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (!next.delete(gmailId)) next.add(gmailId)
      return next
    })
  }

  function runImport(body: { gmail_ids?: string[] }) {
    start.mutate(
      { new: true, ...body },
      {
        onSuccess: (r) => {
          setSelected(new Set())
          toast(
            r.queued === 0 ? 'Nothing left to import' : `Queued ${pluralize(r.queued, 'message')}`,
          )
        },
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }

  return (
    <section
      aria-label="New mail"
      className="mb-4 rounded-xl border border-accent/30 bg-accent/5 text-[12px]"
    >
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <Icon name="mark_email_unread" size={16} className="text-tealink" />
        <div className="min-w-0 flex-1">
          <strong>{pluralize(news.count, 'new message')}</strong>
          {news.since && <span className="text-muted"> since {formatIsoDate(news.since)}</span>}
          {news.checked_at && (
            <span className="text-muted"> · checked {formatIsoDate(news.checked_at)}</span>
          )}
        </div>
        <button
          type="button"
          className="text-[11px] text-muted underline-offset-2 hover:underline"
          disabled={check.isPending}
          onClick={() =>
            check.mutate(undefined, {
              onSuccess: () => toast('Checking for new mail'),
              onError: (err) => toast(err.message, 'error'),
            })
          }
        >
          Check now
        </button>
        <Button variant="secondary" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide' : 'Review'}
        </Button>
        <Button disabled={busy || start.isPending} onClick={() => runImport({})}>
          Import all
        </Button>
        <Button
          variant="secondary"
          disabled={dismiss.isPending}
          onClick={() => setConfirmSkip(true)}
        >
          Skip
        </Button>
      </div>

      {open && (
        <div className="border-t border-line2">
          <ul>
            {news.items.map((m) => (
              <MessageRow
                key={m.gmail_id}
                message={m}
                selected={selected.has(m.gmail_id)}
                onToggle={toggle}
              />
            ))}
          </ul>
          <div className="flex items-center gap-3 px-4 py-2">
            {news.count > news.items.length && (
              <span className="text-muted">
                Showing the oldest {news.items.length} of {news.count}.
              </span>
            )}
            <Button
              className="ml-auto"
              disabled={busy || start.isPending || selected.size === 0}
              onClick={() => runImport({ gmail_ids: [...selected] })}
            >
              Import selected ({selected.size})
            </Button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmSkip}
        onClose={() => setConfirmSkip(false)}
        onConfirm={() => {
          setConfirmSkip(false)
          dismiss.mutate(undefined, {
            onSuccess: () => toast('New mail skipped'),
            onError: (err) => toast(err.message, 'error'),
          })
        }}
        title="Skip these messages?"
        body={`Moves the sync point past the ${pluralize(news.count, 'new message')}. Nothing is imported or deleted, and they stay available under Import history. Mail that arrives later is still offered.`}
        label="Skip"
        pending={dismiss.isPending}
      />
    </section>
  )
}
