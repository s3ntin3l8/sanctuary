import type { FormEvent } from 'react'

import { useIdentity, useSaveIdentity } from '../../api/settings'
import { Button } from '../../ui/Button'
import { Field, inputClass } from '../../ui/Field'
import { SettingsCard } from '../../ui/SettingsCard'
import { TextField } from '../../ui/TextField'
import { useToast } from '../../ui/toast'

export function IdentityPage() {
  const identity = useIdentity().data
  const save = useSaveIdentity()
  const toast = useToast()
  if (!identity) return null

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const data = new FormData(event.currentTarget)
    save.mutate(
      {
        own_self: String(data.get('own_self')),
        own_parties: String(data.get('own_parties'))
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean),
        user_context: String(data.get('user_context')),
      },
      { onSuccess: () => toast('Identity saved') },
    )
  }

  return (
    <form className="space-y-4" onSubmit={onSubmit}>
      <SettingsCard
        title="Party identity"
        description="How the AI recognises 'our side' in correspondence and claims."
      >
        <TextField
          label="Your name"
          name="own_self"
          defaultValue={identity.own_self}
          placeholder="Dr. Katharina Vogt"
        />
        <Field
          label="Own-side parties"
          htmlFor="own_parties"
          hint="Comma-separated: your firm, co-counsel, clients you represent."
        >
          <textarea
            id="own_parties"
            name="own_parties"
            rows={2}
            defaultValue={identity.own_parties.join(', ')}
            className={inputClass}
          />
        </Field>
      </SettingsCard>
      <SettingsCard
        title="User context"
        description="Prepended to every AI system prompt — practice area, house style, standing instructions."
      >
        <Field label="Context" htmlFor="user_context">
          <textarea
            id="user_context"
            name="user_context"
            rows={6}
            defaultValue={identity.user_context}
            className={inputClass}
          />
        </Field>
        <Button type="submit" disabled={save.isPending}>
          Save
        </Button>
      </SettingsCard>
    </form>
  )
}
