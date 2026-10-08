import { type FormEvent, useState } from 'react'

import type { Schemas } from '../../api/client'
import { useBatchAssign, useConfirmBundle, type TriageBundle } from '../../api/triage'
import { actionableReasons, reviewReasonLabel } from '../documents/reviewReasons'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { Modal } from '../../ui/Modal'
import { TextField } from '../../ui/TextField'
import { useToast } from '../../ui/toast'

export type ConfirmTarget =
  | { mode: 'single'; bundle: TriageBundle; action: 'confirm_bundle' | 'assign_case' }
  | { mode: 'batch'; keys: string[] }

type Props = {
  target: ConfirmTarget | null
  onClose: () => void
  cases: Schemas['PickerCase'][]
  proceedings: Schemas['PickerProceeding'][]
}

/** Route one bundle (or a selection) to a case: suggested, picked, or newly created. */
export function ConfirmBundleModal({ target, onClose, cases, proceedings }: Props) {
  if (!target) return null
  return (
    <Dialog
      key={target.mode === 'single' ? target.bundle.key : target.keys.join(',')}
      target={target}
      onClose={onClose}
      cases={cases}
      proceedings={proceedings}
    />
  )
}

function Dialog({ target, onClose, cases, proceedings }: Props & { target: ConfirmTarget }) {
  const confirm = useConfirmBundle()
  const assign = useBatchAssign()
  const toast = useToast()
  const suggestion = target.mode === 'single' ? target.bundle.suggestion : null
  const suggestedProc = target.mode === 'single' ? target.bundle.proceeding : null
  const [useSuggested, setUseSuggested] = useState(!!suggestion)
  const [newCase, setNewCase] = useState(false)
  const [caseId, setCaseId] = useState(suggestion?.case_id ?? '')
  const pending = confirm.isPending || assign.isPending
  const error = confirm.error ?? assign.error
  const title =
    target.mode === 'batch'
      ? `Assign ${target.keys.length} bundles`
      : target.action === 'confirm_bundle'
        ? 'Confirm bundle'
        : 'Route bundle'
  const docCount = target.mode === 'single' ? target.bundle.doc_count : null
  const openReviews =
    target.mode === 'single' && target.action === 'confirm_bundle'
      ? target.bundle.documents
          .map((d) => ({ doc: d, reasons: actionableReasons(d.review_reasons) }))
          .filter((x) => x.reasons.length > 0)
      : []

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
        {openReviews.length > 0 && (
          <div
            role="note"
            className="rounded-xl border border-warning/40 bg-warning/10 p-3 text-[11px]"
          >
            <p className="font-semibold text-warning">
              {openReviews.length} {openReviews.length === 1 ? 'document needs' : 'documents need'}{' '}
              metadata review
            </p>
            <ul className="mt-1 space-y-0.5 text-ink2">
              {openReviews.map(({ doc, reasons }) => (
                <li key={doc.id} className="flex gap-1.5">
                  <span className="min-w-0 truncate">{doc.title}</span>
                  <span className="shrink-0 text-muted">
                    · {reasons.map(reviewReasonLabel).join(', ')}
                  </span>
                </li>
              ))}
            </ul>
            <p className="mt-1 text-muted">You can still confirm; fix them later from the case.</p>
          </div>
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
