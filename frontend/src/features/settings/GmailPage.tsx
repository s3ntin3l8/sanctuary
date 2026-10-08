import { useState } from 'react'
import { Link } from 'react-router'

import {
  useDisconnectGmail,
  useGmail,
  useGmailFilterPreview,
  useGmailSyncNow,
  useResetGmailSync,
  useSaveGmailFilters,
  useSetGmailSyncMode,
} from '../../api/settings'
import { formatIsoDate } from '../../format'
import { useCheckGmailNew } from '../../api/gmailImport'
import { Alert } from '../../ui/Alert'
import { Badge } from '../../ui/Badge'
import { Button, buttonClass } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Field, inputClass } from '../../ui/Field'
import type { Schemas } from '../../api/client'
import { Icon } from '../../ui/Icon'
import { Modal } from '../../ui/Modal'
import { SettingsCard } from '../../ui/SettingsCard'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

type GmailView = Schemas['GmailView']

/** Which part of the mailbox Sanctuary may read. Required: it bounds every list call. */
function FiltersCard({ gmail }: { gmail: GmailView }) {
  const save = useSaveGmailFilters()
  const preview = useGmailFilterPreview()
  const toast = useToast()
  const [allowlist, setAllowlist] = useState(gmail.allowlist.join(', '))
  const [label, setLabel] = useState(gmail.label_filter)
  const [estimate, setEstimate] = useState<number | null>(null)

  const body = {
    allowlist: allowlist
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
    label_filter: label.trim(),
  }
  const empty = body.allowlist.length === 0 && body.label_filter === ''

  function edit(update: () => void) {
    update()
    setEstimate(null) // a count for the previous text would mislead
  }

  return (
    <SettingsCard
      title="What Sanctuary reads"
      description="Only mail matching these filters is ever indexed or imported. Set a sender allowlist, a label, or both."
    >
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault()
          const submitted = { allowlist, label }
          save.mutate(body, {
            onSuccess: (saved) => {
              toast('Gmail filters saved')
              // Show the saved (trimmed) form — but never over edits made since clicking Save.
              setAllowlist((cur) =>
                cur === submitted.allowlist ? saved.allowlist.join(', ') : cur,
              )
              setLabel((cur) => (cur === submitted.label ? saved.label_filter : cur))
            },
            onError: (err) => toast(err.message, 'error'),
          })
        }}
      >
        <Field
          label="Sender allowlist"
          htmlFor="allowlist"
          hint="Comma-separated addresses or domains, e.g. info@firm.de, firm.de"
        >
          <textarea
            id="allowlist"
            rows={2}
            value={allowlist}
            onChange={(e) => edit(() => setAllowlist(e.target.value))}
            className={inputClass}
          />
        </Field>
        <Field
          label="Label"
          htmlFor="label-filter"
          hint="A Gmail label such as Sanctuary. Combined with the senders, mail must match both."
        >
          <input
            id="label-filter"
            value={label}
            placeholder="Sanctuary"
            onChange={(e) => edit(() => setLabel(e.target.value))}
            className={inputClass}
          />
        </Field>
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" variant="secondary" disabled={empty || save.isPending}>
            Save filters
          </Button>
          <Button
            variant="secondary"
            disabled={empty || !gmail.connected || preview.isPending}
            onClick={() =>
              preview.mutate(body, {
                onSuccess: (r) => setEstimate(r.estimate),
                onError: (err) => toast(err.message, 'error'),
              })
            }
          >
            Preview matches
          </Button>
          {!gmail.connected && <span className="text-[11px] text-muted">Connect Gmail first</span>}
          {empty && <Badge tone="warning">Required</Badge>}
          {estimate !== null && (
            <span className="text-[11.5px] text-muted">
              Matches about <strong className="text-ink">{estimate}</strong> message
              {estimate === 1 ? '' : 's'} (Gmail&apos;s estimate)
            </span>
          )}
        </div>
      </form>
    </SettingsCard>
  )
}

/** Moves the sync point and forgets failed ids — the rare "start syncing from…" action. */
function SyncPointModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const resetSync = useResetGmailSync()
  const toast = useToast()
  const [since, setSince] = useState('')
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Change sync point"
      subtitle="Where syncing resumes from"
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={resetSync.isPending}
            onClick={() =>
              resetSync.mutate(
                { since: since || null },
                {
                  onSuccess: () => {
                    toast('Sync point changed')
                    onClose()
                  },
                  onError: (err) => toast(err.message, 'error'),
                },
              )
            }
          >
            Change
          </Button>
        </>
      }
    >
      <div className="space-y-3 text-[12px]">
        <p className="text-muted">
          Sync only fetches mail newer than the sync point. Moving it to an earlier date makes the
          next sync pick up everything since then; moving it to now skips what&apos;s in between
          (older mail stays available through Import history). Messages that failed to import are
          forgotten.
        </p>
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
      </div>
    </Modal>
  )
}

