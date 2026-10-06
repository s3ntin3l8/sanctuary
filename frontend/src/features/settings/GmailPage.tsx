import { type FormEvent, useState } from 'react'

import { useGmail, useGmailBackfill, useSaveGmailFilters } from '../../api/settings'
import { formatIsoDate } from '../../format'
import { Badge } from '../../ui/Badge'
import { Button, buttonClass } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { SettingsCard } from '../../ui/SettingsCard'
import { TextField } from '../../ui/TextField'
import { useToast } from '../../ui/toast'

const WINDOWS = [
  [90, 'Past 90 days'],
  [365, 'Past year'],
  [1825, 'Past 5 years'],
] as const

export function GmailPage() {
  const gmail = useGmail().data
  const save = useSaveGmailFilters()
  const backfill = useGmailBackfill()
  const toast = useToast()
  const [days, setDays] = useState<90 | 365 | 1825>(90)

  if (!gmail) return null

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
      { onSuccess: () => toast('Gmail configuration saved') },
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
              {gmail.last_sync_at && ` · last sync ${formatIsoDate(gmail.last_sync_at)}`}
            </div>
          </div>
          <a href={gmail.oauth_start_url} className={buttonClass('secondary')}>
            {gmail.connected ? 'Reconnect' : 'Connect Gmail'}
          </a>
        </div>
        <p className="flex items-center gap-1 text-[11px] text-muted">
          <Icon name="lock" size={12} /> Read-only access; mail is processed locally.
        </p>
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
        title="Bulk backfill"
        description="Import older mail once. Runs in the background; results land in Triage."
      >
        <div className="flex items-center gap-2">
          <select
            aria-label="Backfill window"
            value={days}
            onChange={(e) => setDays(Number(e.target.value) as 90 | 365 | 1825)}
            className="rounded-[9px] border border-line bg-panel2 px-2 py-1.5 text-[12px]"
          >
            {WINDOWS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
          <Button
            disabled={!gmail.connected || backfill.isPending}
            onClick={() =>
              backfill.mutate(
                { days },
                {
                  onSuccess: () => toast('Backfill queued'),
                  onError: (err) => toast(err.message, 'error'),
                },
              )
            }
          >
            Run
          </Button>
          {!gmail.connected && <Badge>Connect Gmail first</Badge>}
        </div>
      </SettingsCard>
    </>
  )
}
