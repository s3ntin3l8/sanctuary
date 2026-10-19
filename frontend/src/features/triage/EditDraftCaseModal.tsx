import { type FormEvent, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { api, unwrap } from '../../api/client'
import { normalizeCaseId } from '../cases/caseId'
import { Button } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { Modal } from '../../ui/Modal'
import { useToast } from '../../ui/toast'

type Props = {
  open: boolean
  onClose: () => void
  currentCaseId: string
  currentTitle: string
}

/** Modal to edit an AI-drafted case's ID and title directly from Triage. */
export function EditDraftCaseModal({ open, onClose, currentCaseId, currentTitle }: Props) {
  if (!open) return null
  return (
    <EditDraftCaseForm
      onClose={onClose}
      currentCaseId={currentCaseId}
      currentTitle={currentTitle}
    />
  )
}

function EditDraftCaseForm({ onClose, currentCaseId, currentTitle }: Omit<Props, 'open'>) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [caseId, setCaseId] = useState(currentCaseId)
  const [title, setTitle] = useState(currentTitle)
  const [saving, setSaving] = useState(false)

  async function handleSave(ratifyAfterSave: boolean) {
    setSaving(true)
    try {
      const cleanId = normalizeCaseId(caseId) || currentCaseId
      const cleanTitle = title.trim() || currentTitle
      await unwrap(
        api.PATCH('/api/v1/cases/{case_id}', {
          params: { path: { case_id: currentCaseId } },
          body: {
            case_id: cleanId,
            title: cleanTitle,
          },
        }),
      )
      if (ratifyAfterSave) {
        await unwrap(
          api.POST('/api/v1/cases/{case_id}/confirm-draft', {
            params: { path: { case_id: cleanId } },
          }),
        )
        toast(`Case ${cleanId} ratified`)
      } else {
        toast('Draft case updated')
      }
      queryClient.invalidateQueries({ queryKey: ['document'] })
      queryClient.invalidateQueries({ queryKey: ['triage'] })
      queryClient.invalidateQueries({ queryKey: ['cases'] })
      onClose()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err)
      toast(msg, 'error')
    } finally {
      setSaving(false)
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    handleSave(false)
  }

  return (
    <Modal
      open
      onClose={onClose}
      title="Edit draft case"
      subtitle={currentCaseId}
      icon="edit"
      width={480}
    >
      <form onSubmit={submit} className="space-y-3">
        <Field label="Case ID" hint="Changing it renames the draft case and updates its documents.">
          <input
            name="case_id"
            value={caseId}
            onChange={(e) => setCaseId(normalizeCaseId(e.target.value))}
            required
            maxLength={64}
            className={`${inputClass} font-mono`}
          />
        </Field>
        <Field label="Title">
          <input
            name="title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            className={inputClass}
          />
        </Field>
        <div className="flex items-center justify-end gap-2 pt-2">
          <Button variant="secondary" size="sm" type="button" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button variant="secondary" size="sm" type="submit" disabled={saving}>
            Save draft
          </Button>
          <Button size="sm" type="button" disabled={saving} onClick={() => handleSave(true)}>
            Save &amp; ratify
          </Button>
        </div>
      </form>
    </Modal>
  )
}
