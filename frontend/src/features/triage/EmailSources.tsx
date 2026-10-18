import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { api, type Schemas, unwrap } from '../../api/client'
import { formatShortDate } from '../../time'
import { Button } from '../../ui/Button'

const labels: Record<string, string> = {
  mailbox_arrival_unavailable: 'Mailbox arrival is unavailable; the date is a fallback.',
  ambiguous_manifest_filename: 'An attachment name matches more than one file.',
  unmatched_manifest_entry: 'A listed attachment could not be matched.',
  ambiguous_manifest_history: 'Current and quoted transmissions give conflicting groups.',
  invalid_manifest_timestamp: 'A transmission date needs review.',
  unresolved_source_group: 'Some documents need manual grouping.',
  attachment_empty: 'An attachment contains no readable bytes.',
  attachment_unsupported: 'An attachment uses an unsupported format.',
  attachment_failed: 'An attached email could not be read.',
  attachment_encrypted: 'An encrypted or opaque message part cannot be read.',
}

export function EmailSources({ batchId }: { batchId: number }) {
  const [open, setOpen] = useState(false)
  const client = useQueryClient()
  const queryKey = ['email-sources', batchId]
  const query = useQuery({
    queryKey,
    enabled: open,
    queryFn: () =>
      unwrap(
        api.GET('/api/v1/triage/bundles/{batch_id}/sources', {
          params: { path: { batch_id: batchId } },
        }),
      ),
  })
  const acknowledge = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST('/api/v1/triage/bundles/{batch_id}/sources/acknowledge', {
          params: { path: { batch_id: batchId } },
        }),
      ),
    onSuccess: () => {
      client.invalidateQueries({ queryKey })
      client.invalidateQueries({ queryKey: ['triage'] })
      client.invalidateQueries({ queryKey: ['gmail'] })
    },
  })
  const data = query.data
  return (
    <section className="mt-3 border-t border-line2 pt-2 text-[11px]">
      <button
        type="button"
        className="text-accent"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? 'Hide email sources' : 'Email sources and attachments'}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {query.isPending && <p>Loading sources…</p>}
          {query.error && <p role="alert">{query.error.message}</p>}
          {data && (
            <>
              <p>
                {data.received_source === 'gmail'
                  ? 'Mailbox arrival'
                  : data.received_source === 'email_date'
                    ? 'Email sent date · arrival unknown'
                    : data.received_source === 'ingest_time'
                      ? 'Ingest date · arrival unknown'
                      : 'Legacy date · origin unknown'}{' '}
                · {formatShortDate(data.received_at)}
              </p>
              {(data.review_reasons ?? []).includes('legacy_source_identity') && (
                <p className="text-warning">
                  Automatic grouping is paused until this email is reimported. Existing groups are
                  preserved; acknowledging source issues does not resume grouping.
                </p>
              )}
              {!data.acknowledged && (data.review_reasons ?? []).length > 0 && (
                <div className="space-y-1 text-warning">
                  {(data.review_reasons ?? [])
                    .filter((reason) => reason !== 'legacy_source_identity')
                    .map((reason) => (
                      <p key={reason}>{labels[reason] ?? reason.replaceAll('_', ' ')}</p>
                    ))}
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={acknowledge.isPending}
                    onClick={() => acknowledge.mutate()}
                  >
                    Acknowledge source issues
                  </Button>
                </div>
              )}
              {acknowledge.error && <p role="alert">{acknowledge.error.message}</p>}
              <p className="text-muted">
                Transport sources are separate from document enclosure groups.
              </p>
              <SourceTree batchId={batchId} sources={data.sources ?? []} parentId={null} />
            </>
          )}
        </div>
      )}
    </section>
  )
}

function SourceTree({
  batchId,
  sources,
  parentId,
}: {
  batchId: number
  sources: Schemas['EmailSource'][]
  parentId: string | null
}) {
  return (
    <ul className="space-y-2 border-l border-line2 pl-2">
      {sources
        .filter((s) => (s.parent_id ?? null) === parentId)
        .map((source) => (
          <li key={source.id}>
            <p className="font-semibold">{source.subject || 'Email without subject'}</p>
            <p>{source.sender}</p>
            {(['to', 'cc', 'bcc', 'reply_to'] as const).map((key) =>
              source[key]?.length ? (
                <p key={key} className="break-words text-muted">
                  {key === 'reply_to' ? 'Reply to' : key.toUpperCase()}: {source[key]?.join(', ')}
                </p>
              ) : null,
            )}
            {source.sent_at && (
              <p className="text-muted">Sent · {formatShortDate(source.sent_at)}</p>
            )}
            {source.email_note && (
              <details>
                <summary className="cursor-pointer text-muted">Forwarding context</summary>
                <p className="whitespace-pre-wrap break-words">{source.email_note}</p>
              </details>
            )}
            {(source.attachment_manifest ?? []).map((entry, index) => (
              <p key={index} className="text-muted">
                Transmission · {entry.raw_timestamp ?? entry.timestamp ?? 'Unknown date'} ·{' '}
                {entry.source_label}
                {entry.issue ? ' · needs review' : ''}
              </p>
            ))}
            <ul className="my-1 space-y-1">
              {(source.parts ?? []).map((part) => (
                <li key={part.id} className="break-words">
                  <span>
                    {part.filename ?? 'Unnamed attachment'} · {part.outcome.replaceAll('_', ' ')}
                  </span>
                  {part.reason && <p className="text-muted">{part.reason}</p>}
                  {part.id.endsWith('.body') ? null : (
                    <a
                      className="ml-2 text-accent"
                      href={`/api/v1/triage/bundles/${batchId}/sources/${encodeURIComponent(source.id)}/parts/${encodeURIComponent(part.id)}`}
                      download
                    >
                      Download original
                    </a>
                  )}
                </li>
              ))}
            </ul>
            <SourceTree batchId={batchId} sources={sources} parentId={source.id} />
          </li>
        ))}
    </ul>
  )
}
