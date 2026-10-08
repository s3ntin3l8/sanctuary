import { type FormEvent } from 'react'

import { useAddShare, useRemoveShare, useSharing } from '../../../api/caseDetail'
import { Button } from '../../../ui/Button'
import { Field, inputClass } from '../../../ui/Field'
import { Icon } from '../../../ui/Icon'
import { Modal } from '../../../ui/Modal'
import { QueryState } from '../../../ui/QueryState'
import { useToast } from '../../../ui/toast'

type Props = { open: boolean; onClose: () => void; caseId: string }

/** Who else may view or edit this case. Owner or admin only. */
export function SharingModal({ open, onClose, caseId }: Props) {
  const toast = useToast()
  const sharing = useSharing(caseId, open)
  const add = useAddShare(caseId)
  const remove = useRemoveShare(caseId)

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault()
    const form = e.currentTarget
    const d = new FormData(form)
    add.mutate(
      {
        email: String(d.get('email')),
        permission: String(d.get('permission')) as 'viewer' | 'editor',
      },
      {
        onSuccess: () => {
          toast('Access granted')
          form.reset()
        },
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Share case"
      subtitle={caseId}
      icon="group"
      width={520}
    >
      {!sharing.data ? (
        <QueryState error={sharing.error} pending={sharing.isPending} />
      ) : (
        <>
          <p className="mb-3 text-[11.5px] text-muted">
            Owner: <span className="text-ink">{sharing.data.owner_email ?? '—'}</span>
          </p>
          <ul className="space-y-1.5">
            {sharing.data.shares.map((s) => (
              <li
                key={s.user_id}
                className="flex items-center gap-2 rounded-lg border border-line bg-card px-3 py-2 text-[12px]"
              >
                <Icon name="person" size={14} className="text-muted" />
                <span className="min-w-0 flex-1 truncate">{s.display_name ?? s.email}</span>
                <span className="font-mono text-[10px] text-muted">{s.permission}</span>
                <button
                  type="button"
                  aria-label={`Remove access for ${s.email}`}
                  onClick={() =>
                    remove.mutate(s.user_id, { onError: (err) => toast(err.message, 'error') })
                  }
                  className="text-muted hover:text-danger"
                >
                  <Icon name="close" size={14} />
                </button>
              </li>
            ))}
            {sharing.data.shares.length === 0 && (
              <li className="text-[12px] text-muted">Not shared with anyone.</li>
            )}
          </ul>
          <form onSubmit={submit} className="mt-4 flex items-end gap-2">
            <Field label="Email">
              <input
                name="email"
                type="email"
                required
                className={inputClass}
                placeholder="colleague@example.com"
              />
            </Field>
            <Field label="Permission">
              <select name="permission" defaultValue="viewer" className={inputClass}>
                <option value="viewer">viewer</option>
                <option value="editor">editor</option>
              </select>
            </Field>
            <Button type="submit" disabled={add.isPending}>
              Grant
            </Button>
          </form>
        </>
      )}
    </Modal>
  )
}
