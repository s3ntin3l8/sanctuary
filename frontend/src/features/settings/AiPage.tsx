import { type FormEvent, useState } from 'react'

import type { Schemas } from '../../api/client'
import {
  useAiSettings,
  useCreateInstance,
  useDeleteInstance,
  useInstanceModels,
  useReindex,
  useReindexStatus,
  useRoleHealth,
  useSetConcurrency,
  useSetEngine,
  useSetRole,
  useTestInstance,
  useUpdateInstance,
} from '../../api/settings'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Field, inputClass } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { Modal } from '../../ui/Modal'
import { SettingsCard } from '../../ui/SettingsCard'
import { TextField } from '../../ui/TextField'
import { QueryState } from '../../ui/QueryState'
import { useToast } from '../../ui/toast'

type Instance = Schemas['AiInstance']
type Health = Schemas['AiHealth']
type RoleName = Schemas['AiRole']['role']

const ROLE_ICONS: Record<RoleName, string> = {
  chat: 'forum',
  embed: 'scatter_plot',
  ocr: 'document_scanner',
}

export function AiPage() {
  const settingsQuery = useAiSettings()
  const settings = settingsQuery.data
  const [editing, setEditing] = useState<Instance | 'new' | null>(null)
  if (!settings) return <QueryState error={settingsQuery.error} pending={settingsQuery.isPending} />
  return (
    <>
      <SettingsCard
        title="Connections"
        description="Local AI endpoints (Ollama, LM Studio, llama.cpp or an OpenAI-compatible server). The API shape is detected automatically."
      >
        <ul className="divide-y divide-line2">
          {settings.instances.map((inst) => (
            <InstanceRow
              key={inst.id}
              inst={inst}
              roles={settings.roles}
              onEdit={() => setEditing(inst)}
            />
          ))}
        </ul>
        <Button variant="secondary" onClick={() => setEditing('new')}>
          <Icon name="add" size={16} /> Add endpoint
        </Button>
      </SettingsCard>

      <SettingsCard title="Roles" description="Which endpoint and model handle each job.">
        <RoleCards settings={settings} />
      </SettingsCard>

      <EmbeddingIndex settings={settings} />
      <ExtractionEngine
        engine={settings.extraction_engine}
        tesseractAvailable={settings.tesseract_available}
      />
      <Workers settings={settings} />

      <EndpointModal
        key={editing === 'new' ? 'new' : (editing?.id ?? 'closed')}
        instance={editing === 'new' ? null : editing}
        open={editing !== null}
        onClose={() => setEditing(null)}
      />
    </>
  )
}

function HealthPill({ health, pending }: { health: Health | undefined; pending?: boolean }) {
  if (pending) return <span className="font-mono text-[10px] text-muted">testing…</span>
  if (!health) return null
  return (
    <span
      className={`inline-flex items-center gap-1 font-mono text-[10px] ${health.ok ? 'text-success' : 'text-danger'}`}
    >
      <Icon name={health.ok ? 'check_circle' : 'error'} size={13} />
      {health.provider && health.ok ? `${health.provider} · ` : ''}
      {health.detail}
    </span>
  )
}

function InstanceRow({
  inst,
  roles,
  onEdit,
}: {
  inst: Instance
  roles: Schemas['AiRole'][]
  onEdit: () => void
}) {
  const test = useTestInstance()
  const used = roles.filter((r) => r.active_id === inst.id)
  return (
    <li className="flex items-center gap-3 py-2.5">
      <Icon name="dns" size={18} className="text-muted" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-[13px] font-semibold">
          {inst.label}
          {inst.is_external && (
            <Badge tone="warning" className="normal-case">
              external endpoint
            </Badge>
          )}
        </div>
        <div className="truncate font-mono text-[11px] text-muted">
          {inst.base_url}
          {inst.has_api_key && ' · key set'}
          {used.length > 0 && ` · ${used.map((r) => r.label).join(', ')}`}
        </div>
        {test.data && (
          <div className="mt-0.5">
            <HealthPill health={test.data} />
          </div>
        )}
      </div>
      <Button
        size="sm"
        variant="secondary"
        disabled={test.isPending}
        onClick={() => test.mutate(inst.id)}
      >
        Test
      </Button>
      <Button size="sm" variant="secondary" onClick={onEdit}>
        Edit
      </Button>
    </li>
  )
}

