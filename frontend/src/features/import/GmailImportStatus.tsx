import { useState } from 'react'
import { Link } from 'react-router'

import { useCancelGmailImport } from '../../api/gmailImport'
import { useWorkerQueue } from '../../api/shell'
import type { Schemas } from '../../api/client'
import { Alert } from '../../ui/Alert'
import { Button } from '../../ui/Button'
import { Icon } from '../../ui/Icon'
import { Progress } from '../../ui/Progress'
import { useToast } from '../../ui/toast'

export type GmailRun = Schemas['GmailImportStatus']

export const IMPORT_PATH = '/settings/gmail/import'

function Summary({ run }: { run: GmailRun }) {
  if (run.active) {
    return (
      <>
        Importing {run.done}/{run.total}
        {run.current_subject && (
          <span className="text-muted">
            {' · “'}
            {run.current_subject}
            {'”'}
          </span>
        )}
      </>
    )
  }
  return (
    <>
      {run.cancelled ? 'Stopped after' : 'Imported'} {run.done} of {run.total}
      {run.failed_count > 0 && <span className="text-danger"> · {run.failed_count} failed</span>}
    </>
  )
}

function StopButton({ className }: { className?: string }) {
  const cancel = useCancelGmailImport()
  const toast = useToast()
  return (
    <Button
      variant="secondary"
      size="sm"
      className={className}
      disabled={cancel.isPending}
      onClick={() => cancel.mutate(undefined, { onError: (err) => toast(err.message, 'error') })}
    >
      Stop
    </Button>
  )
}

/**
 * A Gmail import run. `full` is the Import page's box; `compact` is the one-line
 * form shown in the processing queue and on Triage.
 */
export function GmailImportStatus({
  run,
  variant,
  onDismiss,
  onNavigate,
}: {
  run: GmailRun
  variant: 'full' | 'compact'
  /** Compact only: shown once the run has ended. */
  onDismiss?: () => void
  /** Compact only: lets a modal close when the link is followed. */
  onNavigate?: () => void
}) {
  if (variant === 'full') {
    return (
      <div className="mb-4 space-y-1.5 rounded-xl border border-line bg-card px-4 py-3">
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1 truncate">
            <Summary run={run} />
          </div>
          {run.active && <StopButton />}
        </div>
        <Progress value={run.done} max={run.total} label="Import progress" />
        {run.error && <Alert>{run.error}</Alert>}
      </div>
    )
  }
  return (
    <div className="space-y-1.5 rounded-xl border border-line bg-card px-3 py-2 text-[12px]">
      <div className="flex items-center gap-2">
        <Icon name="mail" size={14} className="shrink-0 text-muted" />
        <div className="min-w-0 flex-1 truncate">
          <Summary run={run} />
        </div>
        <Link
          to={IMPORT_PATH}
          onClick={onNavigate}
          className="shrink-0 text-[11px] text-tealink hover:underline"
        >
          Import history
        </Link>
        {run.active && <StopButton />}
        {!run.active && onDismiss && (
          <button
            type="button"
            aria-label="Dismiss"
            onClick={onDismiss}
            className="shrink-0 text-muted hover:text-ink"
          >
            <Icon name="close" size={14} />
          </button>
        )}
      </div>
      {run.active && <Progress value={run.done} max={run.total} label="Import progress" />}
      {run.error && <p className="text-[11px] text-danger">{run.error}</p>}
    </div>
  )
}

const dismissKey = (run: GmailRun) => `gmail-import-dismissed:${run.started_at}`

/**
 * The run to announce on Triage (taken from the queue the shell already polls):
 * live, or just finished and not yet dismissed. Dismissal is per run.
 */
export function useGmailRunBanner() {
  const run = useWorkerQueue().data?.gmail_import ?? null
  const [dismissed, setDismissed] = useState<string | null>(null)
  if (!run) return { run: null, dismiss: undefined }
  const gone =
    !run.active && (dismissed === dismissKey(run) || sessionStorage.getItem(dismissKey(run)))
  return {
    run: gone ? null : run,
    dismiss: () => {
      sessionStorage.setItem(dismissKey(run), '1')
      setDismissed(dismissKey(run))
    },
  }
}
