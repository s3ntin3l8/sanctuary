import { type FormEvent, useState } from 'react'

import {
  type CaseDetail,
  useDeleteProceeding,
  usePurgeCase,
  useUpdateCase,
  useUpdateProceeding,
} from '../../../api/caseDetail'
import { leaveTo } from '../../../navigation'
import { Button } from '../../../ui/Button'
import { ConfirmDialog } from '../../../ui/ConfirmDialog'
import { Field, inputClass } from '../../../ui/Field'
import { Modal } from '../../../ui/Modal'
import { Toggle } from '../../../ui/Toggle'
import { useToast } from '../../../ui/toast'

const STATUSES = ['intake', 'discovery', 'pre_trial', 'trial', 'post_trial', 'closed'] as const
const CASE_TYPES = ['civil', 'family', 'administrative', 'criminal'] as const
const LEVELS = ['ag', 'lg', 'olg', 'bgh', 'other'] as const
const PROC_STATUSES = ['active', 'closed'] as const

type Props = { open: boolean; onClose: () => void; detail: CaseDetail }

/** Edit the case, its proceedings, or purge it entirely. */
export function EditCaseModal({ open, onClose, detail }: Props) {
  // Mounted only while open so form state starts fresh each time.
  if (!open) return null
  return <EditCaseForm onClose={onClose} detail={detail} />
}

function EditCaseForm({ onClose, detail }: Omit<Props, 'open'>) {
  const toast = useToast()
  const update = useUpdateCase(detail.id)
  const updateProc = useUpdateProceeding(detail.id)
  const deleteProcMutation = useDeleteProceeding(detail.id)
  const purge = usePurgeCase(detail.id)
  const [worst, setWorst] = useState(detail.assume_worst_case)
  const [purging, setPurging] = useState(false)
  const [deleteProc, setDeleteProc] = useState<CaseDetail['proceedings'][number] | null>(null)

  function submitCase(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const d = new FormData(e.currentTarget)
    update.mutate(
      {
        title: String(d.get('title')),
        status: String(d.get('status')) as CaseDetail['status'],
        case_type: String(d.get('case_type')) as CaseDetail['case_type'],
        assume_worst_case: worst,
      },
      {
        onSuccess: () => {
          toast('Case updated')
          onClose()
        },
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }

  return (
    <Modal open onClose={onClose} title="Edit case" subtitle={detail.id} icon="edit" width={560}>
      <form onSubmit={submitCase} className="space-y-3">
        <Field label="Title">
          <input name="title" defaultValue={detail.title} required className={inputClass} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Status">
            <select name="status" defaultValue={detail.status} className={inputClass}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s.replace('_', ' ')}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Case type">
            <select name="case_type" defaultValue={detail.case_type} className={inputClass}>
              {CASE_TYPES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Toggle
          checked={worst}
          onChange={setWorst}
          label="Assume worst case for cost projections"
        />
        <div className="flex justify-end">
          <Button type="submit" className="px-3 py-1.5 text-[11.5px]" disabled={update.isPending}>
            Save case
          </Button>
        </div>
      </form>

      <h3 className="mt-5 mb-2 text-[10px] font-bold tracking-[.12em] text-muted uppercase">
        Proceedings
      </h3>
      <ul className="space-y-2">
        {detail.proceedings.map((p) => (
          <li key={p.id}>
            <form
              className="grid grid-cols-[1fr_90px_120px_90px_auto] items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                const d = new FormData(e.currentTarget)
                updateProc.mutate(
                  {
                    id: p.id,
                    court_name: String(d.get('court_name')),
                    court_level: String(d.get('court_level')) as (typeof LEVELS)[number],
                    az_court: String(d.get('az_court')) || null,
                    status: String(d.get('status')) as (typeof PROC_STATUSES)[number],
                  },
                  {
                    onSuccess: () => toast('Proceeding updated'),
                    onError: (err) => toast(err.message, 'error'),
                  },
                )
              }}
            >
              <Field label="Court">
                <input
                  name="court_name"
                  defaultValue={p.court_name}
                  required
                  className={inputClass}
                />
              </Field>
              <Field label="Level">
                <select name="court_level" defaultValue={p.court_level} className={inputClass}>
                  {LEVELS.map((l) => (
                    <option key={l} value={l}>
                      {l.toUpperCase()}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Aktenzeichen">
                <input name="az_court" defaultValue={p.az_court ?? ''} className={inputClass} />
              </Field>
              <Field label="Status">
                <select name="status" defaultValue={p.status} className={inputClass}>
                  {PROC_STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </Field>
              <div className="flex gap-1">
                <Button type="submit" variant="secondary" className="px-2 py-1.5 text-[11px]">
                  Save
                </Button>
                {p.is_deletable && (
                  <Button
                    type="button"
                    variant="secondary"
                    className="px-2 py-1.5 text-[11px] text-danger"
                    aria-label={`Delete proceeding ${p.court_name}`}
                    onClick={() => setDeleteProc(p)}
                  >
                    Delete
                  </Button>
                )}
              </div>
            </form>
          </li>
        ))}
      </ul>

      <ConfirmDialog
        open={deleteProc !== null}
        onClose={() => setDeleteProc(null)}
        onConfirm={() => {
          if (deleteProc) {
            deleteProcMutation.mutate(deleteProc.id, {
              onSuccess: () => toast('Proceeding deleted'),
              onError: (err) => toast(err.message, 'error'),
            })
          }
          setDeleteProc(null)
        }}
        title={`Delete proceeding ${deleteProc?.court_name ?? ''}?`}
        body="This cannot be undone."
        label="Delete"
        danger
        pending={deleteProcMutation.isPending}
      />

      <section className="mt-5 rounded-xl border border-danger/40 bg-danger/5 p-3">
        <h3 className="text-[10px] font-bold tracking-[.12em] text-danger uppercase">
          Danger zone
        </h3>
        {!purging ? (
          <Button
            variant="secondary"
            className="mt-2 px-2.5 py-1 text-[11px] text-danger"
            onClick={() => setPurging(true)}
          >
            Purge this case…
          </Button>
        ) : (
          <form
            className="mt-2 flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              purge.mutate(String(new FormData(e.currentTarget).get('confirm')), {
                onSuccess: () => leaveTo('/cases'),
                onError: (err) => toast(err.message, 'error'),
              })
            }}
          >
            <Field label={`Type "purge ${detail.id}" to delete everything on disk`}>
              <input name="confirm" className={inputClass} autoComplete="off" />
            </Field>
            <Button
              type="submit"
              className="bg-danger px-2.5 py-1.5 text-[11px]"
              disabled={purge.isPending}
            >
              Purge
            </Button>
          </form>
        )}
      </section>
    </Modal>
  )
}
