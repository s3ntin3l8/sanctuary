import { type FormEvent, useEffect, useState } from 'react'
import { Link } from 'react-router'

import { useCasesDirectory } from '../../api/cases'
import type { Schemas } from '../../api/client'
import {
  type CostAction,
  type CostRow,
  useCostsOverview,
  useCreateCost,
  useEditCost,
  useLedgerAction,
} from '../../api/costs'
import { formatEur, formatShortDate } from '../../format'
import { Badge, type Tone } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { Modal } from '../../ui/Modal'
import { QueryState } from '../../ui/QueryState'
import { Toggle } from '../../ui/Toggle'
import { useToast } from '../../ui/toast'

const CATEGORIES: [Schemas['CostCategory'], string][] = [
  ['gerichtskosten', 'Court fees (GKG)'],
  ['anwaltskosten', 'Own lawyer (RVG)'],
  ['anwaltskosten_gegner', 'Opposing lawyer'],
  ['sachverstaendiger', 'Expert (JVEG)'],
  ['vorschuss', 'Advance'],
  ['vollstreckung', 'Enforcement'],
  ['auslagen', 'Expenses'],
  ['sonstiges', 'Other'],
]
const STATUS_TONE: Record<string, Tone> = {
  offen: 'warning',
  bezahlt: 'success',
  erstattet: 'success',
  teilweise: 'warning',
  strittig: 'danger',
}

/** The ledger: every booked cost across the caller's cases, with overdue and due-soon alerts. */
export function CostsPage() {
  const query = useCostsOverview()
  const [adding, setAdding] = useState(false)
  useEffect(() => {
    document.title = 'Costs | The Sanctuary'
  }, [])
  const data = query.data
  if (!data) return <QueryState error={query.error} pending={query.isPending} />
  const canAddAnywhere = data.cases.some((c) => c.can_edit)
  return (
    <div className="mx-auto max-w-[1100px] p-6 text-[12px]">
      <header className="mb-4 flex items-center gap-3">
        <div>
          <h1 className="font-display text-[22px] font-extrabold">Costs</h1>
          <p className="text-muted">
            {data.cases.length} cases · {formatEur(data.summary.booked)} booked
          </p>
        </div>
        <Button className="ml-auto px-3 py-1.5 text-[11.5px]" onClick={() => setAdding(true)}>
          <Icon name="add" size={14} /> Add cost
        </Button>
      </header>

      <dl className="mb-5 grid grid-cols-4 gap-3" aria-label="Ledger totals">
        {(
          [
            ['Booked', data.summary.booked],
            ['Paid', data.summary.paid],
            ['Outstanding', data.summary.outstanding],
            ['§91 ZPO reimbursable', data.summary.reimbursable],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="rounded-xl border border-line bg-card p-3">
            <dt className="text-[10px] font-bold tracking-[.12em] text-muted uppercase">{label}</dt>
            <dd className="font-display text-[17px] font-bold">{formatEur(value)}</dd>
          </div>
        ))}
      </dl>

      {(data.overdue.length > 0 || data.due_soon.length > 0) && (
        <div className="mb-5 grid grid-cols-2 gap-3">
          <Alerts title="Overdue" tone="danger" items={data.overdue} />
          <Alerts title="Due within 7 days" tone="warning" items={data.due_soon} />
        </div>
      )}

      {data.cases.length === 0 && (
        <p className="rounded-xl border border-dashed border-line p-8 text-center text-muted">
          No costs booked yet.{canAddAnywhere ? ' Add the first one above.' : ''}
        </p>
      )}
      {data.cases.map((group) => (
        <section key={group.id} className="mb-4 rounded-xl border border-line bg-card p-3">
          <header className="mb-2 flex items-center gap-2">
            <Link
              to={`/cases/${group.id}`}
              className="font-mono text-[11px] font-semibold text-tealink hover:underline"
            >
              {group.id}
            </Link>
            <h2 className="font-display text-[13px] font-bold">{group.title}</h2>
            <Badge>{group.status.replace('_', ' ')}</Badge>
            <span className="ml-auto font-mono text-[10.5px] text-muted">
              outstanding {formatEur(group.summary.outstanding)} · §91{' '}
              {formatEur(group.summary.reimbursable)}
            </span>
            <span className="font-display text-[14px] font-bold">
              {formatEur(group.summary.booked)}
            </span>
          </header>
          <ul className="divide-y divide-line2">
            {group.costs.map((c) => (
              <Row key={c.id} cost={c} canEdit={group.can_edit} />
            ))}
          </ul>
        </section>
      ))}

      <p className="mt-6 text-[10.5px] text-muted">
        RVG (lawyer fees) · GKG (court fees) · JVEG (experts) · §91 ZPO (the losing party bears the
        costs).
      </p>

      <AddCostModal open={adding} onClose={() => setAdding(false)} />
    </div>
  )
}

