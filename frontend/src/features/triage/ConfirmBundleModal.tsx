import { type FormEvent, useState } from 'react'

import type { Schemas } from '../../api/client'
import {
  useBatchAssign,
  useBatchConfirm,
  useConfirmBundle,
  type TriageBundle,
} from '../../api/triage'
import { bundleOpenParts } from '../documents/reviewReasons'
import { OpenItems } from './OpenItems'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { Modal } from '../../ui/Modal'
import { TextField } from '../../ui/TextField'
import { useToast } from '../../ui/toast'

export type ConfirmTarget =
  | { mode: 'single'; bundle: TriageBundle; action: 'confirm_bundle' | 'assign_case' }
  | { mode: 'batch'; keys: string[] }
  | { mode: 'batch_confirm'; bundles: TriageBundle[] }

type Props = {
  target: ConfirmTarget | null
  onClose: () => void
  cases: Schemas['PickerCase'][]
  proceedings: Schemas['PickerProceeding'][]
  /** Open one bundle document in the review pane (the modal closes first). */
  onReview: (bundleKey: string, docId: number) => void
  /** The batch confirm went through (clears the selection). */
  onBatchConfirmed: () => void
}

/** Route one bundle (or a selection) to a case: suggested, picked, or newly created. */
export function ConfirmBundleModal(props: Props) {
  const { target, onClose, onBatchConfirmed } = props
  if (!target) return null
  if (target.mode === 'batch_confirm') {
    return (
      <BatchConfirmDialog
        key={target.bundles.map((b) => b.key).join(',')}
        bundles={target.bundles}
        onClose={onClose}
        onDone={onBatchConfirmed}
      />
    )
  }
  return (
    <Dialog
      key={target.mode === 'single' ? target.bundle.key : target.keys.join(',')}
      {...props}
      target={target}
    />
  )
}

function BatchConfirmDialog({
  bundles,
  onClose,
  onDone,
}: {
  bundles: TriageBundle[]
  onClose: () => void
  onDone: () => void
}) {
  const batchConfirm = useBatchConfirm()
  const toast = useToast()
  // Mirrors the server: a suggestion whose case doesn't exist yet is skipped, not created.
  const caseOf = (b: TriageBundle) =>
    b.suggestion ? (b.suggestion.exists ? b.suggestion.case_id : null) : b.confirmed_case_id
  const skipped = bundles.filter((b) => !caseOf(b))
  return (
    <Modal
      open
      onClose={onClose}
      title={`Confirm ${bundles.length} bundles`}
      subtitle={
        skipped.length > 0
          ? `Each goes to its suggested case; ${skipped.length} will be skipped.`
          : 'Each goes to its suggested case.'
      }
      icon="drive_file_move"
      width={520}
    >
      <ul className="space-y-1.5 text-[11.5px]">
        {bundles.map((b) => {
          const open = bundleOpenParts(b)
          return (
            <li key={b.key} className="flex items-start gap-2 rounded-lg border border-line p-2">
              <span className="min-w-0 flex-1">
                <span className="block truncate font-semibold">{b.subject ?? b.key}</span>
                {open.length > 0 && <span className="block text-warning">{open.join(' · ')}</span>}
              </span>
              {caseOf(b) ? (
                <span className="font-mono text-tealink">→ {caseOf(b)}</span>
              ) : (
                <Badge tone="neutral">
                  {b.suggestion ? 'case not created yet' : 'no suggestion'} · skipped
                </Badge>
              )}
            </li>
          )
        })}
      </ul>
      <p className="mt-2 text-[10.5px] text-muted">
        Open items stay open — you can still fix them from the case.
      </p>
      {batchConfirm.error && (
        <p role="alert" className="mt-2 text-[11px] text-danger">
          {batchConfirm.error.message}
        </p>
      )}
      <div className="mt-3 flex justify-end gap-2">
        <Button variant="secondary" onClick={onClose}>
          Cancel
        </Button>
        <Button
          disabled={batchConfirm.isPending}
          onClick={() =>
            batchConfirm.mutate(
              bundles.map((b) => b.key),
              {
                onSuccess: (r) => {
                  toast(`${r.confirmed} confirmed, ${r.skipped} skipped`)
                  onDone()
                  onClose()
                },
              },
            )
          }
        >
          Confirm & complete →
        </Button>
      </div>
    </Modal>
  )
}