const SYNC_MODES = [
  {
    value: 'off',
    label: 'Off',
    hint: 'Nothing runs in the background. "Sync now" imports new mail when you ask.',
  },
  {
    value: 'notify',
    label: 'Notify me',
    hint: 'Every 5 minutes, look for new mail and tell me. I review it and choose what to import.',
  },
  {
    value: 'auto',
    label: 'Import automatically',
    hint: 'Every 5 minutes, import new mail matching the filters into Triage.',
  },
] as const

export function GmailPage() {
  const gmailQuery = useGmail()
  const gmail = gmailQuery.data
  const setMode = useSetGmailSyncMode()
  const syncNow = useGmailSyncNow()
  const checkNew = useCheckGmailNew()
  const disconnect = useDisconnectGmail()
  const toast = useToast()
  const [confirmDisconnect, setConfirmDisconnect] = useState(false)
  const [syncPointOpen, setSyncPointOpen] = useState(false)

  if (!gmail) return <QueryState error={gmailQuery.error} pending={gmailQuery.isPending} />

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
            <Button variant="secondary" onClick={() => setConfirmDisconnect(true)}>
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

      <FiltersCard gmail={gmail} />

      <SettingsCard
        title="Sync"
        description="New mail is whatever arrived after the sync point. Older mail is imported deliberately via Import history."
      >
        <fieldset className="space-y-2" disabled={!gmail.connected || setMode.isPending}>
          <legend className="mb-1 text-[13px] font-semibold">New mail</legend>
          {SYNC_MODES.map((m) => (
            <label
              key={m.value}
              className={`flex cursor-pointer items-start gap-2.5 rounded-[9px] border px-3 py-2 ${
                gmail.sync_mode === m.value ? 'border-accent/50 bg-accent/5' : 'border-line'
              }`}
            >
              <input
                type="radio"
                name="gmail-sync-mode"
                value={m.value}
                checked={gmail.sync_mode === m.value}
                onChange={() =>
                  setMode.mutate(
                    { mode: m.value },
                    { onError: (err) => toast(err.message, 'error') },
                  )
                }
                className="mt-0.5"
              />
              <span>
                <span className="block text-[12.5px] font-semibold">{m.label}</span>
                <span className="block text-[11px] text-muted">{m.hint}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          {gmail.sync_mode === 'notify' ? (
            <Button
              variant="secondary"
              disabled={!gmail.connected || checkNew.isPending}
              onClick={() =>
                checkNew.mutate(undefined, {
                  onSuccess: () => toast('Checking for new mail'),
                  onError: (err) => toast(err.message, 'error'),
                })
              }
            >
              Check for new mail
            </Button>
          ) : (
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
          )}
          {!gmail.connected && <Badge>Connect Gmail first</Badge>}
          <span className="font-mono text-[11px] text-muted">
            {gmail.last_sync_at
              ? `Sync point ${formatIsoDate(gmail.last_sync_at)}`
              : 'No sync point yet'}
          </span>
          {gmail.connected && (
            <button
              type="button"
              onClick={() => setSyncPointOpen(true)}
              className="ml-auto text-[11px] text-muted underline-offset-2 hover:underline"
            >
              Change sync point…
            </button>
          )}
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
      </SettingsCard>

      <SettingsCard
        title="Import history"
        description="Pull in older mail on purpose: browse it grouped by case reference and import the oldest first."
      >
        <div className="flex items-center gap-2">
          {gmail.connected ? (
            <Link to="/settings/gmail/import" className={buttonClass('secondary')}>
              Open history import
            </Link>
          ) : (
            <Badge>Connect Gmail first</Badge>
          )}
        </div>
      </SettingsCard>

      <ConfirmDialog
        open={confirmDisconnect}
        onClose={() => setConfirmDisconnect(false)}
        onConfirm={() => {
          setConfirmDisconnect(false)
          disconnect.mutate(undefined, {
            onSuccess: () => toast('Gmail disconnected'),
            onError: (err) => toast(err.message, 'error'),
          })
        }}
        title="Disconnect Gmail?"
        body="Sanctuary forgets the Gmail connection and revokes its own access at Google. Mail already imported stays; nothing in your mailbox is touched. Your filters are kept. Raw copies of fetched mail stay on this machine until you clear the cache on the Import page."
        label="Disconnect"
        danger
        pending={disconnect.isPending}
      />
      <SyncPointModal
        key={String(syncPointOpen)}
        open={syncPointOpen}
        onClose={() => setSyncPointOpen(false)}
      />
    </>
  )
}
