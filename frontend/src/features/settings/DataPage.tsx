import { useState } from 'react'

import { useDataView, useMaintenance } from '../../api/settings'
import { Button } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { SettingsCard } from '../../ui/SettingsCard'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

export function DataPage() {
  const viewQuery = useDataView()
  const view = viewQuery.data
  const reset = useMaintenance('reset-enrichment')
  const clear = useMaintenance('clear-all-data')
  const toast = useToast()
  const [confirm, setConfirm] = useState<'reset' | 'clear' | null>(null)
  if (!view) return <QueryState error={viewQuery.error} pending={viewQuery.isPending} />

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