function Dialog({
  target,
  onClose,
  cases,
  proceedings,
  onReview,
}: Props & { target: Exclude<ConfirmTarget, { mode: 'batch_confirm' }> }) {
  const confirm = useConfirmBundle()
  const assign = useBatchAssign()
  const toast = useToast()
  const suggestion = target.mode === 'single' ? target.bundle.suggestion : null
  const suggestedProc = target.mode === 'single' ? target.bundle.proceeding : null
  const [useSuggested, setUseSuggested] = useState(!!suggestion)
  const [newCase, setNewCase] = useState(false)
  // A bundle already routed (assign_case) has no suggestion but knows its case.
  const routedCase = target.mode === 'single' ? target.bundle.confirmed_case_id : null
  const [caseId, setCaseId] = useState(suggestion?.case_id ?? routedCase ?? '')
  const pending = confirm.isPending || assign.isPending
  const error = confirm.error ?? assign.error
  const title =
    target.mode === 'batch'
      ? `Assign ${target.keys.length} bundles`
      : target.action === 'confirm_bundle'
        ? 'Confirm bundle'
        : 'Route bundle'
  const docCount = target.mode === 'single' ? target.bundle.doc_count : null

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const d = new FormData(e.currentTarget)
    const proc = String(d.get('proceeding_id') ?? '')
    const routing = {
      case_id: newCase ? null : useSuggested ? (suggestion?.case_id ?? null) : caseId || null,
      new_case_id: newCase ? String(d.get('new_case_id')) : null,
      new_case_title: newCase ? String(d.get('new_case_title')) : null,
      proceeding_id: proc ? Number(proc) : null,
    }
    const done = (msg: string) => {
      toast(msg)
      onClose()
    }
    if (target.mode === 'batch') {
      assign.mutate(
        { keys: target.keys, ...routing },
        { onSuccess: (r) => done(`${r.confirmed} bundle(s) assigned`) },
      )
    } else {
      confirm.mutate(
        {
          key: target.bundle.key,
          batch_id: target.bundle.batch_id,
          doc_id: target.bundle.batch_id === null ? target.bundle.lead_doc_id : null,
          action: target.action,
          ...routing,
        },
        {
          onSuccess: (r) =>
            done(
              r.case.action === 'created'
                ? `Case ${r.case.id} created`
                : target.action === 'confirm_bundle'
                  ? `Filed into ${r.case.id}`
                  : `Assigned to ${r.case.id}`,
            ),
        },
      )
    }
  }

  const effectiveCase = newCase ? '' : useSuggested ? (suggestion?.case_id ?? '') : caseId
  const procOptions = proceedings.filter((p) => p.case_id === effectiveCase)

  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      subtitle={docCount !== null ? `${docCount} docs will be assigned to:` : undefined}
      icon="drive_file_move"
      width={460}
    >
      <form onSubmit={onSubmit} className="space-y-3">
        {target.mode === 'single' && target.action === 'confirm_bundle' && (
          <OpenItems
            bundle={target.bundle}
            onReview={(docId) => {
              onClose()
              onReview(target.bundle.key, docId)
            }}
          />
        )}
        {suggestion && useSuggested && !newCase ? (
          <div className="rounded-xl border border-accent/30 bg-accent/8 p-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[13px] font-semibold text-tealink">
                {suggestion.case_id}
              </span>
              {suggestion.is_draft && <Badge tone="warning">draft</Badge>}
              <Badge tone="accent">AI suggested</Badge>
              <button
                type="button"
                onClick={() => setUseSuggested(false)}
                className="ml-auto text-[10px] text-tealink hover:underline"
              >
                change
              </button>
            </div>
            <div className="text-[11px] text-muted">
              {suggestion.title ??
                (suggestion.exists ? '' : 'AI auto-created this case from the correspondence')}
            </div>
            {suggestion.is_draft && (
              <p className="mt-1 text-[10.5px] text-muted">
                Confirming ratifies the draft case and files all documents into it.
              </p>
            )}
          </div>
        ) : newCase ? (
          <div className="space-y-3 rounded-xl border border-line p-3">
            <TextField
              label="New case ID"
              name="new_case_id"
              required
              className="font-mono"
              placeholder="ADV-025-A"
            />
            <TextField label="Title" name="new_case_title" placeholder="Weber ./. Weber" />
            <button
              type="button"
              onClick={() => setNewCase(false)}
              className="text-[10px] text-tealink hover:underline"
            >
              pick an existing case instead
            </button>
          </div>
        ) : (
          <Field label="Case" htmlFor="case_id">
            <select
              id="case_id"
              value={caseId}
              onChange={(e) => setCaseId(e.target.value)}
              required
              className={inputClass}
            >
              <option value="">— choose a case —</option>
              {cases.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.id} · {c.title}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => setNewCase(true)}
              className="mt-1 text-[10px] text-tealink hover:underline"
            >
              Create new case…
            </button>
          </Field>
        )}
        {!newCase && (
          <Field
            label="Proceeding"
            htmlFor="proceeding_id"
            hint={procOptions.length === 0 ? 'No proceedings on this case yet.' : undefined}
          >
            <select
              id="proceeding_id"
              name="proceeding_id"
              defaultValue={suggestedProc?.id ?? ''}
              className={inputClass}
              disabled={procOptions.length === 0}
            >
              <option value="">— none —</option>
              {procOptions.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </Field>
        )}
        {error && (
          <p role="alert" className="text-[11px] text-danger">
            {error.message}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={pending}>
            {target.mode === 'batch'
              ? 'Assign'
              : target.action === 'confirm_bundle'
                ? 'Confirm & complete →'
                : 'Assign'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
