import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import type { Schemas } from '../../api/client'
import {
  useAdminAgentAccess,
  useAdminRevokeAgentToken,
  useAdminSetMcpEnabled,
  useAdminTokenActivity,
} from '../../api/settings'
import { formatAgo, formatShortDate } from '../../time'
import { Badge } from '../../ui/Badge'
import { Button } from '../../ui/Button'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { QueryState } from '../../ui/QueryState'
import { SettingsCard } from '../../ui/SettingsCard'
import { Toggle } from '../../ui/Toggle'
import { useToast } from '../../ui/toast'

type Token = Schemas['AdminTokenView']

const STATUS_TONE = { active: 'success', revoked: 'danger', expired: 'warning' } as const

function scopeLabel(token: Token) {
  if (token.all_cases) return 'All cases (incl. future)'
  return token.cases.map((c) => c.id).join(', ') || 'No cases'
}

export function AdminAgentAccessPage() {
  const query = useAdminAgentAccess()
  const setEnabled = useAdminSetMcpEnabled()
  const revoke = useAdminRevokeAgentToken()
  const toast = useToast()
  const [confirmSwitch, setConfirmSwitch] = useState<boolean | null>(null)
  const [toRevoke, setToRevoke] = useState<Token | null>(null)

  useEffect(() => {
    document.title = 'Agent access | The Sanctuary'
  }, [])
  const view = query.data
  if (!view) return <QueryState error={query.error} pending={query.isPending} />

  return (
    <>
      <SettingsCard
        title="MCP access"
        description="The instance-wide switch. When off, /mcp refuses every request and no new tokens can be created; existing tokens stay and work again when it is switched back on."
      >
        <div className="flex items-center gap-3 text-[12px]">
          <span className="flex-1">MCP access is {view.mcp_enabled ? 'enabled' : 'disabled'}</span>
          <Toggle
            checked={view.mcp_enabled}
            label="MCP access"
            disabled={setEnabled.isPending}
            onChange={(next) => setConfirmSwitch(next)}
          />
        </div>
      </SettingsCard>

      <SettingsCard
        title={`Tokens (${view.tokens.length})`}
        description="Every user's agent tokens. Activity counts the agent's audited actions (writes) over the last 7 days."
      >
        <p className="mb-3 text-[12px] text-muted">
          Create your own token under{' '}
          <Link to="/settings/agent-access" className="text-tealink underline">
            Settings → Agent access
          </Link>
          .
        </p>
        {view.tokens.length === 0 ? (
          <p className="text-[12px] text-muted">No tokens yet.</p>
        ) : (
          <ul className="divide-y divide-line2">
            {view.tokens.map((t) => (
              <TokenRow key={t.id} token={t} onRevoke={() => setToRevoke(t)} />
            ))}
          </ul>
        )}
      </SettingsCard>

      <ConfirmDialog
        open={confirmSwitch !== null}
        onClose={() => setConfirmSwitch(null)}
        title={confirmSwitch ? 'Switch MCP access on?' : 'Switch MCP access off?'}
        body={
          confirmSwitch
            ? 'Agents with a valid token can reach this instance again.'
            : 'Every agent is cut off immediately and nobody can create a token until it is switched back on.'
        }
        label={confirmSwitch ? 'Switch on' : 'Switch off'}
        danger={confirmSwitch === false}
        pending={setEnabled.isPending}
        onConfirm={() => {
          if (confirmSwitch === null) return
          setEnabled.mutate(confirmSwitch, {
            onSuccess: () => {
              toast(confirmSwitch ? 'MCP access enabled' : 'MCP access disabled')
              setConfirmSwitch(null)
            },
            onError: (err) => toast(err.message, 'error'),
          })
        }}
      />

      <ConfirmDialog
        open={toRevoke !== null}
        onClose={() => setToRevoke(null)}
        title="Revoke token?"
        body={`“${toRevoke?.name}” (${toRevoke?.owner_email}) stops working immediately. Anything it already wrote stays.`}
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

function TokenRow({ token, onRevoke }: { token: Token; onRevoke: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="py-3">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[13px] font-semibold">
            <span className="truncate">{token.name}</span>
            <Badge tone={STATUS_TONE[token.status]}>{token.status}</Badge>
            <Badge tone="neutral">
              {token.access === 'read_write' ? 'Read & write' : 'Read-only'}
            </Badge>
            {token.kind === 'oauth' && (
              <Badge tone="accent">via {token.oauth_client_name ?? 'OAuth client'}</Badge>
            )}
          </div>
          <div className="truncate font-mono text-[10.5px] text-muted">
            {token.owner_name || token.owner_email} · {token.prefix}… · {scopeLabel(token)} ·
            created {formatShortDate(token.created_at)} ·{' '}
            {token.last_used_at ? `used ${formatAgo(token.last_used_at)} ago` : 'never used'}
          </div>
        </div>
        <Button
          variant="secondary"
          size="sm"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          Activity ({token.activity_count_7d})
        </Button>
        {token.status === 'active' && (
          <Button variant="danger-outline" size="sm" onClick={onRevoke}>
            Revoke
          </Button>
        )}
      </div>
      {open && <Activity tokenId={token.id} />}
    </li>
  )
}

function Activity({ tokenId }: { tokenId: number }) {
  const query = useAdminTokenActivity(tokenId, true)
  if (!query.data) return <QueryState error={query.error} pending={query.isPending} />
  if (query.data.entries.length === 0)
    return <p className="mt-2 text-[12px] text-muted">No audited activity yet.</p>
  return (
    <ul className="mt-2 space-y-1 rounded-lg border border-line bg-panel2 p-2 font-mono text-[11px]">
      {query.data.entries.map((e) => (
        <li key={e.id} className="flex gap-2">
          <span className="text-muted">{formatAgo(e.created_at)}</span>
          <span className="font-semibold">{e.tool ?? e.event_type}</span>
          <span className="truncate text-muted">
            {e.target_type ? `${e.target_type} ${e.target_id ?? ''}` : ''}
          </span>
        </li>
      ))}
    </ul>
  )
}