function Alerts({
  title,
  tone,
  items,
}: {
  title: string
  tone: 'danger' | 'warning'
  items: Schemas['CostAlert'][]
}) {
  const cls = tone === 'danger' ? 'border-danger/40 bg-danger/8' : 'border-warning/40 bg-warning/8'
  return (
    <section className={`rounded-xl border p-3 ${cls}`} aria-label={title}>
      <h2
        className={`mb-1.5 text-[10px] font-bold tracking-[.12em] uppercase ${tone === 'danger' ? 'text-danger' : 'text-warning'}`}
      >
        {title} · {items.length}
      </h2>
      {items.length === 0 && <p className="text-muted">None.</p>}
      <ul className="space-y-1">
        {items.map((a) => (
          <li key={a.cost.id} className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate">{a.cost.title}</span>
            <Link
              to={`/cases/${a.cost.case_id}`}
              className="truncate text-[11px] text-muted hover:underline"
            >
              {a.case_title}
            </Link>
            <span className="font-mono text-[10.5px] text-muted">
              {formatShortDate(a.cost.due_at)}
            </span>
            <span className="font-mono text-[11px]">{formatEur(a.open_amount)}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

function Row({ cost, canEdit }: { cost: CostRow; canEdit: boolean }) {
  const toast = useToast()
  const ledger = useLedgerAction()
  const edit = useEditCost()
  const [editing, setEditing] = useState(false)
  const onError = (e: Error) => toast(e.message, 'error')
  const run = (action: CostAction) => ledger.mutate({ costId: cost.id, action }, { onError })
  const paid = cost.amount_paid ?? 0
  const reimbursed = cost.amount_reimbursed ?? 0
  const category = CATEGORIES.find(([k]) => k === cost.category)?.[1] ?? cost.category
  return (
    <li className="grid grid-cols-[150px_1fr_110px_90px_100px_90px_auto] items-center gap-2 py-1.5">
      <Badge mono>{category}</Badge>
      <span className="min-w-0">
        <span className="block truncate font-medium">
          {cost.source_document_id ? (
            <Link to={`/document/${cost.source_document_id}`} className="hover:underline">
              {cost.title}
            </Link>
          ) : (
            cost.title
          )}
          {cost.auto_created && (
            <span className="ml-1 font-mono text-[9px] text-muted" title="booked from a document">
              auto
            </span>
          )}
        </span>
        <span className="block truncate font-mono text-[10px] text-muted">
          {cost.rvg_position ? `${cost.rvg_position} · ` : ''}
          {cost.issued_at ? `${formatShortDate(cost.issued_at)} · ` : ''}
          {cost.notes ?? ''}
        </span>
      </span>
      <span className="font-mono text-[10.5px] text-muted">
        {cost.streitwert !== null ? `SW ${formatEur(cost.streitwert)}` : '—'}
        {cost.gebuehren_faktor ? ` × ${cost.gebuehren_faktor}` : ''}
      </span>
      <span className="text-right font-mono text-[11px]">
        {formatEur(cost.amount_gross)}
        <span className="block text-[9.5px] text-muted">
          {formatEur(cost.amount_net)} + {Math.round((cost.vat_rate ?? 0) * 100)}%
        </span>
      </span>
      <span className="text-right font-mono text-[10.5px] text-muted">
        {paid > 0 ? `paid ${formatEur(paid)}` : ''}
        {reimbursed > 0 ? ` ↩ ${formatEur(reimbursed)}` : ''}
      </span>
      <span className="flex flex-col items-start gap-0.5">
        <Badge tone={STATUS_TONE[cost.status] ?? 'neutral'}>{cost.status}</Badge>
        {cost.is_reimbursable === false && (
          <span className="text-[9.5px] text-muted">not reimbursable</span>
        )}
      </span>
      <span className="flex justify-end gap-1">
        {canEdit && paid < cost.amount_gross && cost.status !== 'erstattet' && (
          <Button
            variant="secondary"
            className="px-1.5 py-0.5 text-[10px]"
            onClick={() => run('pay')}
          >
            paid
          </Button>
        )}
        {canEdit && paid > 0 && (
          <Button
            variant="secondary"
            className="px-1.5 py-0.5 text-[10px]"
            onClick={() => run('unpay')}
          >
            ↺ paid
          </Button>
        )}
        {canEdit && cost.is_reimbursable !== false && reimbursed < cost.amount_gross && (
          <Button
            variant="secondary"
            className="px-1.5 py-0.5 text-[10px]"
            onClick={() => run('reimburse')}
          >
            reimbursed
          </Button>
        )}
        {canEdit && reimbursed > 0 && (
          <Button
            variant="secondary"
            className="px-1.5 py-0.5 text-[10px]"
            onClick={() => run('unreimburse')}
          >
            ↺ reimb.
          </Button>
        )}
        {canEdit && (
          <button
            type="button"
            aria-label={`Edit ${cost.title}`}
            onClick={() => setEditing(true)}
            className="text-muted hover:text-ink"
          >
            <Icon name="edit" size={14} />
          </button>
        )}
      </span>
      {editing && (
        <EditCostModal
          cost={cost}
          onClose={() => setEditing(false)}
          onSave={(patch) =>
            edit.mutate(
              { costId: cost.id, ...patch },
              {
                onSuccess: () => {
                  toast('Cost updated')
                  setEditing(false)
                },
                onError,
              },
            )
          }
          pending={edit.isPending}
        />
      )}
    </li>
  )
}

function EditCostModal({
  cost,
  onClose,
  onSave,
  pending,
}: {
  cost: CostRow
  onClose: () => void
  onSave: (patch: Schemas['CostFieldUpdate']) => void
  pending: boolean
}) {
  const [reimbursable, setReimbursable] = useState(cost.is_reimbursable !== false)
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const d = new FormData(e.currentTarget)
    const num = (k: string) => {
      const v = String(d.get(k) ?? '').trim()
      return v ? Number(v) : null
    }
    const date = (k: string) => {
      const v = String(d.get(k) ?? '').trim()
      return v ? new Date(v).toISOString() : null
    }
    onSave({
      title: String(d.get('title')),
      category: String(d.get('category')) as Schemas['CostCategory'],
      status: String(d.get('status')) as Schemas['CostStatus'],
      amount_net: num('amount_net') ?? undefined,
      vat_rate: num('vat_rate') === null ? undefined : (num('vat_rate') ?? 0) / 100,
      amount_paid: num('amount_paid') ?? undefined,
      amount_reimbursed: num('amount_reimbursed') ?? undefined,
      streitwert: num('streitwert'),
      gebuehren_faktor: num('gebuehren_faktor'),
      due_at: date('due_at'),
      notes: String(d.get('notes') ?? '') || null,
      is_reimbursable: reimbursable,
    })
  }
  return (
    <Modal open onClose={onClose} title="Edit cost" subtitle={cost.title} icon="edit" width={560}>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Title">
          <input name="title" defaultValue={cost.title} required className={inputClass} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Category">
            <select name="category" defaultValue={cost.category} className={inputClass}>
              {CATEGORIES.map(([k, l]) => (
                <option key={k} value={k}>
                  {l}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Status">
            <select name="status" defaultValue={cost.status} className={inputClass}>
              {Object.keys(STATUS_TONE).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Net amount (€)">
            <input
              name="amount_net"
              type="number"
              step="0.01"
              min="0.01"
              defaultValue={cost.amount_net}
              className={inputClass}
            />
          </Field>
          <Field label="VAT (%)">
            <input
              name="vat_rate"
              type="number"
              step="1"
              min="0"
              max="100"
              defaultValue={Math.round((cost.vat_rate ?? 0) * 100)}
              className={inputClass}
            />
          </Field>
          <Field label="Paid (€)">
            <input
              name="amount_paid"
              type="number"
              step="0.01"
              min="0"
              defaultValue={cost.amount_paid ?? ''}
              className={inputClass}
            />
          </Field>
          <Field label="Reimbursed (€)">
            <input
              name="amount_reimbursed"
              type="number"
              step="0.01"
              min="0"
              defaultValue={cost.amount_reimbursed ?? ''}
              className={inputClass}
            />
          </Field>
          <Field label="Streitwert (€)">
            <input
              name="streitwert"
              type="number"
              step="0.01"
              min="0"
              defaultValue={cost.streitwert ?? ''}
              className={inputClass}
            />
          </Field>
          <Field label="Fee factor">
            <input
              name="gebuehren_faktor"
              type="number"
              step="0.1"
              min="0.1"
              defaultValue={cost.gebuehren_faktor ?? ''}
              className={inputClass}
            />
          </Field>
          <Field label="Due">
            <input
              name="due_at"
              type="date"
              defaultValue={cost.due_at ? cost.due_at.slice(0, 10) : ''}
              className={inputClass}
            />
          </Field>
        </div>
        <Field label="Notes">
          <textarea name="notes" defaultValue={cost.notes ?? ''} rows={2} className={inputClass} />
        </Field>
        <Toggle
          checked={reimbursable}
          onChange={setReimbursable}
          label="Reimbursable by the opposing party (§91 ZPO)"
        />
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="secondary"
            className="px-3 py-1.5 text-[11.5px]"
            onClick={onClose}
          >
            Cancel
          </Button>
          <Button type="submit" className="px-3 py-1.5 text-[11.5px]" disabled={pending}>
            Save
          </Button>
        </div>
      </form>
    </Modal>
  )
}

function AddCostModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null
  return <AddCostForm onClose={onClose} />
}

function AddCostForm({ onClose }: { onClose: () => void }) {
  const toast = useToast()
  const cases = useCasesDirectory()
  const create = useCreateCost()
  const [net, setNet] = useState(0)
  const [vat, setVat] = useState(19)
  const [reimbursable, setReimbursable] = useState(true)
  // The directory lists visible cases; the server refuses creation on cases the user cannot edit.
  const editable = cases.data?.cases ?? []
  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const d = new FormData(e.currentTarget)
    const str = (k: string) => String(d.get(k) ?? '').trim()
    create.mutate(
      {
        caseId: str('case_id'),
        category: str('category') as Schemas['CostCategory'],
        title: str('title'),
        rvg_position: str('rvg_position') || null,
        amount_net: net,
        vat_rate: vat / 100,
        status: 'offen',
        streitwert: str('streitwert') ? Number(str('streitwert')) : null,
        gebuehren_faktor: str('gebuehren_faktor') ? Number(str('gebuehren_faktor')) : null,
        due_at: str('due_at') ? new Date(str('due_at')).toISOString() : null,
        notes: str('notes') || null,
        is_reimbursable: reimbursable,
      },
      {
        onSuccess: () => {
          toast('Cost booked')
          onClose()
        },
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }
  return (
    <Modal open onClose={onClose} title="Add cost" icon="payments" width={560}>
      <form onSubmit={submit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Case">
            <select name="case_id" required className={inputClass} defaultValue="">
              <option value="" disabled>
                Select a case…
              </option>
              {editable.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id} · {c.title}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Category">
            <select name="category" className={inputClass} defaultValue="anwaltskosten">
              {CATEGORIES.map(([k, l]) => (
                <option key={k} value={k}>
                  {l}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Title">
          <input
            name="title"
            required
            className={inputClass}
            placeholder="e.g. Verfahrensgebühr 1. Instanz"
          />
        </Field>
        <div className="grid grid-cols-3 gap-3">
          <Field label="Net amount (€)">
            <input
              name="amount_net"
              type="number"
              step="0.01"
              min="0.01"
              required
              value={net || ''}
              onChange={(e) => setNet(Number(e.target.value))}
              className={inputClass}
            />
          </Field>
          <Field label="VAT (%)">
            <input
              name="vat_rate"
              type="number"
              step="1"
              min="0"
              max="100"
              value={vat}
              onChange={(e) => setVat(Number(e.target.value))}
              className={inputClass}
            />
          </Field>
          <Field label="Gross (€)">
            <input
              readOnly
              value={(net * (1 + vat / 100)).toFixed(2)}
              className={`${inputClass} opacity-70`}
              aria-label="Gross amount"
            />
          </Field>
          <Field label="RVG position">
            <input name="rvg_position" className={inputClass} placeholder="VV 3100" />
          </Field>
          <Field label="Streitwert (€)">
            <input name="streitwert" type="number" step="0.01" min="0" className={inputClass} />
          </Field>
          <Field label="Fee factor">
            <input
              name="gebuehren_faktor"
              type="number"
              step="0.1"
              min="0.1"
              className={inputClass}
              placeholder="1.3"
            />
          </Field>
          <Field label="Due">
            <input name="due_at" type="date" className={inputClass} />
          </Field>
        </div>
        <Field label="Notes">
          <textarea name="notes" rows={2} className={inputClass} />
        </Field>
        <Toggle
          checked={reimbursable}
          onChange={setReimbursable}
          label="Reimbursable by the opposing party (§91 ZPO)"
        />
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="secondary"
            className="px-3 py-1.5 text-[11.5px]"
            onClick={onClose}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            className="px-3 py-1.5 text-[11.5px]"
            disabled={create.isPending || editable.length === 0}
          >
            Book cost
          </Button>
        </div>
      </form>
    </Modal>
  )
}