function EndpointModal({
  instance,
  open,
  onClose,
}: {
  instance: Instance | null
  open: boolean
  onClose: () => void
}) {
  const create = useCreateInstance()
  const update = useUpdateInstance()
  const remove = useDeleteInstance()
  const toast = useToast()
  const [confirmRemove, setConfirmRemove] = useState(false)
  const pending = create.isPending || update.isPending
  const error = create.error ?? update.error ?? remove.error

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    const label = String(data.get('label'))
    const base_url = String(data.get('base_url'))
    const key = String(data.get('api_key'))
    const clearKey = data.get('clear_key') === 'on'
    const done = () => {
      toast(instance ? 'Endpoint saved' : 'Endpoint added')
      onClose()
    }
    if (instance) {
      // null keeps the stored key (never echoed to the browser); "" clears it.
      const api_key = clearKey ? '' : key === '' ? null : key
      update.mutate({ id: instance.id, body: { label, base_url, api_key } }, { onSuccess: done })
    } else {
      create.mutate({ label, base_url, api_key: key || null }, { onSuccess: done })
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={instance ? 'Edit endpoint' : 'Add endpoint'}
      icon="dns"
      width={460}
    >
      <form onSubmit={onSubmit} className="space-y-3">
        <TextField
          label="Name"
          name="label"
          required
          defaultValue={instance?.label ?? ''}
          placeholder="Ollama · local"
        />
        <TextField
          label="Base URL"
          name="base_url"
          required
          defaultValue={instance?.base_url ?? 'http://127.0.0.1:11434'}
          className="font-mono"
        />
        <TextField
          label="API key"
          name="api_key"
          type="password"
          autoComplete="off"
          placeholder={instance?.has_api_key ? '•••••••• (unchanged)' : 'optional'}
          hint="Only needed for OpenAI-compatible servers that require one. Stored locally."
        />
        {instance?.has_api_key && (
          <label className="flex items-center gap-2 text-[12px] text-ink2">
            <input type="checkbox" name="clear_key" className="h-4 w-4 accent-accent" /> Remove the
            stored key
          </label>
        )}
        {error && (
          <p role="alert" className="text-[11px] text-danger">
            {error.message}
          </p>
        )}
        <div className="flex gap-2 pt-1">
          {instance && (
            <Button variant="danger-outline" onClick={() => setConfirmRemove(true)}>
              Remove
            </Button>
          )}
          <span className="flex-1" />
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={pending}>
            {instance ? 'Save' : 'Add'}
          </Button>
        </div>
      </form>
      {instance && (
        <ConfirmDialog
          open={confirmRemove}
          onClose={() => setConfirmRemove(false)}
          onConfirm={() =>
            remove.mutate(instance.id, {
              onSuccess: () => {
                setConfirmRemove(false)
                toast('Endpoint removed')
                onClose()
              },
              onError: () => setConfirmRemove(false),
            })
          }
          title="Remove endpoint?"
          body={`${instance.label} will be removed. Roles pointing at it must be switched first.`}
          label="Remove"
          danger
          pending={remove.isPending}
        />
      )}
    </Modal>
  )
}

function RoleCards({ settings }: { settings: Schemas['AiSettingsView'] }) {
  const health = useRoleHealth(settings.instances.length > 0)
  return (
    <div className="grid gap-3">
      {settings.roles.map((role) => (
        <RoleCard
          key={role.role}
          role={role}
          instances={settings.instances}
          health={health.data?.[role.role]}
          healthPending={health.isPending}
        />
      ))}
    </div>
  )
}

