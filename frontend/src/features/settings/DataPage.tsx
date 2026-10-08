import { useState } from 'react'

import {
  useDataView,
  useDebugLog,
  useDebugLogs,
  useMaintenance,
  useSetDebugRedact,
} from '../../api/settings'
import { Button } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Icon } from '../../ui/Icon'
import { SettingsCard } from '../../ui/SettingsCard'
import { Toggle } from '../../ui/Toggle'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

export function DataPage() {
  const viewQuery = useDataView()
  const view = viewQuery.data
  const reset = useMaintenance('reset-enrichment')
  const clear = useMaintenance('clear-all-data')
  const redact = useSetDebugRedact()
  const toast = useToast()
  const [confirm, setConfirm] = useState<'reset' | 'clear' | null>(null)
  const [redactOn, setRedactOn] = useState<boolean | null>(null)
  if (!view) return <QueryState error={viewQuery.error} pending={viewQuery.isPending} />
  const redactValue = redactOn ?? view.ai_debug_redact

  const stats = [
    [view.doc_count, 'Documents'],
    [view.case_count, 'Cases'],
    [view.claim_count, 'Claims'],
    [view.cost_count, 'Cost entries'],
    [`${view.db_size_mb} MB`, 'Database'],
  ] as const

  return (
    <>
      <SettingsCard title="Workspace">
        <div className="grid grid-cols-5 gap-2">
          {stats.map(([value, label]) => (
            <div key={label} className="rounded-xl border border-line bg-card2 px-3 py-2">
              <div className="font-mono text-[18px] font-semibold">{value}</div>
              <div className="text-[10px] font-bold tracking-[.1em] text-muted uppercase">
                {label}
              </div>
            </div>
          ))}
        </div>
      </SettingsCard>

      <SettingsCard title="Danger zone" danger description="These cannot be undone.">
        <div className="flex items-center gap-3">
          <div className="flex-1 text-[12px]">
            <div className="font-semibold">Reset AI enrichment</div>
            <div className="text-muted">
              Clears summaries, tiers, key passages and all vectors. Documents stay.
            </div>
          </div>
          <Button
            variant="danger-outline"
            disabled={reset.isPending}
            onClick={() => setConfirm('reset')}
          >
            Reset
          </Button>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex-1 text-[12px]">
            <div className="font-semibold">Clear workspace</div>
            <div className="text-muted">
              Deletes every case, document and file. Keeps accounts, settings and the audit log.
            </div>
          </div>
          <Button variant="danger" disabled={clear.isPending} onClick={() => setConfirm('clear')}>
            Clear
          </Button>
        </div>
      </SettingsCard>

      <SettingsCard title="Debug log privacy">
        <div className="flex items-center gap-3 text-[12px]">
          <div className="flex-1">
            <div className="font-semibold">Redact PII in AI debug logs</div>
            <div className="text-muted">
              Masks names and addresses in the prompts written to the local debug log.
            </div>
          </div>
          <Toggle
            checked={redactValue}
            label="Redact PII in AI debug logs"
            disabled={redact.isPending}
            onChange={(next) => {
              setRedactOn(next)
              redact.mutate({ enabled: next }, { onError: () => setRedactOn(!next) })
            }}
          />
        </div>
      </SettingsCard>

      <DebugLogs />

      <ConfirmDialog
        open={confirm === 'reset'}
        onClose={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          reset.mutate(undefined, {
            onSuccess: (r) => toast(r.message),
            onError: (e) => toast(e.message, 'error'),
          })
        }}
        title="Reset AI enrichment?"
        body="All AI-generated summaries, significance tiers, key passages and embeddings are cleared. Re-enrichment runs again from the pipeline."
        label="Reset"
        danger
      />
      <ConfirmDialog
        open={confirm === 'clear'}
        onClose={() => setConfirm(null)}
        onConfirm={() => {
          setConfirm(null)
          clear.mutate(undefined, {
            onSuccess: (r) => toast(r.message),
            onError: (e) => toast(e.message, 'error'),
          })
        }}
        title="Clear the whole workspace?"
        body="Every case, document, claim, cost and file is deleted for all users. Accounts, settings and the audit log survive."
        label="Clear workspace"
        danger
      />
    </>
  )
}

function DebugLogs() {
  const logs = useDebugLogs().data
  const [path, setPath] = useState<string | null>(null)
  const log = useDebugLog(path).data
  return (
    <SettingsCard
      title="Recent AI calls"
      description={logs ? `Local debug index in ${logs.log_root}` : undefined}
    >
      {!logs || logs.rows.length === 0 ? (
        <p className="text-[12px] text-muted">No AI calls recorded yet.</p>
      ) : (
        <table className="w-full text-[11px]">
          <thead className="text-left text-[9.5px] font-extrabold tracking-[.1em] text-muted uppercase">
            <tr>
              <th className="py-1">Time</th>
              <th>Kind</th>
              <th>Scope</th>
              <th>Stage</th>
              <th>Model</th>
              <th className="text-right">Duration</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody className="divide-y divide-line2 font-mono">
            {logs.rows.map((r, i) => (
              <tr key={`${r.ts}-${i}`}>
                <td className="py-1 text-muted">{r.ts?.slice(0, 19).replace('T', ' ')}</td>
                <td>{r.kind}</td>
                <td>{r.scope_id}</td>
                <td>{r.stage}</td>
                <td className="truncate">{r.model}</td>
                <td className="text-right">
                  {r.duration_ms != null ? `${Math.round(r.duration_ms / 1000)}s` : ''}
                </td>
                <td className={r.status === 'ok' ? 'text-success' : r.status ? 'text-danger' : ''}>
                  {r.status}
                </td>
                <td className="text-right">
                  {r.path && (
                    <button
                      type="button"
                      onClick={() => setPath(r.path)}
                      className="text-tealink hover:underline"
                    >
                      View
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {path && (
        <div className="rounded-xl border border-line bg-card2 p-3">
          <div className="mb-2 flex items-center gap-2 font-mono text-[11px] text-muted">
            {path}
            <button
              type="button"
              onClick={() => setPath(null)}
              aria-label="Close log"
              className="ml-auto text-ink"
            >
              <Icon name="close" size={16} />
            </button>
          </div>
          <pre className="max-h-96 overflow-auto text-[11px] whitespace-pre-wrap">
            {log?.body ?? 'Loading…'}
          </pre>
        </div>
      )}
    </SettingsCard>
  )
}
