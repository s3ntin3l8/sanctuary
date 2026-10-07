import { type FormEvent, useState } from 'react'
import { Link } from 'react-router'

import {
  useDisconnectGmail,
  useGmail,
  useGmailSyncNow,
  useResetGmailSync,
  useSaveGmailFilters,
  useSetGmailAutoSync,
} from '../../api/settings'
import { formatIsoDate } from '../../format'
import { Alert } from '../../ui/Alert'
import { Badge } from '../../ui/Badge'
import { Button, buttonClass } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Field, inputClass } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { SettingsCard } from '../../ui/SettingsCard'
import { TextField } from '../../ui/TextField'
import { Toggle } from '../../ui/Toggle'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

export function GmailPage() {
  const gmailQuery = useGmail()
  const gmail = gmailQuery.data
  const save = useSaveGmailFilters()
  const autoSync = useSetGmailAutoSync()
  const syncNow = useGmailSyncNow()
  const resetSync = useResetGmailSync()
  const disconnect = useDisconnectGmail()
  const toast = useToast()
  const [since, setSince] = useState('')
  const [confirm, setConfirm] = useState<'disconnect' | 'reset' | null>(null)

  if (!gmail) return <QueryState error={gmailQuery.error} pending={gmailQuery.isPending} />

  function onSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    save.mutate(
      {
        allowlist: String(data.get('allowlist'))
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        label_filter: String(data.get('label_filter')),
      },
      {
        onSuccess: () => toast('Gmail configuration saved'),
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }

  return (
    <>
      <SettingsCard title="Connection">
        <div className="flex items-center gap-3">
          <Icon name="mail" size={22} className={gmail.connected ? 'text-success' : 'text-muted'} />
          <div className="flex-1">
            <div className="text-[13px] font-semibold">
              {gmail.connected ? 'Gmail connected' : 'No Gmail connected'}
            </div>
            <div className="font-mono text-[11px] text-muted">
              {gmail.connected &&
                gmail.connected_at &&
                `Connected on ${formatIsoDate(gmail.connected_at)}`}
              {gmail.last_sync_at && ` · synced up to ${formatIsoDate(gmail.last_sync_at)}`}
            </div>
          </div>
          <a href={gmail.oauth_start_url} className={buttonClass('secondary')}>
            {gmail.connected ? 'Reconnect' : 'Connect Gmail'}
          </a>
          {gmail.connected && (
            <Button variant="secondary" onClick={() => setConfirm('disconnect')}>
              Disconnect
            </Button>
          )}
        </div>
        {gmail.reconnect_required && (
          <Alert>
            Reconnect required — {gmail.last_sync_error ?? 'the stored Gmail grant is unusable.'}
          </Alert>
        )}
        {gmail.ai_external && (
          <div
            role="alert"
            className="rounded-[9px] border border-warning/40 bg-warning/10 px-3 py-2 text-xs font-medium text-warning"
          >
            An active AI endpoint is on the public internet — imported mail would leave this
            machine. Switch to a local endpoint under AI &amp; Models first.
          </div>
        )}
        <p className="flex items-center gap-1 text-[11px] text-muted">
          <Icon name="lock" size={12} /> Read-only access — Sanctuary never changes, labels or
          deletes anything in your mailbox. Credentials are stored encrypted.
        </p>
      </SettingsCard>

      <SettingsCard
        title="Sync"
        description="Sync fetches mail received since the last sync point. Older mail is imported deliberately via Import history."
      >
        <div className="flex items-center gap-3">
          <div className="flex-1">
            <div className="text-[13px] font-semibold">Automatic sync</div>
            <div className="text-[11px] text-muted">Poll every 5 minutes. Off by default.</div>
          </div>
          <Toggle
            label="Automatic sync"
            checked={gmail.auto_sync}
            disabled={!gmail.connected || autoSync.isPending}
            onChange={(enabled) =>
              autoSync.mutate({ enabled }, { onError: (err) => toast(err.message, 'error') })
            }
          />
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            disabled={!gmail.connected || syncNow.isPending}
            onClick={() =>
              syncNow.mutate(undefined, {
                onSuccess: () => toast('Sync queued'),
                onError: (err) => toast(err.message, 'error'),
              })
            }
          >
            Sync now
          </Button>
          {!gmail.connected && <Badge>Connect Gmail first</Badge>}
        </div>
        {gmail.last_sync_result && !gmail.last_sync_error && (
          <p className="font-mono text-[11px] text-muted">Last run: {gmail.last_sync_result}</p>
        )}
        {gmail.last_sync_error && !gmail.reconnect_required && (
          <Alert>Last sync failed — {gmail.last_sync_error}</Alert>
        )}
        {gmail.failed_count > 0 && (
          <p className="text-[11px] text-muted">
            {gmail.failed_count} message{gmail.failed_count === 1 ? '' : 's'} failed to import and
            will be retried on the next sync.
          </p>
        )}
        <div className="flex items-end gap-2">
          <Field label="Resume from" htmlFor="gmail-since" hint="Empty = now.">
            <input
              id="gmail-since"
              type="date"
              max={formatIsoDate(new Date().toISOString())}
              value={since}
              onChange={(e) => setSince(e.target.value)}
              className={inputClass}
            />
          </Field>
          <Button
            variant="secondary"
            disabled={!gmail.connected || resetSync.isPending}
            onClick={() => setConfirm('reset')}
          >
            Reset sync state
          </Button>
        </div>
      </SettingsCard>

      <SettingsCard
        title="Inbox filters"
        description="Only mail from these senders (and, optionally, with this label) is ingested."
      >
        <form className="space-y-3" onSubmit={onSave}>
          <Field
            label="Sender allowlist"
            htmlFor="allowlist"
            hint="Comma-separated addresses or domains."
          >
            <textarea
              id="allowlist"
              name="allowlist"
              rows={3}
              defaultValue={gmail.allowlist.join(', ')}
              className={inputClass}
            />
          </Field>
          <TextField
            label="Label filter"
            name="label_filter"
            defaultValue={gmail.label_filter}
            placeholder="Sanctuary"
          />
          <Button type="submit" variant="secondary" disabled={save.isPending}>
            Save configuration
          </Button>
        </form>
      </SettingsCard>

      <SettingsCard
        title="Import history"
        description="Pull in older mail on purpose: browse it grouped by case reference and import the oldest first."
      >
        <div className="flex items-center gap-2">
          {gmail.connected ? (
            <Link to="/import" className={buttonClass('secondary')}>
              Open history import
            </Link>
          ) : (
            <Badge>Connect Gmail first</Badge>
          )}
        </div>
      </SettingsCard>

      <ConfirmDialog
        open={confirm === 'disconnect'}
        onClose={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          disconnect.mutate(undefined, {
            onSuccess: () => toast('Gmail disconnected'),
            onError: (err) => toast(err.message, 'error'),
          })
        }}
        title="Disconnect Gmail?"
        body="Sanctuary forgets the Gmail connection and revokes its own access at Google. Mail already imported stays; nothing in your mailbox is touched. Your sender allowlist is kept. Raw copies of fetched mail stay on this machine until you clear the cache on the Import page."
        label="Disconnect"
        danger
        pending={disconnect.isPending}
      />
      <ConfirmDialog
        open={confirm === 'reset'}
        onClose={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          resetSync.mutate(
            { since: since || null },
            {
              onSuccess: () => toast('Sync state reset'),
              onError: (err) => toast(err.message, 'error'),
            },
          )
        }}
        title="Reset sync state?"
        body={`Forgets failed messages and moves the sync point to ${since || 'now'}. Mail older than that is only imported via Import history.`}
        label="Reset"
        pending={resetSync.isPending}
      />
    </>
  )
}