function RoleCard({
  role,
  instances,
  health,
  healthPending,
}: {
  role: Schemas['AiRole']
  instances: Instance[]
  health: Health | undefined
  healthPending: boolean
}) {
  const setRole = useSetRole()
  const models = useInstanceModels(role.active_id)
  const toast = useToast()
  const options = models.data?.[role.role] ?? []
  const listed = role.model && !options.includes(role.model) ? [role.model, ...options] : options
  return (
    <div className="rounded-xl border border-line bg-card2 p-3">
      <div className="flex items-center gap-2">
        <Icon name={ROLE_ICONS[role.role]} size={18} className="text-accent" />
        <div className="text-[13px] font-semibold">{role.label}</div>
        <span className="font-mono text-[11px] text-muted">
          {role.model || '— no model —'}
          {role.embed_dim ? ` · ${role.embed_dim}d` : ''}
        </span>
        <span className="ml-auto">
          <HealthPill health={health} pending={healthPending && !!role.active_id} />
        </span>
      </div>
      <p className="mt-0.5 text-[11px] text-muted">{role.hint}</p>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <Field label="Endpoint" htmlFor={`endpoint-${role.role}`}>
          <select
            id={`endpoint-${role.role}`}
            value={role.active_id ?? ''}
            disabled={instances.length === 0 || setRole.isPending}
            onChange={(e) =>
              setRole.mutate(
                { role: role.role, body: { instance_id: e.target.value, model: '' } },
                { onError: (err) => toast(err.message, 'error') },
              )
            }
            className={inputClass}
          >
            {instances.length === 0 && <option value="">No endpoints</option>}
            {instances.map((i) => (
              <option key={i.id} value={i.id}>
                {i.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Model" htmlFor={`model-${role.role}`}>
          <div className="flex gap-1">
            <select
              id={`model-${role.role}`}
              value={role.model}
              disabled={!role.active_id || setRole.isPending}
              onChange={(e) =>
                setRole.mutate(
                  {
                    role: role.role,
                    body: { instance_id: role.active_id ?? '', model: e.target.value },
                  },
                  {
                    onSuccess: (r) =>
                      toast(
                        r.warning ?? `${r.role.model} active for ${role.label}`,
                        r.warning ? 'error' : 'success',
                      ),
                    onError: (err) => toast(err.message, 'error'),
                  },
                )
              }
              className={inputClass}
            >
              {!role.model && <option value="">— select a model —</option>}
              {listed.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <Button
              size="sm"
              variant="secondary"
              aria-label={`Discover models for ${role.label}`}
              disabled={!role.active_id || models.isFetching}
              onClick={() => models.refetch()}
            >
              <Icon name="refresh" size={16} />
            </Button>
          </div>
        </Field>
      </div>
    </div>
  )
}

function EmbeddingIndex({ settings }: { settings: Schemas['AiSettingsView'] }) {
  const job = useReindexStatus(settings.reindex_job).data
  const rebuild = useReindex('rebuild-index')
  const reindex = useReindex('reindex')
  const [confirm, setConfirm] = useState(false)
  const toast = useToast()
  const idx = settings.embed_index
  const running = job?.status === 'running'
  const pct =
    job && job.total > 0 ? Math.round((100 * (job.reindexed + job.failed)) / job.total) : 0
  return (
    <SettingsCard
      title="Embedding index"
      description="pgvector columns are sized to the embedding dimension; a model with a different dimension needs a rebuild."
    >
      <div className="flex items-center gap-4 font-mono text-[12px]">
        <span>
          Dim <strong>{idx.dim}</strong>
        </span>
        <span>
          Model <strong>{idx.model || '—'}</strong>
        </span>
        {idx.mismatch && (
          <Badge tone="danger" className="normal-case">
            index is {idx.index_dim}d — rebuild needed
          </Badge>
        )}
        <span className="flex-1" />
        <Button
          size="sm"
          variant="secondary"
          disabled={running || rebuild.isPending}
          onClick={() => setConfirm(true)}
        >
          Rebuild index
        </Button>
        <Button
          size="sm"
          variant="secondary"
          disabled={running || reindex.isPending}
          onClick={() =>
            reindex.mutate(undefined, { onError: (err) => toast(err.message, 'error') })
          }
        >
          Quick reindex
        </Button>
      </div>
      {job && (
        <div className="text-[11.5px]">
          {running ? (
            <>
              <div>
                Reindexing embeddings… {job.reindexed} of {job.total}
                {job.failed > 0 && ` (${job.failed} failed)`} · dim={job.embed_dim}
              </div>
              <div className="mt-1 h-1.5 w-full rounded bg-line2">
                <div
                  className="h-1.5 rounded bg-accent transition-all"
                  style={{ width: `${pct}%` }}
                />
              </div>
            </>
          ) : job.status === 'done' ? (
            <span className="text-success">
              Rebuilt at dim={job.embed_dim}: {job.reindexed}/{job.total} indexed
            </span>
          ) : (
            <span className="text-danger">Reindex failed. {job.error}</span>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() => {
          setConfirm(false)
          rebuild.mutate(undefined, { onError: (err) => toast(err.message, 'error') })
        }}
        title="Drop all vectors and reindex?"
        body="Every document chunk embedding is deleted, both vector columns are resized to the configured dimension, and all documents are re-embedded in the background."
        label="Rebuild"
        danger
      />
    </SettingsCard>
  )
}

function ExtractionEngine({
  engine,
  tesseractAvailable,
}: {
  engine: Schemas['AiSettingsView']['extraction_engine']
  tesseractAvailable: boolean
}) {
  const set = useSetEngine()
  const [current, setCurrent] = useState(engine)
  const toast = useToast()
  return (
    <SettingsCard
      title="Document extraction engine"
      description="How PDFs are turned into text before the AI reads them."
    >
      {(
        [
          [
            'chandra',
            'Chandra OCR',
            'Vision-model OCR via the OCR role. Best for scans and stamped letters. Recommended.',
          ],
          [
            'docling',
            'Docling + Tesseract',
            'Classic layout parsing with Tesseract fallback. No OCR model needed.',
          ],
        ] as const
      ).map(([value, label, hint]) => (
        <label key={value} className="flex items-start gap-2 text-[12.5px]">
          <input
            type="radio"
            name="engine"
            value={value}
            checked={current === value}
            onChange={() => {
              const previous = current
              setCurrent(value)
              set.mutate(
                { engine: value },
                {
                  onSuccess: () => toast(`Default engine: ${label}`),
                  onError: (err) => {
                    setCurrent(previous)
                    toast(err.message, 'error')
                  },
                },
              )
            }}
            className="mt-0.5 accent-accent"
          />
          <span>
            <span className="font-semibold">{label}</span>
            <span className="block text-[11px] text-muted">{hint}</span>
          </span>
        </label>
      ))}
      {!tesseractAvailable && (
        <p className="text-[11px] text-danger">
          The tesseract binary is not installed on this server: the Docling engine, and the fallback
          when Chandra fails, cannot read scanned pages.
        </p>
      )}
    </SettingsCard>
  )
}

function Workers({ settings }: { settings: Schemas['AiSettingsView'] }) {
  return (
    <SettingsCard
      title="Workers"
      description="Concurrency of the AI and OCR worker pools; applied live when the workers are running."
    >
      <div className="grid grid-cols-2 gap-3">
        <ConcurrencyField
          kind="worker"
          label="AI concurrency"
          initial={settings.worker_concurrency}
        />
        <ConcurrencyField kind="ocr" label="OCR concurrency" initial={settings.ocr_concurrency} />
      </div>
    </SettingsCard>
  )
}

function ConcurrencyField({
  kind,
  label,
  initial,
}: {
  kind: 'worker' | 'ocr'
  label: string
  initial: number
}) {
  const set = useSetConcurrency(kind)
  const toast = useToast()
  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        const value = Number(new FormData(e.currentTarget).get('concurrency'))
        set.mutate(
          { concurrency: value },
          {
            onSuccess: (r) =>
              toast(
                r.applied_live
                  ? `${label} → ${r.concurrency} (applied live)`
                  : `Saved (${r.concurrency}); applies on next worker start`,
              ),
            onError: (err) => toast(err.message, 'error'),
          },
        )
      }}
    >
      <div className="flex-1">
        <TextField
          label={label}
          name="concurrency"
          type="number"
          min={1}
          max={16}
          defaultValue={initial}
        />
      </div>
      <Button type="submit" variant="secondary" disabled={set.isPending}>
        Apply
      </Button>
    </form>
  )
}
