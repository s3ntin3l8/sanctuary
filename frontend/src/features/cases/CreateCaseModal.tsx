import type { FormEvent } from 'react'

import { useCreateCase } from '../../api/cases'
import { leaveTo } from '../../navigation'
import { Button } from '../../ui/Button'
import { Modal } from '../../ui/Modal'
import { TextField } from '../../ui/TextField'

type Props = { open: boolean; onClose: () => void }

const JURISDICTIONS = [
  ['de', 'Germany'],
  ['uk', 'United Kingdom'],
  ['us', 'United States'],
  ['other', 'Other'],
] as const

export function CreateCaseModal({ open, onClose }: Props) {
  const create = useCreateCase()

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    create.mutate(
      {
        case_id: String(form.get('case_id')),
        title: String(form.get('title')),
        court_name: String(form.get('court_name')),
        jurisdiction: form.get('jurisdiction') as 'de' | 'uk' | 'us' | 'other',
      },
      { onSuccess: (created) => leaveTo(`/cases/${created.id}`) },
    )
  }

  return (
    <Modal open={open} onClose={onClose} title="New case" icon="create_new_folder" width={432}>
      <form id="create-case" onSubmit={onSubmit} className="space-y-4">
        <TextField
          label="Case ID"
          name="case_id"
          required
          placeholder="ADV-024-A"
          className="font-mono"
          hint="Short internal identifier; it leads everywhere (URLs, chat, reports)."
        />
        <TextField label="Title" name="title" required placeholder="Weber ./. Weber" />
        <TextField
          label="Court"
          name="court_name"
          required
          placeholder="Amtsgericht Hamburg"
          hint="Creates the first active proceeding; the court level is inferred."
        />
        <div>
          <label
            htmlFor="jurisdiction"
            className="text-[10px] font-bold tracking-[.1em] text-muted uppercase"
          >
            Jurisdiction
          </label>
          <select
            id="jurisdiction"
            name="jurisdiction"
            defaultValue="de"
            className="mt-1 w-full rounded-[9px] border border-line bg-panel2 px-3 py-2 text-[13px] text-ink focus:border-accent focus:outline-none"
          >
            {JURISDICTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        {create.error && (
          <p role="alert" className="text-[11px] font-medium text-danger">
            {create.error.message}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={create.isPending}>
            Create case
          </Button>
        </div>
      </form>
    </Modal>
  )
}
