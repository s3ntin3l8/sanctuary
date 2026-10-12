import { type FormEvent, useState } from 'react'

import { useCasesDirectory } from '../../api/cases'
import { useAgentAccess, useCreateAgentToken, useRevokeAgentToken } from '../../api/settings'
import type { Schemas } from '../../api/client'
import { formatAgo, formatShortDate } from '../../format'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Field, inputClass } from '../../ui/Field'
import { Icon } from '../../ui/Icon'
import { QueryState } from '../../ui/QueryState'
import { SettingsCard } from '../../ui/SettingsCard'
import { TextField } from '../../ui/TextField'
import { useToast } from '../../ui/toast'

type Token = Schemas['ApiTokenView']

const EXPIRY = [
  { value: '', label: 'Never' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
  { value: '365', label: '1 year' },
]

function scopeLabel(token: Token) {
  if (token.all_cases) return 'All cases (incl. future)'
  return token.cases.map((c) => c.id).join(', ') || 'No cases'
}

function status(token: Token): { label: string; tone: 'success' | 'danger' | 'warning' } {
  if (token.revoked_at) return { label: 'Revoked', tone: 'danger' }
  if (token.expires_at && new Date(token.expires_at) <= new Date())
    return { label: 'Expired', tone: 'warning' }
  return { label: 'Active', tone: 'success' }
}

export function AgentAccessPage() {
  const query = useAgentAccess()
  const cases = useCasesDirectory().data?.cases ?? []
  const create = useCreateAgentToken()
  const revoke = useRevokeAgentToken()
  const toast = useToast()
  const [allCases, setAllCases] = useState(true)
  const [picked, setPicked] = useState<string[]>([])
  const [created, setCreated] = useState<{ secret: string; name: string } | null>(null)
  const [toRevoke, setToRevoke] = useState<Token | null>(null)

  const view = query.data
  if (!view) return <QueryState error={query.error} pending={query.isPending} />

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = event.currentTarget
    const data = new FormData(form)
    const expiry = String(data.get('expiry') ?? '')
    create.mutate(
      {
        name: String(data.get('name')),
        access: data.get('access') === 'read_write' ? 'read_write' : 'read',
        all_cases: allCases,
        case_ids: allCases ? [] : picked,
        expires_in_days: expiry ? Number(expiry) : null,
      },
      {
        onSuccess: ({ secret, token }) => {
          setCreated({ secret, name: token.name })
          form.reset()
          setPicked([])
          setAllCases(true)
        },
        onError: (err) => toast(err.message, 'error'),
      },
    )
  }

  const authHeader = created ? ['Authorization:', 'Bearer', created.secret].join(' ') : ''
  const snippet = created
    ? `claude mcp add --transport http sanctuary ${view.mcp_url} --header "${authHeader}"`
    : ''

  return (
    <>
      <SettingsCard
        title="Agent access"
        description="Let an external AI agent (Claude Code, Claude Desktop, …) read your cases over MCP instead of you pasting files into a chat."
      >
        <p className="flex gap-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-[12px] text-ink2">
          <Icon name="warning" size={16} />
          <span>
            Case content is sent to whatever model the agent uses. If that is a cloud model, it
            leaves this device. Prefer read-only tokens limited to the cases the agent needs.
          </span>
        </p>
        <div className="font-mono text-[11px] text-muted">
          MCP endpoint · <span className="text-ink2">{view.mcp_url}</span>
        </div>
      </SettingsCard>

      {!view.mcp_enabled && (
        <p
          role="alert"
          className="flex gap-2 rounded-lg border border-danger/40 bg-danger/10 p-3 text-[12px] text-ink2"
        >
          <Icon name="block" size={16} />
          <span>
            An admin has switched MCP access off for this instance. Agents are refused and new
            tokens cannot be created until it is switched back on.
          </span>
        </p>
      )}

      {created && (
        <SettingsCard
          title={`Token “${created.name}” created`}
          description="Copy it now — it is shown only once."
        >
          <pre className="overflow-x-auto rounded-lg border border-line bg-panel2 p-3 font-mono text-[11px] break-all whitespace-pre-wrap">
            {snippet}
          </pre>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                void navigator.clipboard.writeText(snippet)
                toast('Command copied')
              }}
            >
              <Icon name="content_copy" size={16} /> Copy command
            </Button>
            <Button variant="secondary" onClick={() => setCreated(null)}>
              Done
            </Button>
          </div>
        </SettingsCard>
      )}

      <SettingsCard title="New token">
        <form className="space-y-4" onSubmit={submit}>
          <fieldset disabled={!view.mcp_enabled} className="space-y-4">
            <TextField label="Name" name="name" required maxLength={80} placeholder="Claude Code" />

            <fieldset className="space-y-2">
              <legend className="text-[10px] font-bold tracking-[.1em] text-muted uppercase">
                Cases
              </legend>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input type="radio" checked={allCases} onChange={() => setAllCases(true)} />
                All cases, including ones created later
              </label>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input type="radio" checked={!allCases} onChange={() => setAllCases(false)} />
                Only these cases
              </label>
              {!allCases && (
                <ul className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-line p-2">
                  {cases.map((c) => (
                    <li key={c.id}>
                      <label className="flex items-center gap-2 text-[12px]">
                        <input
                          type="checkbox"
                          checked={picked.includes(c.id)}
                          onChange={(e) =>
                            setPicked((p) =>
                              e.target.checked ? [...p, c.id] : p.filter((x) => x !== c.id),
                            )
                          }
                        />
                        <span className="font-mono text-[11px]">{c.id}</span>
                        <span className="truncate text-muted">{c.title}</span>
                      </label>
                    </li>
                  ))}
                  {cases.length === 0 && <li className="text-[12px] text-muted">No cases yet.</li>}
                </ul>
              )}
            </fieldset>

            <fieldset className="space-y-2">
              <legend className="text-[10px] font-bold tracking-[.1em] text-muted uppercase">
                Access
              </legend>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input type="radio" name="access" value="read" defaultChecked />
                Read-only
              </label>
              <label className="flex items-center gap-2 text-[12.5px]">
                <input type="radio" name="access" value="read_write" />
                Read &amp; write — annotate, add claims and deadlines, upload
              </label>
            </fieldset>

            <Field label="Expires" htmlFor="expiry">
              <select id="expiry" name="expiry" className={inputClass} defaultValue="">
                {EXPIRY.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>

            <Button type="submit" disabled={create.isPending || (!allCases && picked.length === 0)}>
              Create token
            </Button>
          </fieldset>
        </form>
      </SettingsCard>

      <SettingsCard title="Tokens">
        {view.tokens.length === 0 ? (
          <p className="text-[12px] text-muted">No tokens yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {view.tokens.map((t) => {
              const s = status(t)
              return (
                <li key={t.id} className="flex items-center gap-3 py-2.5">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-[13px] font-semibold">{t.name}</span>
                      <Badge tone={s.tone}>{s.label}</Badge>
                      <Badge tone="neutral">
                        {t.access === 'read_write' ? 'Read & write' : 'Read-only'}
                      </Badge>
                    </div>
                    <div className="truncate font-mono text-[10.5px] text-muted">
                      {t.prefix}… · {scopeLabel(t)} · created {formatShortDate(t.created_at)} ·{' '}
                      {t.last_used_at ? `used ${formatAgo(t.last_used_at)} ago` : 'never used'}
                      {t.expires_at && ` · expires ${formatShortDate(t.expires_at)}`}
                    </div>
                  </div>
                  {!t.revoked_at && (
                    <Button variant="danger-outline" size="sm" onClick={() => setToRevoke(t)}>
                      Revoke
                    </Button>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </SettingsCard>

      <ConfirmDialog
        open={toRevoke !== null}
        onClose={() => setToRevoke(null)}
        title="Revoke token?"
        body={`“${toRevoke?.name}” stops working immediately. Anything it already wrote stays.`}
        label="Revoke"
        danger
        pending={revoke.isPending}
        onConfirm={() => {
          if (!toRevoke) return
          revoke.mutate(toRevoke.id, {
            onSuccess: () => {
              toast('Token revoked')
              setToRevoke(null)
            },
            onError: (err) => toast(err.message, 'error'),
          })
        }}
      />
    </>
  )
}
